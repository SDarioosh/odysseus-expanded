"""Fork: the brain's turn: the hook's entry point, the loop and the chat window's events
(docs/fork/brain.md, "The loop").

Hook: ``chat_stream`` (``routes/chat_routes.py``) calls :func:`brain_response` once the
session, owner and message are resolved, the chat privileges enforced, a tool approval
consumed (upstream's own checks) and the research decision made. It returns None, and the
route goes on as upstream wrote it, when ``FORK_BRAIN`` is off, for ``/research`` and
other turns the brain doesn't take (compare, plan mode, incognito, attachments, image
chats, workspace or native-client turns, internal tool requests), and for an approval
that isn't one of the brain's. Otherwise it returns the brain's streaming response,
detached like upstream's (``src/agent_runs.py``), with the same headers.

Per turn (:func:`_drive`):

1. The endpoint must be local or on a private network (upstream's ``is_local_endpoint``,
   the fork's ``is_private_host``); otherwise the answer is :data:`LOCAL_ONLY`.
2. Each round streams from upstream's ``stream_llm`` (endpoint headers, auth,
   ``cache_prompt`` and ``session_id`` slot affinity stay upstream's), with thinking off:
   ``enable_thinking: false`` is sent to local endpoints whatever the model's name
   (:func:`apply_brain_thinking`, called by a hook in upstream's
   ``_apply_local_qwen_thinking_mode``). Text deltas go to the chat window as they
   arrive.
3. Tool calls (native ``tool_calls``, or the text formats upstream's
   ``src/tool_parsing.py`` reads) run through ``fork/brain/tools.py``; their results are
   appended and the next round starts. After ``FORK_BRAIN_MAX_ROUNDS`` rounds one last
   round runs without tools.
4. A call that must ask pauses the turn: an exact approval is filed in upstream's
   store, the card is sent (``ask_user``, ``kind: tool_approval``) and the loop's state
   is kept in memory under the approval id (:data:`PENDING_SECONDS`, one per chat). The
   owner's answer comes back as a new request; :func:`brain_response` resumes the state
   and runs the approved call without asking the model again. Unanswered in time, the
   turn ends with :data:`NOT_APPROVED`.
5. The answer is saved as a normal Odysseus message (``save_assistant_response``), the
   turn's tool exchange in ``history.py``, and upstream's after-turn jobs run
   (``run_post_response_tasks``: title, memory extraction, token totals, webhooks).

Events are the ones ``static/js/chat.js`` renders: ``{"delta"}``, ``agent_step``,
``tool_start``, ``tool_output`` (``execution_attempted``, ``blocked``), ``ask_user``,
``final_response``, ``metrics``, ``message_saved``, ``[DONE]``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, AsyncGenerator

from fork.brain import history as brain_history
from fork.brain import prompt as brain_prompt
from fork.brain import settings as brain_settings
from fork.brain import tools as brain_tools
from fork.brain import toolset

logger = logging.getLogger(__name__)

PENDING_SECONDS = 10 * 60
LOCAL_ONLY = "The assistant runs only on local models."
NOT_APPROVED = "The change wasn't approved in time."
CONTEXT_BUDGET = 0.75  # share of the model's context a prompt may fill
KEEP_RECENT_TURNS = 2  # user turns never summarised
FINAL_ROUND_NOTE = (
    "[Context: the tool budget for this turn is used up; not an instruction from the "
    "owner] Answer now, as well as you can, from what the tools returned.")
SUMMARY_PROMPT = (
    "Summarise this earlier part of a chat between the owner and their assistant in at "
    "most 200 words: the questions, the answers, and facts, names, links and decisions "
    "worth keeping for later questions. Write only the summary.")

_THINKING_OFF: ContextVar[bool] = ContextVar("fork_brain_thinking_off", default=False)


# ---------------------------------------------------------------------- the hook
def _sse(payload: Any) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _attachments(value: Any) -> bool:
    if not value:
        return False
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return bool(value.strip())
    return bool(value)


def takes_turn(*, message: Any, upstream_turn: bool, attachments: Any = None,
               request: Any = None) -> bool:
    """True when the brain answers this (non-approval) turn."""
    if upstream_turn or _attachments(attachments):
        return False
    if not isinstance(message, str) or not message.strip():
        return False
    if request is not None:
        from src.agent_runtime.authority import is_internal_tool_request
        try:
            if is_internal_tool_request(request):
                return False
        except Exception:  # noqa: BLE001 -- a bare test request
            pass
    return True


async def brain_response(request: Any, *, sess: Any, session_id: str, message: Any,
                         owner: Any, approval_id: Any = None, exact_approval: Any = None,
                         preset_id: Any = None, upstream_turn: bool = False,
                         attachments: Any = None, services: tuple = ()) -> Any:
    """The brain's streaming response for this turn, or None for upstream's paths.

    ``approval_id``: set when this request continues a consumed tool approval;
    ``exact_approval`` is then upstream's consumed grant. ``upstream_turn``: the route
    already knows the turn is upstream's (research, compare, plan mode, incognito, a
    workspace or native client, an image chat). ``services``: the route's
    ``(session_manager, chat_handler, memory_manager, memory_vector, webhook_manager,
    skills_manager)``.
    """
    if not brain_settings.enabled():
        return None
    deps = _Deps(*services) if services else _Deps()
    if approval_id:
        # Taken before expire_pending(): upstream has consumed this approval already.
        paused = take_pending(approval_id, owner=owner, session_id=session_id)
        expire_pending()
        if paused is None or exact_approval is None:
            return None  # not the brain's approval: upstream resumes its own
        paused.sess = sess
        paused.deps = deps if services else paused.deps
        return _response(session_id, _resume(paused, exact_approval))
    expire_pending()
    if not takes_turn(message=message, upstream_turn=upstream_turn,
                      attachments=attachments, request=request):
        return None
    drop_pending_for_chat(owner=owner, session_id=session_id)
    turn = _start_turn(request, sess=sess, session_id=session_id, message=message,
                       owner=owner, preset_id=preset_id, deps=deps)
    return _response(session_id, _run_new_turn(turn))


def _response(session_id: str, stream: AsyncGenerator[str, None]) -> Any:
    """Detached like upstream's chat turns: it survives a closed tab and
    ``/api/chat/resume`` reconnects to it. Same media type and headers."""
    from fastapi.responses import StreamingResponse

    from src import agent_runs

    run = agent_runs.start(session_id, stream)
    return StreamingResponse(
        agent_runs.subscribe(session_id, run),
        media_type="text/event-stream",
        headers={
            "X-Odysseus-Run-Id": run.run_id,
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


# --------------------------------------------------------------------- the state
@dataclass
class _Deps:
    session_manager: Any = None
    chat_handler: Any = None
    memory_manager: Any = None
    memory_vector: Any = None
    webhook_manager: Any = None
    skills_manager: Any = None


@dataclass
class _Turn:
    """Everything one turn needs; kept whole while it waits for an approval."""
    session_id: str
    owner: Any
    message: str
    sess: Any
    deps: _Deps
    endpoint_url: str
    model: str
    headers: dict
    temperature: float | None
    max_tokens: int | None
    character_name: str | None
    tools: tuple[dict, ...]
    run: Any
    messages: list[dict]
    turn_start: int  # index of the context message: what is before it is history
    history_ids: list[str] = field(default_factory=list)
    exchange: list[dict] = field(default_factory=list)
    queue: list[dict] = field(default_factory=list)  # calls of the round still to run
    round: int = 0
    rounds_run: int = 0
    tool_calls: int = 0
    tool_events: list[dict] = field(default_factory=list)
    round_texts: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    first_token_at: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    approval_id: str = ""
    expires_at: float = 0.0
    paused_message_id: str | None = None
    timer: Any = None


_PENDING: dict[str, _Turn] = {}
_PENDING_LOCK = threading.Lock()


def _owner_key(owner: Any) -> str:
    return str(owner or "").strip().casefold()


def take_pending(approval_id: Any, *, owner: Any, session_id: Any) -> _Turn | None:
    """Pop the paused turn waiting on ``approval_id`` for this owner and chat."""
    with _PENDING_LOCK:
        turn = _PENDING.get(str(approval_id or ""))
        if turn is None or _owner_key(turn.owner) != _owner_key(owner) \
                or turn.session_id != str(session_id):
            return None
        _PENDING.pop(turn.approval_id, None)
    if turn.timer is not None:
        turn.timer.cancel()
    return turn


def drop_pending_for_chat(*, owner: Any, session_id: Any) -> None:
    """A new message in a chat drops the turn that waited there (one per chat)."""
    with _PENDING_LOCK:
        gone = [k for k, t in _PENDING.items()
                if t.session_id == str(session_id) and _owner_key(t.owner) == _owner_key(owner)]
        turns = [_PENDING.pop(k) for k in gone]
    for t in turns:
        if t.timer is not None:
            t.timer.cancel()


def _park(turn: _Turn, approval_expires_at: float | None = None) -> None:
    """Keep the paused turn until a little before upstream's approval record expires
    (:data:`PENDING_SECONDS`), so the time-out below can still retire that record."""
    drop_pending_for_chat(owner=turn.owner, session_id=turn.session_id)
    until = time.time() + PENDING_SECONDS
    if approval_expires_at:
        until = min(until, approval_expires_at - 1.0)
    turn.expires_at = until
    with _PENDING_LOCK:
        _PENDING[turn.approval_id] = turn
    try:
        loop = asyncio.get_running_loop()
        turn.timer = loop.call_later(max(0.0, until - time.time()), expire_pending)
    except RuntimeError:
        turn.timer = None


def expire_pending(now: float | None = None) -> int:
    """Drop the paused turns that can't resume any more. One whose approval record is
    gone from upstream's store (the owner chose Deny, or a new message retired it) is
    dropped quietly. One whose time is up has its record retired and
    :data:`NOT_APPROVED` saved as its answer. Runs on a timer and whenever a brain turn
    starts; returns how many turns timed out."""
    from src.tool_approvals import tool_approval_store

    now = time.time() if now is None else now
    timed_out: list[_Turn] = []
    with _PENDING_LOCK:
        for key, t in list(_PENDING.items()):
            if tool_approval_store.peek(key) is None:
                _PENDING.pop(key, None)
            elif t.expires_at <= now:
                _PENDING.pop(key, None)
                timed_out.append(t)
    for t in timed_out:
        if t.timer is not None:
            t.timer.cancel()
        tool_approval_store.consume(t.approval_id, decision="deny", owner=t.owner,
                                    session_id=t.session_id)
        try:
            _save_answer(t, NOT_APPROVED, post_tasks=False)
        except Exception:  # noqa: BLE001
            logger.warning("brain: could not save the timed-out turn", exc_info=True)
    return len(timed_out)


def pending_count() -> int:
    with _PENDING_LOCK:
        return len(_PENDING)


# ---------------------------------------------------------------- starting a turn
def _preset(chat_handler: Any, preset_id: Any) -> SimpleNamespace:
    empty = SimpleNamespace(temperature=None, max_tokens=None, system_prompt="",
                            character_name=None)
    if chat_handler is None or not hasattr(chat_handler, "validate_and_extract_preset"):
        return empty
    try:
        from routes.chat_helpers import extract_preset
        return extract_preset(chat_handler, preset_id)
    except Exception:  # noqa: BLE001 -- an unknown preset: no preset text
        logger.debug("brain: preset unavailable", exc_info=True)
        return empty


def _privileges(request: Any, owner: Any) -> dict:
    try:
        auth_manager = request.app.state.auth_manager
    except AttributeError:
        return {}
    if not owner or auth_manager is None:
        return {}
    try:
        return auth_manager.get_privileges(owner) or {}
    except Exception:  # noqa: BLE001
        return {}


def _delegated(request: Any) -> bool:
    try:
        from src.auth_helpers import is_delegated_credential
        return bool(is_delegated_credential(request))
    except Exception:  # noqa: BLE001
        return False


def _save_user_message(turn_sess: Any, deps: _Deps, request: Any, session_id: str,
                       message: str) -> None:
    """The owner's message, saved the way upstream's chat turns save it."""
    from routes.chat_helpers import add_user_message, fire_message_event

    pre = SimpleNamespace(attachment_meta=None, user_content=message,
                          text_for_context=message)
    handler = deps.chat_handler
    if handler is not None and hasattr(handler, "update_session_name_if_needed"):
        add_user_message(turn_sess, handler, pre, interaction_mode="agent")
    else:
        from core.models import ChatMessage
        turn_sess.add_message(ChatMessage("user", message, metadata={"interaction_mode": "agent"}))
    try:
        fire_message_event(request, deps.webhook_manager, session_id, turn_sess, message)
    except Exception:  # noqa: BLE001
        logger.debug("brain: message event failed", exc_info=True)


def _start_turn(request: Any, *, sess: Any, session_id: str, message: str, owner: Any,
                preset_id: Any, deps: _Deps) -> _Turn:
    brain_history.sweep()
    preset = _preset(deps.chat_handler, preset_id)
    rows = list(getattr(sess, "history", None) or ())
    _save_user_message(sess, deps, request, session_id, message)

    delegated = _delegated(request)
    disabled = brain_tools.turn_disabled_tools(
        request, owner, privileges=_privileges(request, owner), delegated=delegated)
    schemas = toolset.for_chat(session_id, owner=owner, disabled=disabled)
    schemas = toolset.with_skill_tools(schemas, message, owner=owner, disabled=disabled)

    keep_ids = [brain_prompt.message_id(m) for m in rows if brain_prompt.message_id(m)]
    try:
        exchanges = brain_history.turns(session_id, keep_ids=keep_ids)
        summary = brain_history.summary(session_id)
    except Exception:  # noqa: BLE001 -- follow-ups lose tool results, the turn still runs
        logger.warning("brain: history unavailable", exc_info=True)
        exchanges, summary = {}, None
    hist = brain_prompt.history_messages(rows, exchanges, summary=summary)
    recalled = brain_prompt.recalled_items(owner, message, deps.memory_manager)
    messages = brain_prompt.build_messages(preset_text=preset.system_prompt, history=hist,
                                           recalled=recalled, message=message)
    run = brain_tools.build_run(
        request_text=message, owner=owner, session_id=session_id, history=rows,
        offered=tuple(toolset.names(schemas)), disabled=disabled, delegated=delegated,
        prompt_messages=messages)
    if run.policy.block_all_tool_calls:
        schemas = ()
    temperature = getattr(sess, "temperature_override", None)
    if temperature is None:
        temperature = preset.temperature
    max_tokens = getattr(sess, "max_tokens_override", None) or preset.max_tokens
    return _Turn(
        session_id=str(session_id), owner=owner, message=message, sess=sess, deps=deps,
        endpoint_url=str(getattr(sess, "endpoint_url", "") or ""),
        model=str(getattr(sess, "model", "") or ""),
        headers=dict(getattr(sess, "headers", None) or {}),
        temperature=temperature, max_tokens=max_tokens,
        character_name=preset.character_name, tools=tuple(schemas), run=run,
        messages=messages, turn_start=len(messages) - 2, history_ids=keep_ids)


# --------------------------------------------------------------------- the model
def apply_brain_thinking(payload: dict) -> bool:
    """The hook in ``src/llm_core.py`` (``_apply_local_qwen_thinking_mode``), which runs
    for local and private endpoints only: during a brain request it sets
    ``chat_template_kwargs.enable_thinking = False`` whatever the model's name and returns
    True; for every other request it returns False and leaves ``payload`` untouched."""
    if not _THINKING_OFF.get():
        return False
    kwargs = payload.get("chat_template_kwargs")
    if not isinstance(kwargs, dict):
        kwargs = {}
        payload["chat_template_kwargs"] = kwargs
    kwargs["enable_thinking"] = False
    return True


def is_local(url: str) -> bool:
    """A local or private-network endpoint (upstream's check, then the fork's)."""
    from src.model_context import is_local_endpoint

    from fork.hub.private_endpoint_thinking import is_private_host

    try:
        return bool(is_local_endpoint(url) or is_private_host(url))
    except Exception:  # noqa: BLE001
        return False


async def _stream_round(turn: _Turn, messages: list[dict], tools: tuple[dict, ...]):
    """One model round: yields ``("text", str)``, ``("calls", list)``, ``("usage", dict)``
    and ``("error", sse chunk)``."""
    from src import llm_core

    kwargs: dict[str, Any] = {"headers": turn.headers, "session_id": turn.session_id,
                              "thinking_mode": "off", "workload": "foreground"}
    if turn.temperature is not None:
        kwargs["temperature"] = turn.temperature
    if turn.max_tokens:
        kwargs["max_tokens"] = turn.max_tokens
    if tools:
        kwargs["tools"] = list(tools)
    else:
        kwargs["tool_choice_none"] = True
    token = _THINKING_OFF.set(True)
    try:
        async for chunk in llm_core.stream_llm(turn.endpoint_url, turn.model, messages, **kwargs):
            if chunk.startswith("event: error"):
                yield "error", chunk
                return
            if not chunk.startswith("data: "):
                continue
            body = chunk[6:].strip()
            if body == "[DONE]":
                return
            try:
                data = json.loads(body)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("type") == "tool_calls":
                yield "calls", list(data.get("calls") or [])
            elif data.get("type") == "usage":
                yield "usage", data.get("data") or {}
            elif isinstance(data.get("delta"), str) and not data.get("thinking"):
                yield "text", data["delta"]
    finally:
        _THINKING_OFF.reset(token)


def _text_calls(text: str, offered: list[str]) -> tuple[list[dict], str]:
    """Tool calls the model wrote as text (Qwen's and the other formats upstream's
    ``src/tool_parsing.py`` reads), and the text without them."""
    from src.tool_parsing import parse_tool_blocks, strip_tool_blocks

    try:
        blocks = parse_tool_blocks(text, skip_fenced=True, additional_tool_names=offered)
    except Exception:  # noqa: BLE001
        return [], text
    calls = [{"id": f"call_{uuid.uuid4().hex[:12]}", "name": b.tool_type,
              "arguments": b.content} for b in blocks if b.tool_type in offered]
    if not calls:
        return [], text
    try:
        text = strip_tool_blocks(text)
    except Exception:  # noqa: BLE001
        pass
    return calls, text


# ---------------------------------------------------------------- context budget
def _estimate(messages: list[dict], tools: tuple[dict, ...] = ()) -> int:
    from src.model_context import estimate_tokens

    return estimate_tokens(messages) + len(json.dumps(list(tools))) // 4


async def _context_window(turn: _Turn) -> int:
    try:
        from src.agent_runtime.context_resolution import resolve_effective_context
        res = await resolve_effective_context(turn.endpoint_url, turn.model,
                                              headers=turn.headers)
        if getattr(res, "effective", 0):
            return int(res.effective)
    except Exception:  # noqa: BLE001
        logger.debug("brain: context resolution failed", exc_info=True)
    from src.model_context import get_context_length
    return int(get_context_length(turn.endpoint_url, turn.model) or 0)


DROPPED_RESULT = "[older tool result removed to save space]"


def drop_old_tool_results(messages: list[dict], budget: int, tools=(), *,
                          turn_start: int | None = None, keep_last: int = 1) -> bool:
    """Replace the oldest tool results with a short note until the prompt fits
    ``budget`` tokens. Earlier turns' results go first (all of them may go), then this
    turn's, whose ``keep_last`` newest stay. True when it fits."""
    start = len(messages) if turn_start is None else turn_start
    live = [i for i, m in enumerate(messages)
            if m.get("role") == "tool" and m.get("content") != DROPPED_RESULT]
    older = [i for i in live if i < start]
    current = [i for i in live if i >= start]
    if keep_last:
        current = current[:-keep_last] if len(current) > keep_last else []
    indexes = older + current
    for i in indexes:
        if _estimate(messages, tools) <= budget:
            return True
        messages[i] = {**messages[i], "content": DROPPED_RESULT}
    return _estimate(messages, tools) <= budget


