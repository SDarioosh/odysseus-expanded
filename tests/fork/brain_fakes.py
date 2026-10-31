"""Fakes for the brain's tests (``tests/fork/test_brain_*.py``). Fictional data only.

``Brain`` sets up one test: a fake chat session and request, the fake model (``llm``:
scripted rounds, every request recorded), a recorder in place of upstream's
``execute_tool_block`` (``ran``), a fake Athena MCP server named ``convo`` with read and
write tools, the brain's DB in the test's ``DATA_DIR`` and ``FORK_BRAIN`` on.
"""
from __future__ import annotations

import copy
import json
import time
from types import SimpleNamespace

from tests.fork.fakes import FakeMcpManager

ATHENA = "convo"
ATHENA_TOOLS = [
    {"name": "search_transcripts", "description": "Search transcripts.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
     "annotations": {"readOnlyHint": True}},
    {"name": "get_conversation", "description": "One conversation.",
     "input_schema": {"type": "object", "properties": {"id": {"type": "integer"}}},
     "annotations": {"readOnlyHint": True}},
    {"name": "update_action_item", "description": "Change an action item.",
     "input_schema": {"type": "object", "properties": {"id": {"type": "integer"}}},
     "annotations": {"readOnlyHint": False, "destructiveHint": False}},
    {"name": "erase_fact", "description": "Forget a fact.",
     "input_schema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": False, "destructiveHint": True}},
]


class SchemaMcpManager(FakeMcpManager):
    def get_all_openai_schemas(self, disabled_map=None):
        out = []
        for sid, tools in self._tools.items():
            off = (disabled_map or {}).get(sid, set())
            for t in tools:
                if t["name"] in off:
                    continue
                out.append({"type": "function", "function": {
                    "name": f"mcp__{sid}__{t['name']}", "description": t["description"],
                    "parameters": t["input_schema"]}})
        return out


def athena_manager() -> SchemaMcpManager:
    return SchemaMcpManager({ATHENA: copy.deepcopy(ATHENA_TOOLS)},
                            {ATHENA: {"status": "connected", "name": ATHENA}})


def text(t: str) -> str:
    return f"data: {json.dumps({'delta': t})}\n\n"


def calls(*items) -> str:
    """``calls(("web_search", {"query": "x"}), ...)`` as stream_llm's tool_calls event."""
    out = [{"id": f"call_{i}", "name": n, "arguments": json.dumps(a)}
           for i, (n, a) in enumerate(items)]
    return f"data: {json.dumps({'type': 'tool_calls', 'calls': out})}\n\n"


DONE = "data: [DONE]\n\n"


class FakeLLM:
    """``stream_llm`` replaced: each call answers the next scripted round."""

    def __init__(self):
        self.script: list[list[str]] = []
        self.requests: list[dict] = []

    def rounds(self, *rounds):
        self.script.extend(list(r) + [DONE] for r in rounds)

    async def __call__(self, url, model, messages, **kw):
        self.requests.append({"url": url, "model": model,
                              "messages": copy.deepcopy(messages), **kw})
        assert self.script, "the model was called more often than scripted"
        for chunk in self.script.pop(0):
            yield chunk


class FakeSession:
    def __init__(self, sid="chat-1", owner="alice", url="http://127.0.0.1:8082/v1"):
        self.id = sid
        self.owner = owner
        self.endpoint_url = url
        self.model = "local-model"
        self.headers = {}
        self.history = []
        self.name = "test"
        self.temperature_override = None
        self.max_tokens_override = None
        self._n = 0

    def add_message(self, message):
        self._n += 1
        message.metadata = dict(message.metadata or {})
        message.metadata["_db_id"] = f"{self.id}-m{self._n}"
        self.history.append(message)


def fake_request(user="alice"):
    return SimpleNamespace(headers={}, app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)),
                           state=SimpleNamespace(current_user=user), cookies={})


