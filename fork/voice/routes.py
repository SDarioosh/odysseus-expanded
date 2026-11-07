"""Fork: the voice endpoint (docs/fork/voice-assistant.md).

``POST /api/chat/voice/v1/chat/completions`` and ``GET /api/chat/voice/v1/models``
(:data:`PREFIX` is the OpenAI base URL a client is given), OpenAI's shapes
(``fork/voice/openai_api.py``), for a Home Assistant conversation agent, and the device
screen's feeds ``GET …/actions`` and ``GET …/turns/latest`` (``fork/voice/ledger.py``),
and ``GET …/today``, the day at a glance (``fork/voice/today_route.py``), and the Done
buttons' ``POST …/todos/done`` and the rest (``fork/voice/done.py``).
Only an API token with the ``voice`` scope may call them (a browser login may not: these
routes are for a device, and a token can be revoked on its own).

Each question is one brain turn of the token's owner (``fork/brain/turn.py``,
``read_only_turn`` with the chat's prompt prefix) in a chat of the ``Voice`` folder
(``fork/voice/sessions.py``): it can look things up in Athena, the calendar, notes,
contacts, memory, beliefs, documents and the web; email is never run (a voice assistant
answers out loud, to anyone in the room), and a change of the allow-list is only kept
until the owner says yes to it (``fork/voice/policy.py``, ``confirm.py``). The satellite's
context (device, speaker; ``fork/voice/satellite.py``, when present) is claimed first.

``POST /api/fork/voice/token`` (admin, browser) makes a token with only the ``voice``
scope for the Settings card (``static/js/forkVoiceAssistant.js``); it is shown once and
listed with the other tokens, where it is revoked.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from fork.voice import confirm, interrupts, ledger, openai_api, timings
from fork.voice import policy as voice_policy
from fork.voice import prompt as voice_prompt
from fork.voice import sessions as voice_sessions

logger = logging.getLogger(__name__)

SCOPE = "voice"
# Under /api/chat: app.py's request timeout (REQUEST_HARD_TIMEOUT, 45 s) leaves that prefix
# alone, as chat streams do, and an answer can take longer; and a /v1 of upstream's own
# later can't clash with it. Clients take PREFIX as their OpenAI base URL.
PREFIX = "/api/chat/voice/v1"
TOKEN_NAME = "Voice assistant"
CONVERSATION_HEADER = "x-conversation-id"
# Never run by voice (listed, so the prompt's tools match a chat's; refused at execution)
LEFT_OUT = voice_policy.EMAIL_TOOLS


def voice_owner(request: Request) -> str:
    """The owner of the request's API token, which must hold the ``voice`` scope."""
    if not getattr(request.state, "api_token", False):
        raise HTTPException(403, "This endpoint needs an Odysseus API token with the "
                                 "'voice' scope.")
    scopes = set(getattr(request.state, "api_token_scopes", None) or ())
    if SCOPE not in scopes:
        raise HTTPException(403, "API token missing required scope: voice")
    owner = getattr(request.state, "api_token_owner", None)
    if not owner:
        raise HTTPException(403, "API token has no owner")
    return str(owner)


# ------------------------------------------------------------- the satellite's context
async def satellite_context(owner: str) -> tuple[Any, Any]:
    """``(the satellite module or None, its claimed context or None)``. The module
    (``fork/voice/satellite.py``) is optional: without it there is no context and every
    speaker gets personal answers."""
    try:
        from fork.voice import satellite
    except ImportError:
        return None, None
    try:
        return satellite, await satellite.claim(owner)
    except Exception:  # noqa: BLE001 -- answered without the context
        logger.warning("voice: claiming the satellite context failed", exc_info=True)
        return satellite, None


def personal_allowed(module: Any, ctx: Any) -> bool:
    if module is None:
        return True
    try:
        return bool(module.personal_allowed(ctx))
    except Exception:  # noqa: BLE001 -- can't tell: not personal
        logger.warning("voice: the speaker check failed", exc_info=True)
        return False


