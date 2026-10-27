"""Fork: scripts/modules_setup.py against a fake admin API."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

_SPEC = importlib.util.spec_from_file_location(
    "modules_setup", Path(__file__).resolve().parents[1] / "scripts" / "modules_setup.py")
modules_setup = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(modules_setup)


PRESET_TEXT = ("These tools search the user's own recorded conversations.\n\n"
               "Ground every claim in tool results and include the link given with it.")


@pytest.fixture(autouse=True)
def _no_trusted_env(monkeypatch):
    monkeypatch.delenv(modules_setup.TRUSTED_ENV, raising=False)


class FakeApi:
    def __init__(self, servers=None, skills=None, preset_text=PRESET_TEXT):
        self.skills = skills if skills is not None else []
        self.preset_text = preset_text
        self.calls: list[tuple[str, str, object]] = []
        self.servers = servers if servers is not None else [
            {"name": "convo", "status": "connected", "is_enabled": True,
             "enabled_tool_count": 10},
        ]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        path = request.url.path
        if path == "/api/auth/login":
            return httpx.Response(200, json={"ok": True})
        if path == "/api/tools" and request.method == "GET":
            return httpx.Response(200, json={"tools": [
                {"id": "ask_user"}, {"id": "web_search"}, {"id": "manage_tasks"}]})
        if path == "/api/mcp/tools":
            return httpx.Response(200, json=[
                {"server_id": "email", "qualified_name": "mcp__email__send_email"},
                {"server_id": "convo", "qualified_name": "mcp__convo__get_transcript"},
            ])
        if path == "/api/mcp/servers":
            return httpx.Response(200, json=self.servers)
        if path == "/api/skills" and request.method == "GET":
            return httpx.Response(200, json={"skills": self.skills, "count": len(self.skills)})
        if path == "/api/presets":
            return httpx.Response(200, json={"custom": {"system_prompt": self.preset_text}})
        if path == "/api/default-chat":
            return httpx.Response(200, json={"endpoint_id": "ep1", "model": "qwen3.5-35b"})
        return httpx.Response(200, json={"ok": True})

    def writes(self):
        """Tool and model writes (skill writes: skill_writes)."""
        return [(m, p, b) for m, p, b in self.calls
                if m in {"POST", "PATCH"} and p != "/api/auth/login"
                and not p.startswith("/api/skills")]

    def skill_writes(self):
        return [(m, p, b) for m, p, b in self.calls
                if m in {"POST", "PUT"} and p.startswith("/api/skills")]

    def skill_deletes(self):
        return [p for m, p, _ in self.calls if m == "DELETE" and p.startswith("/api/skills")]


def _run(tmp_path, argv, api=None):
    api = api or FakeApi()
    pw = tmp_path / "pw.txt"
    pw.write_text("not-a-real-password\n")
    args = modules_setup.parse_args(["--password-file", str(pw), *argv])
    with httpx.Client(base_url="http://odysseus.test", transport=httpx.MockTransport(api)) as c:
        assert modules_setup.run(c, args) == 0
    return api


def test_default_keeps_every_tool_and_sets_auto_mode(tmp_path):
    api = _run(tmp_path, [])
    assert api.writes() == [
        ("PATCH", "/api/model-endpoints/ep1/models",
         {"model_tool_modes": {"qwen3.5-35b": ""}}),
    ]


def test_trim_defaults_to_regular_tools(tmp_path):
    api = _run(tmp_path, ["--trim"])
    assert api.writes()[-1] == (
        "PATCH", "/api/model-endpoints/ep1/models",
        {"model_tool_modes": {"qwen3.5-35b": "full"}})


def test_no_model_modes_leaves_mode_alone(tmp_path):
    api = _run(tmp_path, ["--no-model-modes"])
    assert api.writes() == []


def test_trim_turns_off_all_but_keep_list_and_builtin_mcp(tmp_path):
    api = _run(tmp_path, ["--trim", "--keep", "manage_tasks", "--model-mode", "unchanged"])
    assert api.writes() == [
        ("POST", "/api/tools",
         {"disabled": ["web_search", "mcp__email__send_email"]}),
    ]


def test_untrim_clears_disabled_list_and_sets_auto(tmp_path):
    api = _run(tmp_path, ["--untrim"])
    assert api.writes() == [
        ("POST", "/api/tools", {"disabled": []}),
        ("PATCH", "/api/model-endpoints/ep1/models",
         {"model_tool_modes": {"qwen3.5-35b": ""}}),
    ]


def test_dry_run_writes_nothing(tmp_path):
    api = _run(tmp_path, ["--trim", "--dry-run"])
    assert api.writes() == []


def test_missing_module_server_is_reported(tmp_path, capsys):
    _run(tmp_path, [], FakeApi(servers=[]))
    assert "module MCP server 'convo' is not registered" in capsys.readouterr().err


def test_keep_without_trim_is_rejected(tmp_path):
    with pytest.raises(SystemExit):
        modules_setup.parse_args(["--password-file", "x", "--keep", "web_search"])


def test_default_creates_athena_and_context_skills(tmp_path):
    api = _run(tmp_path, [])
    writes = api.skill_writes()
    assert [(m, p) for m, p, _ in writes] == [("POST", "/api/skills/add")] * 2
    athena, context = (b for _, _, b in writes)
    assert athena["name"] == "athena"
    assert athena["requires_toolsets"] == ["mcp:convo"]
    # The procedure is the Custom preset's text (the module's instructions), one step per paragraph.
    assert athena["procedure"] == PRESET_TEXT.split("\n\n")
    assert athena["source"] == "managed" and athena["status"] == "published"
    assert context["name"] == "context"
    assert context["requires_toolsets"] == ["mcp:convo"]
    assert any("link" in step for step in context["procedure"])


def test_context_skill_uses_trusted_env(tmp_path, monkeypatch):
    monkeypatch.setenv(modules_setup.TRUSTED_ENV, "convo, notes")
    api = _run(tmp_path, [])
    context = api.skill_writes()[-1][2]
    assert context["requires_toolsets"] == ["mcp:convo", "mcp:notes"]


def test_module_skill_text_file_and_names(tmp_path):
    text = tmp_path / "instr.txt"
    text.write_text("- search first\n- cite the link\n")
    api = _run(tmp_path, ["--module-skill", "notes=notes", "--skill-text", f"notes={text}",
                          "--context-skill", "all", "--trusted", "convo,notes"])
    notes, all_skill = (b for _, _, b in api.skill_writes())
    assert notes["name"] == "notes" and notes["requires_toolsets"] == ["mcp:notes"]
    assert notes["procedure"] == ["search first", "cite the link"]
    assert all_skill["name"] == "all"
    assert all_skill["requires_toolsets"] == ["mcp:convo", "mcp:notes"]
    assert not any(p == "/api/presets" for _, p, _ in api.calls)


def test_existing_managed_skill_is_updated(tmp_path):
    api = _run(tmp_path, [], FakeApi(skills=[{"name": "athena", "source": "managed"}]))
    assert [(m, p) for m, p, _ in api.skill_writes()] == [
        ("PUT", "/api/skills/athena"), ("POST", "/api/skills/add")]
    assert api.skill_writes()[0][2]["requires_toolsets"] == ["mcp:convo"]


def test_someone_elses_skill_is_left_alone(tmp_path, capsys):
    api = _run(tmp_path, [], FakeApi(skills=[{"name": "athena", "source": "user"}]))
    assert [b["name"] for _, _, b in api.skill_writes()] == ["context"]
    assert "skill 'athena' exists and isn't managed" in capsys.readouterr().err


def test_old_sound_skill_is_deleted(tmp_path, capsys):
    # /sound was renamed /athena: the old managed skill goes, so /sound stops working.
    api = _run(tmp_path, [], FakeApi(skills=[{"name": "sound", "source": "managed"}]))
    assert api.skill_deletes() == ["/api/skills/sound"]
    assert [b["name"] for _, _, b in api.skill_writes()] == ["athena", "context"]
    assert "skill /sound: delete (renamed)" in capsys.readouterr().out


def test_old_sound_skill_kept_when_not_managed_or_still_wanted(tmp_path, capsys):
    api = _run(tmp_path, [], FakeApi(skills=[{"name": "sound", "source": "user"}]))
    assert api.skill_deletes() == []
    api = _run(tmp_path, ["--module-skill", "sound=convo"],
               FakeApi(skills=[{"name": "sound", "source": "managed"}]))
    assert api.skill_deletes() == []
    assert [b["name"] for _, _, b in api.skill_writes()] == ["sound", "context"]
    api = _run(tmp_path, ["--dry-run"], FakeApi(skills=[{"name": "sound", "source": "managed"}]))
    assert api.skill_deletes() == []
    assert "skill /sound: delete (renamed)" in capsys.readouterr().out


def test_no_skills_and_dry_run_write_no_skills(tmp_path, capsys):
    assert _run(tmp_path, ["--no-skills"]).skill_writes() == []
    api = _run(tmp_path, ["--dry-run"])
    assert api.skill_writes() == []
    out = capsys.readouterr().out
    assert "skill /athena: create" in out and '"skills"' in out


def test_bad_module_skill_is_rejected():
    with pytest.raises(SystemExit):
        modules_setup.parse_args(["--password-file", "x", "--module-skill", "athena"])


def test_procedure_steps_splits_paragraphs_and_lists():
    assert modules_setup.procedure_steps("a\nb\n\n1. c\n2) d\n\n") == ["a b", "c", "d"]


def test_skill_toolsets_are_casefolded(tmp_path, monkeypatch):
    # Server keys match case-insensitively (ODYSSEUS_TRUSTED_READONLY_MCP, expand_toolsets),
    # but with_mcp_toolsets only offers "mcp:<name as registered>" and its casefold, so a
    # skill requiring "mcp:Convo" for a server named "convo" was hidden from skill matching.
    monkeypatch.setenv(modules_setup.TRUSTED_ENV, "Convo")
    api = _run(tmp_path, ["--module-skill", "athena=Convo"])
    athena, context = (b for _, _, b in api.skill_writes())
    assert athena["requires_toolsets"] == ["mcp:convo"]
    assert context["requires_toolsets"] == ["mcp:convo"]



class TasksApi(FakeApi):
    def __init__(self, status="paused", opened=False, **kw):
        super().__init__(**kw)
        self.status, self.opened = status, opened

    def __call__(self, request):
        path = request.url.path
        if path == "/api/tasks" and request.method == "GET":
            self.calls.append(("GET", path, None))
            return httpx.Response(200, json={"tasks": [
                {"id": "t1", "action": "tidy_sessions", "status": "paused"},
                {"id": "t9", "action": modules_setup.ACTION_ITEMS_ACTION,
                 "status": self.status}]})
        if path == "/api/tasks/onboarding" and request.method == "GET":
            self.calls.append(("GET", path, None))
            return httpx.Response(200, json={"opened": self.opened, "enabled": False})
        return super().__call__(request)


def test_resumes_the_action_items_task(tmp_path):
    api = _run(tmp_path, ["--model-mode", "unchanged"], TasksApi())
    assert api.writes() == [("POST", "/api/tasks/onboarding", {"enabled": False}),
                            ("POST", "/api/tasks/t9/resume", None)]
    api = _run(tmp_path, ["--model-mode", "unchanged"], TasksApi(opened=True))
    assert api.writes() == [("POST", "/api/tasks/t9/resume", None)]


def test_action_items_task_left_alone_when_active_dry_run_or_opted_out(tmp_path):
    for argv, api in ((["--dry-run"], TasksApi()), ([], TasksApi(status="active")),
                      (["--no-action-items"], TasksApi())):
        api = _run(tmp_path, ["--model-mode", "unchanged", *argv], api)
        assert api.writes() == []