class Brain:
    def __init__(self, monkeypatch, data_dir, *, admins=("alice",), manager=None):
        from fork.brain import history as brain_history
        from fork.brain import toolset, turn

        self.mp = monkeypatch
        self.data_dir = data_dir
        self.llm = FakeLLM()
        self.ran: list[dict] = []
        self.results: dict[str, dict] = {}
        self.saved: list[tuple] = []
        self.post: list[tuple] = []
        self.manager = manager if manager is not None else athena_manager()
        monkeypatch.setenv("FORK_BRAIN", "on")
        monkeypatch.setenv("ODYSSEUS_TRUSTED_READONLY_MCP", ATHENA)
        import src.llm_core as llm_core

        self.real_stream_llm = llm_core.stream_llm  # upstream's streaming client itself
        monkeypatch.setattr("src.llm_core.stream_llm", self.llm)
        import src.tool_execution as tool_execution

        self.real_execute = tool_execution.execute_tool_block  # upstream's runner itself
        monkeypatch.setattr("src.tool_execution.execute_tool_block", self._execute)
        monkeypatch.setattr("src.tool_execution.get_mcp_manager", lambda: self.manager)
        monkeypatch.setattr("fork.upstream.load_mcp_disabled_map", lambda: {})
        monkeypatch.setattr("src.tool_security.owner_is_admin_or_single_user",
                            lambda owner: owner in admins)
        monkeypatch.setattr("fork.beliefs.search.search_beliefs", lambda *a, **k: [])
        monkeypatch.setattr("routes.chat_helpers.save_assistant_response", self._save)
        monkeypatch.setattr("routes.chat_helpers.run_post_response_tasks",
                            lambda *a, **k: self.post.append((a, k)))
        monkeypatch.setattr("routes.chat_helpers.fire_message_event", lambda *a, **k: None)
        monkeypatch.setattr(turn, "_context_window", self._window)
        self.window = 100_000
        toolset._CHAT_SETS.clear()
        turn._PENDING.clear()
        # the chat-delete sweep reads upstream's chat database: not in these tests
        monkeypatch.setattr(brain_history, "_last_sweep", time.monotonic())

    async def _window(self, _turn):
        return self.window

    async def _execute(self, block, **kw):
        self.ran.append({"tool": block.tool_type, "content": block.content, **kw})
        result = self.results.get(block.tool_type, {"output": f"result of {block.tool_type}",
                                                    "exit_code": 0})
        return f"{block.tool_type}: ok", dict(result)

    def _save(self, sess, sm, sid, text, md, **kw):
        from core.models import ChatMessage

        sess.add_message(ChatMessage("assistant", text, metadata=dict(md or {})))
        self.saved.append((sid, text, md))
        return sess.history[-1].metadata["_db_id"]

    def session(self, **kw) -> FakeSession:
        return FakeSession(**kw)

    def start(self, sess, message, *, owner=None, request=None, preset_id=None, deps=None):
        from fork.brain import turn

        return turn._start_turn(request or fake_request(owner or sess.owner), sess=sess,
                                session_id=sess.id, message=message,
                                owner=owner or sess.owner, preset_id=preset_id,
                                deps=deps or turn._Deps())

    async def run(self, sess, message, **kw) -> list[dict]:
        from fork.brain import turn

        t = self.start(sess, message, **kw)
        return [_event(c) async for c in turn._run_new_turn(t)]

    async def resume(self, approval_id, sess, decision="approve_task", owner=None) -> list[dict]:
        """Answer an approval card the way upstream's route consumes it, then resume."""
        from src.tool_approvals import tool_approval_store

        from fork.brain import turn

        owner = owner or sess.owner
        exact = tool_approval_store.consume(approval_id, decision=decision, owner=owner,
                                            session_id=sess.id)
        paused = turn.take_pending(approval_id, owner=owner, session_id=sess.id)
        assert paused is not None and exact is not None
        paused.sess = sess
        return [_event(c) async for c in turn._resume(paused, exact)]


def _event(chunk: str):
    if chunk.startswith("data: "):
        body = chunk[6:].strip()
        if body == "[DONE]":
            return {"type": "[DONE]"}
        return json.loads(body)
    return {"type": "raw", "chunk": chunk}


def of(events, kind):
    return [e for e in events if e.get("type") == kind]


def answer_text(events) -> str:
    return "".join(e["delta"] for e in events if isinstance(e.get("delta"), str))
