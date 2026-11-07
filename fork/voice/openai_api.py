"""Fork: the OpenAI chat-completions shapes a voice client sends and reads
(docs/fork/voice-assistant.md, "The endpoint").

Only what a voice satellite needs: the newest user message is the question, earlier
turns only name the conversation (``fork/voice/sessions.py``; the chat's own history is
what the model sees), and the client's own system prompt and tools are ignored (Home
Assistant handles its own device commands before it asks us). The answer comes back as
one assistant message, or as ``chat.completion.chunk`` events with ``stream: true``, in
the text :mod:`fork.voice.speech` makes speakable. Every response also carries an
``odysseus`` object: the chat id and the links taken out of the spoken text.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from fork.voice import prompt as voice_prompt
from fork.voice.speech import (SpeechStream, end_with_question, end_with_statement,
                               end_without_question)

logger = logging.getLogger(__name__)

MODEL_ID = "odysseus"
MAX_QUESTION_CHARS = 2000


class BadRequest(ValueError):
    """The request isn't a chat completion we can answer."""


@dataclass(frozen=True)
class Question:
    text: str
    stream: bool
    conversation: str | None  # the client's conversation id, if it sent one
    first_question: str | None  # the first user message, when earlier turns were sent
    model: str = MODEL_ID
    # the conversation's first user message: this question when it is the first one
    # (remembered, so the next turn, which sends it as its first, continues the chat)
    opening: str | None = None


def _content_text(content: Any) -> str:
    """A message's text: a string, or the text parts of a list of content parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [p.get("text") for p in content
                 if isinstance(p, dict) and p.get("type") in ("text", "input_text")]
        return " ".join(str(p) for p in parts if p)
    return ""


def parse(body: Any, *, conversation_header: str | None = None) -> Question:
    if not isinstance(body, dict):
        raise BadRequest("The body must be a JSON object.")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise BadRequest("'messages' must be a non-empty list.")
    users = [_content_text(m.get("content")).strip() for m in messages
             if isinstance(m, dict) and m.get("role") == "user"]
    users = [u for u in users if u]
    if not users:
        raise BadRequest("There is no user message to answer.")
    question = users[-1]
    if len(question) > MAX_QUESTION_CHARS:
        raise BadRequest(f"The question is longer than {MAX_QUESTION_CHARS} characters.")
    user = body.get("user")
    conversation = (user if isinstance(user, str) and user.strip() else None) \
        or (conversation_header or None)
    model = body.get("model")
    return Question(text=question, stream=body.get("stream") is True,
                    conversation=conversation,
                    first_question=users[0] if len(users) > 1 else None,
                    model=model if isinstance(model, str) and model.strip() else MODEL_ID,
                    opening=users[0])


def error_body(message: str, kind: str = "invalid_request_error") -> dict:
    return {"error": {"message": message, "type": kind}}


def models_body() -> dict:
    return {"object": "list", "data": [
        {"id": MODEL_ID, "object": "model", "created": 0, "owned_by": "odysseus"}]}


# ------------------------------------------------------------------ brain events in
@dataclass
class Answer:
    """What a voice turn said: fed the brain's events, gives the spoken pieces.

    ``streaming``: speak each finished sentence at once. Otherwise (a whole answer) only
    the text of the rounds that ended without a tool call is spoken, so a model's "Let
    me search…" before its tool calls isn't read out."""
    streaming: bool = True
    speech: SpeechStream = field(default_factory=SpeechStream)
    rounds: list = field(default_factory=lambda: [["", False]])  # [text, called a tool]
    heard_text: bool = False  # the brain streamed any answer text
    refused: bool = False  # the turn wanted an approval: nobody can give one by voice
    failed: bool = False
    tokens: tuple[int, int] = (0, 0)
    # the confirmation question while a proposal of this turn waits for a yes, or None
    # (fork/voice/confirm.py); then the spoken answer ends with it
    confirmation: Callable[[], str | None] | None = None
    awaiting: str | None = None  # the question the answer ended with, once finished
    # said in place of a last question when no change is kept: the turn answers a "yes"
    # that had nothing to confirm, so asking again would loop (confirm.UNHELD)
    no_question: str | None = None
    held: str = ""  # streamed text ending in "?", held until more comes or the end
    sent: str = ""  # what the client got so far, while streaming
    # the satellite's barge-in cut the turn short (fork/voice/interrupts.py): nothing
    # more is said, not even an apology, so Home Assistant ends its run at once
    interrupted: bool = False

    def interrupt(self) -> None:
        """The turn was cut short: the answer is what was already sent, nothing more."""
        self.interrupted = True
        self.speech.spoken = self.sent
        self.speech.links = []
        self.held = ""
        self.awaiting = None

    def _out(self, piece: str) -> str:
        """A spoken piece out while streaming; one that ends with "?" is held back, so
        a last question can still be changed (:meth:`finish`)."""
        if not piece:
            return ""
        text = self.held + piece
        if text.rstrip().endswith("?"):
            self.held = text
            return ""
        self.held = ""
        self.sent += text
        return text

    def feed(self, chunk: str) -> str:
        """One SSE chunk of the brain's turn in; the text to speak now out (or "")."""
        if chunk.startswith("event: error"):
            self.failed = True
            return ""
        if not chunk.startswith("data: "):
            return ""
        body = chunk[6:].strip()
        if body == "[DONE]":
            return ""
        try:
            data = json.loads(body)
        except ValueError:
            return ""
        if not isinstance(data, dict):
            return ""
        if isinstance(data.get("delta"), str) and not data.get("thinking"):
            self.heard_text = True
            if not self.streaming:
                self.rounds[-1][0] += data["delta"]
                return ""
            return self._out(self.speech.feed(data["delta"]))
        kind = data.get("type")
        if kind == "tool_start":
            self.rounds[-1][1] = True
        elif kind == "agent_step":
            self.rounds.append(["", False])
        if kind == "final_response" and not self.heard_text:
            # the brain's replacement answer (a fixed reply, an approval question)
            self.heard_text = True
            if not self.streaming:
                self.rounds[-1][0] += str(data.get("content") or "")
                return ""
            return self._out(self.speech.feed(str(data.get("content") or "")))
        if kind == "ask_user" or (kind == "tool_output" and isinstance(data.get("ask_user"), dict)):
            self.refused = True
        elif kind == "metrics":
            metrics = data.get("data") or {}
            self.tokens = (int(metrics.get("input_tokens") or 0),
                           int(metrics.get("output_tokens") or 0))
        elif kind == "error":
            self.failed = True
        return ""

    def _body(self) -> None:
        if not self.streaming:
            answer = [text for text, called in self.rounds if text.strip() and not called]
            self.speech.feed("\n\n".join(answer))
            self.rounds = [["", False]]
        self.speech.finish()
        if self.speech.spoken:
            return
        if self.refused:
            # a read-only turn never pauses (fork/brain/tools.py); if one did, say so
            self.speech.feed(voice_prompt.READ_ONLY_REPLY)
        elif self.failed:
            self.speech.feed(voice_prompt.FAILED_REPLY)
        elif self.confirmation is None or not self.confirmation():
            self.speech.feed(voice_prompt.NO_ANSWER_REPLY)
        self.speech.finish()

    def finish(self) -> str:
        """The rest to speak once the turn is over. A kept change's confirmation question
        replaces any question the model ended with (then :attr:`awaiting` is it); else a
        filler offer at the end is dropped and a real question is kept."""
        if self.interrupted:
            return ""
        self._body()
        spoken = self.speech.spoken
        question = self.confirmation() if self.confirmation is not None else None
        final = end_with_question(spoken, question) if question else end_without_question(spoken)
        if not question and self.no_question and final.endswith("?"):
            final = end_with_statement(final, self.no_question)
        if not final.startswith(self.sent.rstrip()):
            final = spoken  # never: only the held tail changes; then left as it is
        if question and final.endswith("?"):
            cut = max(final.rfind(". ", 0, len(final) - 1), final.rfind("! ", 0, len(final) - 1))
            self.awaiting = final[cut + 2:] if cut >= 0 else final
        self.speech.spoken = final
        rest = final[len(self.sent):]
        self.sent = final
        self.held = ""
        return rest

    def extra(self, session_id: str, turn_id: str | None = None) -> dict:
        out = {"session_id": session_id, "links": list(self.speech.links)}
        if turn_id:
            out["turn_id"] = turn_id
        return out