async def _complete(turn: _Turn, messages: list[dict]) -> str:
    parts = []
    async for kind, value in _stream_round(turn, messages, ()):
        if kind == "text":
            parts.append(value)
    from src.tool_parsing import strip_tool_blocks
    return strip_tool_blocks("".join(parts)).strip()


async def _summarise_older(turn: _Turn, budget: int) -> bool:
    """Summarise the chat's older turns once with the local model; the summary replaces
    them in this prompt and in the next turns'. True when the prompt fits after it."""
    rows = list(getattr(turn.sess, "history", None) or ())[:-1]  # without the new message
    users = [i for i, m in enumerate(rows) if brain_prompt._role(m) == "user"]
    if len(users) <= KEEP_RECENT_TURNS:
        return False
    cut = users[-KEEP_RECENT_TURNS]
    upto = next((brain_prompt.message_id(m) for m in reversed(rows[:cut])
                 if brain_prompt.message_id(m)), "")
    if not upto:
        return False
    older = brain_prompt.history_messages(rows[:cut])
    text = "\n".join(f"{m['role']}: {m['content']}" for m in older if m.get("content"))
    summary = await _complete(turn, [
        {"role": "system", "content": SUMMARY_PROMPT},
        {"role": "user", "content": brain_tools.trim(text, 24000)}])
    if not summary:
        return False
    brain_history.save_summary(turn.session_id, upto, summary)
    exchanges = brain_history.turns(turn.session_id)
    hist = brain_prompt.history_messages(rows, exchanges, summary=(upto, summary))
    tail = turn.messages[turn.turn_start:]
    turn.messages = [turn.messages[0], *hist, *tail]
    turn.turn_start = 1 + len(hist)
    return _estimate(turn.messages, turn.tools) <= budget


