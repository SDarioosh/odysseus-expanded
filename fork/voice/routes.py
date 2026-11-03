"""Fork: the voice endpoint (docs/fork/voice-assistant.md).

``POST /api/chat/voice/v1/chat/completions`` and ``GET /api/chat/voice/v1/models``
(:data:`PREFIX` is the OpenAI base URL a client is given), OpenAI's shapes
(``fork/voice/openai_api.py``), for a Home Assistant conversation agent. Only an API token
with the ``voice`` scope may call them (a browser login may not: these routes are for a
device, and a token can be revoked on its own). Each question is one read-only brain turn
of the token's owner (``fork/brain/turn.py``, ``read_only_turn``) in a chat of the
``Voice`` folder (``fork/voice/sessions.py``): it can look things up in Athena, the
calendar, notes, contacts, memory, beliefs, documents and the web, and it can't change
anything. Email is left out: a voice assistant answers out loud, to anyone in the room.

While the owner setting "Voice can add and complete items" is on, a voice turn may also
add and complete items, and nothing else (``fork/voice/actions.py``).
``GET``/``PUT /api/fork/voice/settings`` (admin) read and change that setting and its
phone push.

``POST /api/fork/voice/token`` (admin, browser) makes a token with only the ``voice``
scope for the Settings card (``static/js/forkVoiceAssistant.js``); it is shown once and
listed with the other tokens, where it is revoked.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from fork.voice import openai_api
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
# Never offered by voice, whatever the brain's groups say (see the module doc).
LEFT_OUT = frozenset({
    "list_email_accounts", "list_emails", "search_emails", "read_email", "draft_email",
    "draft_email_reply", "send_email", "reply_to_email",
})


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


def _error(status: int, message: str, kind: str = "invalid_request_error") -> JSONResponse:
    return JSONResponse(status_code=status, content=openai_api.error_body(message, kind))


class VoiceSettingsPut(BaseModel):
    actions: bool | None = None
    push: bool | None = None


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

    @router.get("/api/fork/voice/settings", dependencies=[Depends(require_admin)])
    def get_settings() -> Any:
        from fork.voice import actions as voice_actions
        return voice_actions.settings()

    @router.put("/api/fork/voice/settings", dependencies=[Depends(require_admin)])
    def put_settings(body: VoiceSettingsPut) -> Any:
        from fork.voice import actions as voice_actions
        return voice_actions.save(actions=body.actions, push=body.push)

    @router.get(PREFIX + "/models")
    def list_models(request: Request) -> Any:
        voice_owner(request)
        return openai_api.models_body()

    @router.post(PREFIX + "/chat/completions")
    async def chat_completions(request: Request) -> Any:
        owner = voice_owner(request)
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
        key = voice_sessions.conversation_key(explicit=question.conversation,
                                              first_question=question.first_question)
        try:
            sess = voice_sessions.chat_for(session_manager, owner, key, question.text)
        except voice_sessions.NoModel as exc:
            return _error(503, str(exc), "server_error")
        from fork.brain.turn import read_only_turn
        from fork.voice import actions as voice_actions

        actions = voice_actions.for_turn()  # None: read-only, exactly as before
        events = read_only_turn(
            request, sess=sess, session_id=str(sess.id), message=question.text,
            owner=owner, disabled=LEFT_OUT, services=(session_manager,),
            instructions=voice_prompt.ACTION_INSTRUCTIONS if actions is not None
            else voice_prompt.INSTRUCTIONS,
            **({"actions": actions} if actions is not None else {}))
        if events is None:
            return _error(503, "Voice answers need the brain (FORK_BRAIN) and an owner "
                               "allowed to use the agent.", "server_error")
        answer = openai_api.Answer(streaming=question.stream)
        if question.stream:
            return StreamingResponse(
                openai_api.stream(events, question, answer, str(sess.id)),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache, no-transform",
                         "X-Accel-Buffering": "no"})
        try:
            await openai_api.collect(events, answer)
        except Exception:  # noqa: BLE001 -- a spoken apology instead of a 500
            logger.exception("voice: the turn failed")
            answer.failed = True
            answer.finish()
        return openai_api.completion_body(question, answer, str(sess.id))

    return router