def _usage(answer: Answer) -> dict:
    prompt, completion = answer.tokens
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion}


async def collect(events: AsyncIterator[str], answer: Answer,
                  interrupted: Callable[[], bool] | None = None) -> str:
    """The whole answer. ``interrupted()`` true once the events end: the turn was cut
    short (:meth:`Answer.interrupt`), and the answer is empty."""
    async for chunk in events:
        answer.feed(chunk)
    if interrupted is not None and interrupted():
        answer.interrupt()
    answer.finish()
    return answer.speech.spoken


def completion_body(question: Question, answer: Answer, session_id: str,
                    turn_id: str | None = None) -> dict:
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex, "object": "chat.completion",
        "created": int(time.time()), "model": question.model,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": answer.speech.spoken}}],
        "usage": _usage(answer), "odysseus": answer.extra(session_id, turn_id),
    }


def _chunk(cid: str, created: int, model: str, delta: dict, finish: str | None = None,
           **extra) -> str:
    payload = {"id": cid, "object": "chat.completion.chunk", "created": created,
               "model": model, "choices": [{"index": 0, "delta": delta,
                                            "finish_reason": finish}], **extra}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def stream(events: AsyncIterator[str], question: Question, answer: Answer,
                 session_id: str, turn_id: str | None = None,
                 on_done: Callable[[Answer], None] | None = None,
                 interrupted: Callable[[], bool] | None = None) -> AsyncIterator[str]:
    """The answer as OpenAI stream chunks, one finished sentence at a time.
    ``on_done(answer)`` runs once the answer is complete (the turn ledger).
    ``interrupted()`` true once the events end: the turn was cut short, and the stream
    ends with what was already sent (:meth:`Answer.interrupt`)."""
    cid, created = "chatcmpl-" + uuid.uuid4().hex, int(time.time())
    yield _chunk(cid, created, question.model, {"role": "assistant", "content": ""})
    try:
        async for chunk in events:
            piece = answer.feed(chunk)
            if piece:
                yield _chunk(cid, created, question.model, {"content": piece})
    except Exception:  # noqa: BLE001 -- the client gets a spoken apology, not a cut stream
        logger.exception("voice: the turn failed while streaming")
        answer.failed = True
    if interrupted is not None and interrupted():
        answer.interrupt()
    piece = answer.finish()
    if piece:
        yield _chunk(cid, created, question.model, {"content": piece})
    if on_done is not None:
        try:
            on_done(answer)
        except Exception:  # noqa: BLE001 -- the answer still ends
            logger.exception("voice: recording the turn failed")
    yield _chunk(cid, created, question.model, {}, "stop", usage=_usage(answer),
                 odysseus=answer.extra(session_id, turn_id))
    yield "data: [DONE]\n\n"