async def _fit_context(turn: _Turn) -> None:
    window = await _context_window(turn)
    if window <= 0:
        return
    budget = int(window * CONTEXT_BUDGET)
    if _estimate(turn.messages, turn.tools) <= budget:
        return
    if drop_old_tool_results(turn.messages, budget, turn.tools, turn_start=turn.turn_start):
        return
    try:
        await _summarise_older(turn, budget)
    except Exception:  # noqa: BLE001
        logger.warning("brain: summarising older turns failed", exc_info=True)


# ----------------------------------------------------------------------- events
def _display(name: str, content: str) -> str:
    try:
        args = json.loads(content or "{}")
    except ValueError:
        return str(content or "")[:240]
    if isinstance(args, dict):
        for key in ("query", "url", "location", "name", "action"):
            if args.get(key):
                return str(args[key])[:240]
    return str(content or "")[:240]


_FORWARD_KEYS = ("memory_id", "note_id", "note_title", "task_id", "doc_id", "document_id",
                 "uid", "anchor", "events", "image_url", "ui_event")


def _tool_output_event(name: str, content: str, call_id: str, result: dict, *,
                       attempted: bool, window_text: str) -> dict:
    event = {"type": "tool_output", "tool": name, "command": _display(name, content),
             "output": window_text, "exit_code": result.get("exit_code"),
             "execution_attempted": attempted, "blocked": bool(result.get("blocked", False))}
    if call_id:
        event.update({"call_id": call_id, "tool_call_id": call_id})
    for key in _FORWARD_KEYS:
        if result.get(key) is not None:
            event[key] = result[key]
    return event