def private_reply(module: Any) -> str:
    return str(getattr(module, "PRIVATE_REPLY", "") or voice_policy.PRIVATE_REPLY)


def device_of(ctx: Any) -> dict | None:
    if ctx is None:
        return None
    return {"id": getattr(ctx, "device_id", "") or "",
            "name": getattr(ctx, "device_name", "") or "",
            "area": getattr(ctx, "area", "") or ""}


def speaker_of(ctx: Any) -> dict | None:
    sp = getattr(ctx, "speaker", None) if ctx is not None else None
    if sp is None:
        return None
    return {"verified": bool(getattr(sp, "verified", False)),
            "is_owner": bool(getattr(sp, "is_owner", False)),
            "name": getattr(sp, "name", None)}


def asked_from(ctx: Any) -> str:
    """The context message's line: "Asked from: Kitchen Echo, Kitchen"."""
    device = device_of(ctx) or {}
    parts = [p for p in (device.get("name"), device.get("area")) if p]
    if len(parts) == 2 and parts[0].casefold() == parts[1].casefold():
        parts = parts[:1]
    return "Asked from: " + ", ".join(parts) if parts else ""


# ------------------------------------------------------------- answered without a model
async def _fixed(text: str):
    yield "data: " + json.dumps({"type": "final_response", "content": text}) + "\n\n"
    yield "data: [DONE]\n\n"


def _save_exchange(sess: Any, session_manager: Any, question: str, answer: str) -> None:
    """A yes / no and its reply, saved in the voice chat like a turn."""
    try:
        from core.models import ChatMessage
        from routes.chat_helpers import save_assistant_response

        sess.add_message(ChatMessage("user", question, metadata={"interaction_mode": "agent",
                                                                 "voice": "confirmation"}))
        save_assistant_response(sess, session_manager, str(sess.id), answer,
                                {"voice": "confirmation"})
    except Exception:  # noqa: BLE001 -- the change is done; the chat misses the lines
        logger.warning("voice: could not save the confirmation in the chat", exc_info=True)


async def confirmation_reply(*, owner: str, sess: Any, session_manager: Any,
                             question: str, turn_id: str, personal: bool,
                             module: Any) -> str | None:
    """The reply when this question answers a kept proposal ("yes" / "no"), after
    running or dropping it; None when it is a new question (the proposal is dropped)."""
    proposal = confirm.pending(owner, str(sess.id))
    if proposal is None:
        return None
    kind = confirm.reply_kind(question)
    confirm.drop(owner, str(sess.id))
    if kind == "other":
        return None
    if kind == "no":
        text = confirm.DECLINED
    elif not personal:
        text = private_reply(module)
    else:
        outcomes = await confirm.run(proposal)
        for o in outcomes:
            if not o.ok:
                continue
            c = o.change
            try:
                ledger.add_action(turn_id, owner, action=c.action, kind=c.kind, title=c.title,
                                  where=c.where, detail=c.detail, due=c.due,
                                  all_day=c.all_day, source=c.source)
            except Exception:  # noqa: BLE001
                logger.exception("voice: recording an action failed")
        text = confirm.reply(outcomes)
    _save_exchange(sess, session_manager, question, text)
    return text


def _last_question(sess: Any) -> str:
    """The voice chat's previous question, before this one is added."""
    for m in reversed(list(getattr(sess, "history", None) or ())):
        if getattr(m, "role", None) == "user":
            return str(getattr(m, "content", "") or "")
    return ""


def _last_answer(sess: Any) -> str:
    """The voice chat's previous answer, before this question is added."""
    for m in reversed(list(getattr(sess, "history", None) or ())):
        if getattr(m, "role", None) == "assistant":
            return str(getattr(m, "content", "") or "")
    return ""


def _timing_start(owner: str, turn_id: str, ctx: Any, timer: Any) -> str | None:
    try:
        return timings.save_hub(owner, turn_id, getattr(ctx, "device_id", None), timer)
    except Exception:  # noqa: BLE001 -- timing never breaks a turn
        logger.warning("voice: saving the turn's timing failed", exc_info=True)
        return None


