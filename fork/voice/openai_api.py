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
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from fork.voice import prompt as voice_prompt
from fork.voice.speech import SpeechStream

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
                    model=model if isinstance(model, str) and model.strip() else MODEL_ID)


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
            return self.speech.feed(data["delta"])
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
            return self.speech.feed(str(data.get("content") or ""))
        if kind == "ask_user" or (kind == "tool_output" and isinstance(data.get("ask_user"), dict)):
            self.refused = True
        elif kind == "metrics":
            metrics = data.get("data") or {}
            self.tokens = (int(metrics.get("input_tokens") or 0),
                           int(metrics.get("output_tokens") or 0))
        elif kind == "error":
            self.failed = True
        return ""

    def finish(self) -> str:
        """The rest to speak once the turn is over."""
        if not self.streaming:
            answer = [text for text, called in self.rounds if text.strip() and not called]
            self.speech.feed("\n\n".join(answer))
            self.rounds = [["", False]]
        rest = self.speech.finish()
        if self.refused and not self.speech.spoken:
            # a read-only turn never pauses (fork/brain/tools.py); if one did, say so
            return self.speech.feed(voice_prompt.READ_ONLY_REPLY) + self.speech.finish()
        if self.speech.spoken:
            return rest
        reply = voice_prompt.FAILED_REPLY if self.failed else voice_prompt.NO_ANSWER_REPLY
        return self.speech.feed(reply) + self.speech.finish()

    def extra(self, session_id: str) -> dict:
        return {"session_id": session_id, "links": list(self.speech.links)}


def _usage(answer: Answer) -> dict:
    prompt, completion = answer.tokens
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion}


async def collect(events: AsyncIterator[str], answer: Answer) -> str:
    async for chunk in events:
        answer.feed(chunk)
    answer.finish()
    return answer.speech.spoken


def completion_body(question: Question, answer: Answer, session_id: str) -> dict:
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex, "object": "chat.completion",
        "created": int(time.time()), "model": question.model,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": answer.speech.spoken}}],
        "usage": _usage(answer), "odysseus": answer.extra(session_id),
    }


def _chunk(cid: str, created: int, model: str, delta: dict, finish: str | None = None,
           **extra) -> str:
    payload = {"id": cid, "object": "chat.completion.chunk", "created": created,
               "model": model, "choices": [{"index": 0, "delta": delta,
                                            "finish_reason": finish}], **extra}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def stream(events: AsyncIterator[str], question: Question, answer: Answer,
                 session_id: str) -> AsyncIterator[str]:
    """The answer as OpenAI stream chunks, one finished sentence at a time."""
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
    piece = answer.finish()
    if piece:
        yield _chunk(cid, created, question.model, {"content": piece})
    yield _chunk(cid, created, question.model, {}, "stop", usage=_usage(answer),
                 odysseus=answer.extra(session_id))
    yield "data: [DONE]\n\n"