def _metrics(turn: _Turn, **extra) -> dict:
    now = time.time()
    data = {
        "model": turn.model, "requested_model": turn.model,
        "input_tokens": turn.input_tokens, "output_tokens": turn.output_tokens,
        "total_time": round(now - turn.started, 2),
        "response_time": round(now - turn.started, 2),
        "time_to_first_token": (round(turn.first_token_at - turn.started, 2)
                                if turn.first_token_at else None),
        "agent_rounds": turn.rounds_run, "tool_calls": turn.tool_calls,
        "round_texts": list(turn.round_texts), "brain": True,
    }
    if turn.tool_events:
        data["tool_events"] = list(turn.tool_events)
    data.update(extra)
    return data


def _save_answer(turn: _Turn, text: str, *, post_tasks: bool = True,
                 extra_metadata: dict | None = None) -> str | None:
    """Save ``text`` as the turn's answer (a normal assistant message), its tool
    exchange in ``history.py``, then upstream's after-turn jobs. Returns the message id."""
    from routes.chat_helpers import run_post_response_tasks, save_assistant_response

    metrics = _metrics(turn, **{brain_prompt.TURN_FLAG: True, **(extra_metadata or {})})
    deps = turn.deps
    message_id = save_assistant_response(
        turn.sess, deps.session_manager, turn.session_id, text, metrics,
        character_name=turn.character_name)
    if message_id and turn.exchange and not (extra_metadata or {}).get(brain_prompt.PAUSED_FLAG):
        try:
            brain_history.save_turn(turn.session_id, str(message_id), turn.exchange,
                                    owner=turn.owner)
        except Exception:  # noqa: BLE001
            logger.warning("brain: could not keep the turn's tool results", exc_info=True)
    if post_tasks:
        try:
            from routes.chat_helpers import load_prefs_for_user
            uprefs = load_prefs_for_user(turn.owner) or {}
        except Exception:  # noqa: BLE001
            uprefs = {}
        try:
            run_post_response_tasks(
                turn.sess, deps.session_manager, turn.session_id, turn.message, text, metrics,
                uprefs, deps.memory_manager, deps.memory_vector, deps.webhook_manager,
                character_name=turn.character_name, agent_rounds=turn.rounds_run,
                agent_tool_calls=turn.tool_calls, skills_manager=deps.skills_manager,
                owner=turn.owner, extract_skills=False)
        except Exception:  # noqa: BLE001
            logger.warning("brain: after-turn jobs failed", exc_info=True)
    return str(message_id) if message_id else None