def _timing_end(row_id: str | None, timer: Any, answer: Any, *, interrupted: bool,
                fixed: bool) -> None:
    if row_id is None:
        return
    try:
        if not answer.streaming:
            timer.mark("first_spoken")  # the whole answer goes back at once
        outcome = ("interrupted" if interrupted else "failed" if answer.failed
                   else "fixed" if fixed else "answered")
        timer.finish(outcome)
        timings.update_hub(row_id, timer)
    except Exception:  # noqa: BLE001
        logger.warning("voice: saving the turn's timing failed", exc_info=True)


def _error(status: int, message: str, kind: str = "invalid_request_error") -> JSONResponse:
    return JSONResponse(status_code=status, content=openai_api.error_body(message, kind))


def setup_voice_routes() -> APIRouter:
    from core.middleware import require_admin

    router = APIRouter(tags=["fork-voice"])

    @router.post("/api/fork/voice/token", dependencies=[Depends(require_admin)])
    def create_token(request: Request) -> Any:
        from datetime import date

        from fork.athena.phone_pairing import create_api_token
        from src.auth_helpers import get_current_user

        from src.agent_runtime.authority import is_internal_tool_request

        if is_internal_tool_request(request) or getattr(request.state, "api_token", False):
            raise HTTPException(403, "Only a signed-in admin can make a voice token")
        owner = get_current_user(request)
        if not owner:
            raise HTTPException(403, "Not signed in")
        token = create_api_token(owner, f"{TOKEN_NAME} {date.today().isoformat()}", [SCOPE])
        invalidate = getattr(request.app.state, "invalidate_token_cache", None)
        if callable(invalidate):
            invalidate()
        return {"token": token["token"], "id": token["id"], "scopes": [SCOPE],
                "model": openai_api.MODEL_ID}

    @router.get(PREFIX + "/models")
    def list_models(request: Request) -> Any:
        voice_owner(request)
        return openai_api.models_body()

    @router.get(PREFIX + "/actions")
    def list_actions(request: Request, since: int = 0, limit: int = ledger.DEFAULT_LIMIT) -> Any:
        owner = voice_owner(request)
        return {"actions": ledger.actions(owner, since_ms=since, limit=limit)}

    @router.get(PREFIX + "/turns/latest")
    def latest_turn(request: Request, since: int = 0) -> Any:
        owner = voice_owner(request)
        return {"turn": ledger.latest_turn(owner, since_ms=since)}

    @router.post(PREFIX + "/chat/completions")
    async def chat_completions(request: Request) -> Any:
        owner = voice_owner(request)
        timer = timings.HubTimer()  # how long each step took (fork/voice/timings.py)
        # No browser sends a zone: event times and "today" use the owner's.
        from fork.common.local_time import use_owner_timezone
        from src.user_time import clear_user_time_context
        clear_user_time_context()
        use_owner_timezone(owner)
        try:
            body = await request.json()
        except ValueError:
            return _error(400, "The body must be JSON.")
        try:
            question = openai_api.parse(
                body, conversation_header=request.headers.get(CONVERSATION_HEADER))
        except openai_api.BadRequest as exc:
            return _error(400, str(exc))
        session_manager = getattr(request.app.state, "session_manager", None)
        if session_manager is None:
            return _error(503, "Odysseus isn't ready yet.", "server_error")
        # A first question starts a new chat, kept under itself so the follow-up (which
        # sends it as its first) continues it; an explicit conversation id goes first.
        key = voice_sessions.conversation_key(
            explicit=question.conversation,
            first_question=question.first_question or question.opening)
        first = not question.conversation and question.first_question is None
        try:
            sess = voice_sessions.chat_for(session_manager, owner, key, question.text,
                                           new=first)
        except voice_sessions.NoModel as exc:
            return _error(503, str(exc), "server_error")
        module, ctx = await satellite_context(owner)
        personal = personal_allowed(module, ctx)
        session_id = str(sess.id)
        turn_id = ledger.start_turn(owner, question.text, session_id=session_id,
                                    device=device_of(ctx), speaker=speaker_of(ctx))
        # A barge-in on the asking device cuts this turn short (fork/voice/interrupts.py).
        stop = interrupts.start(turn_id, owner, getattr(ctx, "device_id", None))
        if getattr(ctx, "interrupted", False):
            stop.set()  # the device was interrupted before Home Assistant asked
        if stop.is_set():
            fixed = ""
        else:
            fixed = await confirmation_reply(
                owner=owner, sess=sess, session_manager=session_manager, question=question.text,
                turn_id=turn_id, personal=personal, module=module)
        # A yes to a question the model asked without calling the tool: nothing waits for
        # it, so the model is told to call the tool now and never asks the same again.
        unheld = fixed is None and confirm.unheld_yes(question.text, _last_answer(sess))
        if fixed is not None:
            events = _fixed(fixed)
            confirmation = None
        else:
            from fork.brain.turn import read_only_turn
            from fork.voice.chat_prefix import default_preset

            gate = voice_policy.VoiceGate(owner=owner, session_id=session_id,
                                          turn_id=turn_id, personal=personal,
                                          private_reply=private_reply(module),
                                          earlier=_last_question(sess))
            events = read_only_turn(
                request, sess=sess, session_id=session_id, message=question.text,
                owner=owner, instructions=voice_prompt.INSTRUCTIONS,
                services=(session_manager,), preset=default_preset(), gate=gate,
                shared_prefix=True,
                context_lines=(asked_from(ctx),) + ((confirm.UNHELD_NOTE,) if unheld else ()),
                no_memory=not personal)
            if events is None:
                interrupts.end(turn_id)
                ledger.finish_turn(turn_id, answer="")
                return _error(503, "Voice answers need the brain (FORK_BRAIN) and an owner "
                                   "allowed to use the agent.", "server_error")

            def confirmation() -> str | None:
                p = confirm.pending(owner, session_id)
                return p.question() if p is not None and p.turn_id == turn_id else None

        answer = openai_api.Answer(streaming=question.stream, confirmation=confirmation,
                                   no_question=confirm.UNHELD if unheld else None)
        events = interrupts.until_stopped(events, stop)
        timer.mark("ready")  # timing never fails the turn
        timing_row = _timing_start(owner, turn_id, ctx, timer)
        events = timings.observed(events, timer)

        def done(a: openai_api.Answer) -> None:
            interrupts.end(turn_id)
            if stop.is_set():
                # Nobody heard it: no answer, and no change waits for a "yes".
                a.interrupt()
                p = confirm.pending(owner, session_id)
                if p is not None and p.turn_id == turn_id:
                    confirm.drop(owner, session_id)
                logger.info("voice: turn %s was interrupted by its satellite", turn_id)
            ledger.finish_turn(turn_id, answer=a.speech.spoken, links=list(a.speech.links),
                               awaiting_confirmation=a.awaiting)
            _timing_end(timing_row, timer, a, interrupted=stop.is_set(),
                        fixed=fixed is not None)

        if question.stream:
            return StreamingResponse(
                timings.first_spoken(
                    openai_api.stream(events, question, answer, session_id, turn_id,
                                      on_done=done, interrupted=stop.is_set),
                    timer, lambda: answer.sent),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache, no-transform",
                         "X-Accel-Buffering": "no"})
        try:
            await openai_api.collect(events, answer, interrupted=stop.is_set)
        except Exception:  # noqa: BLE001 -- a spoken apology instead of a 500
            logger.exception("voice: the turn failed")
            answer.failed = True
            answer.finish()
        try:
            done(answer)
        except Exception:  # noqa: BLE001
            logger.exception("voice: recording the turn failed")
        return openai_api.completion_body(question, answer, session_id, turn_id)

    from fork.voice.today_route import setup_voice_today_routes

    router.include_router(setup_voice_today_routes())  # GET …/today, for device screens
    from fork.voice.done import setup_voice_done_routes

    router.include_router(setup_voice_done_routes())  # Done / undo from a device's screen
    return router