# ----------------------------------------------------------------------- the loop
async def _run_new_turn(turn: _Turn) -> AsyncGenerator[str, None]:
    async for chunk in _guarded(turn, _drive(turn)):
        yield chunk


async def _resume(turn: _Turn, exact_approval: Any) -> AsyncGenerator[str, None]:
    async def go():
        if getattr(exact_approval, "allow_remaining_actions", False):
            # The card's "allow" lifts upstream's approval gate for the rest of this
            # task, as in upstream's agent; every write of the brain still asks.
            turn.run.security.approval_gate_bypassed = True
        turn.started = time.time()
        call = turn.queue.pop(0)
        async for chunk in _run_call(turn, call, exact_approval=exact_approval):
            yield chunk
        async for chunk in _drive(turn):
            yield chunk
    async for chunk in _guarded(turn, go()):
        yield chunk


async def _guarded(turn: _Turn, inner: AsyncGenerator[str, None]) -> AsyncGenerator[str, None]:
    """Marks the chat as streaming (upstream's background jobs wait for it) while
    ``inner`` runs, and always ends the stream with ``[DONE]``."""
    from fork.upstream import chat_active_streams

    streams = chat_active_streams()
    streams[turn.session_id] = {"status": "streaming", "partial": "", "query": turn.message,
                                "is_research": False, "mode": "agent"}
    try:
        async for chunk in inner:
            yield chunk
    except Exception:  # noqa: BLE001
        logger.exception("brain: turn failed")
        yield f'event: error\ndata: {json.dumps({"error": "The assistant failed on this turn.", "status": 500})}\n\n'
    finally:
        streams.pop(turn.session_id, None)
    yield "data: [DONE]\n\n"


async def _drive(turn: _Turn) -> AsyncGenerator[str, None]:
    """Run the queued calls, then rounds until an answer, a pause or an error."""
    if not is_local(turn.endpoint_url):
        turn.round_texts.append(LOCAL_ONLY)
        yield _sse({"type": "final_response", "content": LOCAL_ONLY})
        yield _sse({"type": "metrics", "data": _metrics(turn)})
        saved = _save_answer(turn, LOCAL_ONLY)
        if saved:
            yield _sse({"type": "message_saved", "id": saved})
        return
    max_rounds = brain_settings.max_rounds()
    while True:
        while turn.queue:
            call = turn.queue[0]
            reason = brain_tools.needs_approval(turn.run, call["name"], call["arguments"])
            if reason:
                async for chunk in _pause(turn, call, reason):
                    yield chunk
                return
            turn.queue.pop(0)
            async for chunk in _run_call(turn, call):
                yield chunk
        last = turn.round >= max_rounds
        tools = () if last else turn.tools
        if last and turn.round > 0 and turn.messages[-1].get("content") != FINAL_ROUND_NOTE:
            turn.messages.append({"role": "user", "content": FINAL_ROUND_NOTE})
        await _fit_context(turn)
        if turn.round > 0:
            yield _sse({"type": "agent_step", "round": turn.round + 1})
        text_parts: list[str] = []
        calls: list[dict] = []
        async for kind, value in _stream_round(turn, turn.messages, tools):
            if kind == "text":
                if turn.first_token_at is None and value.strip():
                    turn.first_token_at = time.time()
                text_parts.append(value)
                yield _sse({"delta": value})
            elif kind == "calls":
                calls = value
            elif kind == "usage":
                turn.input_tokens += int(value.get("input_tokens") or 0)
                turn.output_tokens += int(value.get("output_tokens") or 0)
            elif kind == "error":
                yield value
                partial = "".join(text_parts).strip()
                if partial:
                    saved = _save_answer(turn, partial, extra_metadata={"stopped": True})
                    if saved:
                        yield _sse({"type": "message_saved", "id": saved})
                return
        turn.round += 1
        turn.rounds_run += 1
        text = "".join(text_parts)
        offered = toolset.names(tools)
        calls = [c for c in calls if c.get("name")]
        if not calls and tools:
            calls, text = _text_calls(text, offered)
        turn.round_texts.append(text.strip())
        if not calls:
            async for chunk in _finish(turn, text):
                yield chunk
            return
        for c in calls:
            c["id"] = c.get("id") or f"call_{uuid.uuid4().hex[:12]}"
            c["arguments"] = c.get("arguments") or "{}"
        assistant = {"role": "assistant", "content": text.strip(), "tool_calls": [
            {"id": c["id"], "type": "function",
             "function": {"name": c["name"], "arguments": c["arguments"]}} for c in calls]}
        turn.messages.append(assistant)
        turn.exchange.append(assistant)
        turn.queue = [{"id": c["id"], "name": c["name"], "arguments": c["arguments"]}
                      for c in calls]


async def _run_call(turn: _Turn, call: dict, *, exact_approval: Any = None):
    name, content, call_id = call["name"], call["arguments"], call["id"]
    yield _sse({"type": "tool_start", "tool": name, "command": _display(name, content),
                "full_command": content, "round": turn.round, "call_id": call_id,
                "tool_call_id": call_id})
    ran, desc, result = await brain_tools.execute(turn.run, name, content,
                                                  exact_approval=exact_approval)
    attempted = not (result.get("blocked") or "BLOCKED" in desc or "NOT RUN" in desc)
    if attempted:
        turn.tool_calls += 1
    full = brain_tools.result_text(result)
    window = brain_tools.trim(full, brain_tools.WINDOW_RESULT_CHARS)
    event = _tool_output_event(name, ran, call_id, result, attempted=attempted, window_text=window)
    if exact_approval is not None:
        event["approved"] = True
    yield _sse(event)
    turn.tool_events.append({"round": turn.round, "tool": name, "desc": desc,
                             "command": _display(name, ran), "output": window[:4000],
                             "exit_code": result.get("exit_code")})
    tool_message = {"role": "tool", "tool_call_id": call_id, "name": name,
                    "content": brain_tools.trim(full)}
    turn.messages.append(tool_message)
    turn.exchange.append(tool_message)


async def _pause(turn: _Turn, call: dict, reason: str):
    """File the exact approval, send the card, save the waiting message, keep the state."""
    name, content, call_id = call["name"], call["arguments"], call["id"]
    pending = brain_tools.create_approval(turn.run, name, content,
                                          continuation_query=turn.message)
    turn.approval_id = pending.approval_id
    payload = pending.public_payload(reason=reason)
    result = {"output": "Waiting for an exact user approval.", "exit_code": None,
              "approval_required": True}
    event = _tool_output_event(name, content, call_id, result, attempted=False,
                               window_text=result["output"])
    event["ask_user"] = payload
    yield _sse(event)
    question = f"I need your approval to run {name}."
    if not "".join(turn.round_texts).strip():
        yield _sse({"type": "final_response", "content": question})
    yield _sse({"type": "ask_user", "data": payload})
    turn.tool_events.append({"round": turn.round, "tool": name,
                             "desc": f"{name}: APPROVAL REQUIRED",
                             "command": _display(name, content),
                             "output": result["output"], "exit_code": None,
                             "ask_user": payload})
    yield _sse({"type": "metrics", "data": _metrics(turn)})
    saved = _save_answer(turn, (turn.round_texts[-1] if turn.round_texts else "") or question,
                         post_tasks=False, extra_metadata={brain_prompt.PAUSED_FLAG: True})
    turn.paused_message_id = saved
    if saved:
        yield _sse({"type": "message_saved", "id": saved})
    turn.tool_events = [e for e in turn.tool_events if "ask_user" not in e]
    _park(turn, getattr(pending, "expires_at", None))


async def _finish(turn: _Turn, text: str):
    answer = text.strip()
    if not answer:
        answer = "Done." if turn.tool_calls else "(no answer)"
        yield _sse({"type": "final_response", "content": answer})
    yield _sse({"type": "metrics", "data": _metrics(turn)})
    saved = _save_answer(turn, answer)
    if saved:
        yield _sse({"type": "message_saved", "id": saved})
