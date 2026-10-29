"""Fork: scripts/athena_names_migrate.py on a fictional hub data folder.

The folder is made by the hub's own code as a hub before the rename wrote it (the old
names patched in where today's code writes the new ones): the MCP server registered as
``convo`` with a per-tool disable, voice-note items filed by the action-items job (a
calendar event, a to-do, a note, the "Voice notes" list), Athena's facts in the belief
ledger and memory (and an old Phase 0 memory copy), the ``/athena`` and ``/context``
skills requiring ``mcp:convo``, a chat that used Athena's tools, and an env file.
"""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
from fork.athena import action_items as mai
from fork.athena import client
from fork.beliefs import evidence_intake as ledger
from fork.routing.skill_mcp_toolsets import expand_toolsets
from tests.fork.fakes import FakeMcpManager
from tests.fork.test_action_items_characterization import Module, row

_SPEC = importlib.util.spec_from_file_location(
    "athena_names_migrate",
    Path(__file__).resolve().parents[2] / "scripts" / "athena_names_migrate.py")
migrate = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = migrate  # dataclasses look their module up
_SPEC.loader.exec_module(migrate)

UTC = datetime.UTC
T0 = datetime.datetime(2031, 10, 20, 5, 0, tzinfo=UTC)
OLD, NEW = "odysseus-sound", "athena"
SERVER_ID = "a1b2c3d4"
TOOLS = ["search_transcripts", "get_transcript", "reassign_line"]
ENV_TEXT = ("# fictional deploy settings\n"
            "ODYSSEUS_TRUSTED_READONLY_MCP=convo,notes\n"
            "export MODULE_ACTION_ITEMS_SERVER=\"convo\"\n"
            "  - SOUND_MODULE_SERVER=convo\n"
            "OTHER_SETTING=convo\n")


@pytest.fixture
def hub(data_dir, monkeypatch):
    """The hub's app.db in the data folder, its notes, calendar and pushes."""
    import routes.calendar_routes as cr
    from routes import note_routes
    from src.user_time import clear_user_time_context

    engine = create_engine(f"sqlite:///{data_dir / 'app.db'}", poolclass=NullPool,
                           connect_args={"check_same_thread": False})
    cdb.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(cdb, "SessionLocal", factory)
    monkeypatch.setattr(cr, "SessionLocal", factory, raising=False)
    monkeypatch.setattr(mai, "_LOCKS", {})
    for name in ("MODULE_FILE_ITEMS", "BARVIS_FILE_ITEMS", "MODULE_NOTIFY_CHANNEL",
                 "BARVIS_NOTIFY_CHANNEL", "MODULE_DIGEST_HOUR", "BARVIS_DIGEST_HOUR",
                 "MODULE_ACTION_ITEMS_SERVER", "BARVIS_ACTION_ITEMS_SERVER",
                 "SOUND_MODULE_SERVER", "BARVIS_SOUND_SERVER", "BELIEF_CONFIRM_SOURCES",
                 "BELIEF_CONFIRM_TIMES", "BELIEF_CONFIRM_DAYS", "BELIEF_STALE_DAYS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("ODYSSEUS_TRUSTED_READONLY_MCP", "convo")
    clear_user_time_context()

    async def fake_dispatch(title, body, key, owner="", settings_override=None, **kw):
        return {"ntfy_sent": True}

    monkeypatch.setattr(note_routes, "dispatch_reminder", fake_dispatch)
    yield data_dir, factory
    engine.dispose()


def fact(fid, text, *, subject="Priya", about="person", confirmed=True, person="p1"):
    return ledger.Evidence(source=ledger.MODULE_SOURCE, source_id=str(fid), subject=subject,
                           about=about, text=text, person_id=person,
                           link=f"http://h:8000/view/c{fid}?sig=s#t=4",
                           seen_first="2031-10-01", seen_last="2031-10-02",
                           source_confirmed=confirmed)


FACTS = [fact(1, "Priya is allergic to peanuts"), fact(2, "Priya lives in Porto"),
         fact(3, "I prefer tea", subject="Me", about="owner", person=None)]
ITEMS = [row(12, "Meet Jason", "event", due_date="2031-10-22", due_time="12:00",
             explicit=True),
         row(14, "Get the proposal done", due_date="2031-10-22", explicit=True),
         row(18, "Ideas for the garden", "note", note_title="Garden ideas"),
         row(22, "Water the plants", explicit=True)]


def run_hub(data_dir: Path, items, facts, now) -> None:
    """One run of the jobs that write Athena's names: action items and facts."""
    module = Module([], [row(1, "Old voice note task", due_date="2031-10-22"), *items])
    asyncio.run(mai.sync("alice", module, now=now))
    ledger.sync_people("alice", ledger.MODULE_SOURCE, [{"id": "p1", "name": "Priya"},
                                                        {"id": "p0", "name": "Me",
                                                         "is_owner": True}], mirror=False)
    ledger.submit("alice", facts, now=now)


def build_old_hub(data_dir: Path, factory, monkeypatch) -> Path:
    """The data folder as the hub wrote it before the rename; returns the env file."""
    from fork.athena import action_items_filing, action_items_lists, action_items_state
    from fork.beliefs import ledger_model
    from services.memory import skills as skills_mod
    from src.memory import MemoryManager

    with monkeypatch.context() as m:  # the old names, where today's code writes new ones
        for module in (mai, action_items_state, action_items_filing, action_items_lists):
            m.setattr(module, "LABEL", OLD)
        m.setattr(ledger_model, "MODULE_SOURCE", OLD)
        m.setattr(ledger, "MODULE_SOURCE", OLD)
        with factory() as db:
            db.add(cdb.McpServer(id=SERVER_ID, name="convo", transport="http",
                                 url="http://h:8001/mcp?access_token=fictional",
                                 disabled_tools=json.dumps(["reassign_line"])))
            db.add(cdb.McpServer(id="n0t35000", name="notes", transport="stdio",
                                 command="notes-mcp"))
            db.add(cdb.Session(id="chat1", name="Athena chat", endpoint_url="http://m",
                               model="m", owner="alice"))
            db.add(cdb.ChatMessage(id="msg1", session_id="chat1", role="assistant",
                                   content="From convo (odysseus-sound): Priya lives in Porto",
                                   meta_data=json.dumps({"tool": f"mcp__{SERVER_ID}__search"
                                                                 "_transcripts"})))
            db.commit()
        asyncio.run(mai.sync("alice", Module([], [row(1, "Old voice note task",
                                                          due_date="2031-10-22")]), now=T0))
        run_hub(data_dir, ITEMS, FACTS, T0)
        manager = MemoryManager(str(data_dir))
        copy = manager.add_entry("Priya once lived in Lyon", source=OLD, owner="alice")
        own = manager.add_entry("Tom likes chess", source="user", owner="alice")
        manager.save([*manager.load_all_for_update(), {**copy, "module_fact_id": 99}, own])
    monkeypatch.setattr(skills_mod, "_approval_policy", lambda owner: (True, 0.85))
    skills = skills_mod.SkillsManager(str(data_dir))
    skills.add_skill(name="athena", description="Use Athena's tools for this message",
                     category="modules", requires_toolsets=["mcp:convo"],
                     procedure=["Search the recordings."], status="published",
                     source="managed", confidence=1.0, owner="alice")
    skills.add_skill(name="context", description="Search every connected module: convo",
                     category="modules", requires_toolsets=["mcp:convo", "mcp:notes"],
                     procedure=["Search every module."], status="published",
                     source="managed", confidence=1.0, owner="alice")
    env = data_dir.parent / f"{data_dir.name}.env"
    env.write_text(ENV_TEXT, encoding="utf-8")
    return env


def files(root: Path) -> dict[str, str]:
    """Every file under ``root`` (the backups left out) -> its sha256."""
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*"))
            if p.is_file() and migrate.BACKUPS not in p.parts}


def tables(path: Path) -> dict[str, list]:
    db = sqlite3.connect(str(path))
    try:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                          "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {n: [list(r) for r in db.execute(f"SELECT * FROM {n} ORDER BY rowid")]
                for n in names}
    finally:
        db.close()


def run(capsys, *argv) -> str:
    assert migrate.main([str(a) for a in argv]) == 0
    return capsys.readouterr().out


@pytest.fixture
def old_hub(hub, monkeypatch):
    data_dir, factory = hub
    env = build_old_hub(data_dir, factory, monkeypatch)
    return data_dir, factory, env


def test_the_old_hub_wrote_the_old_names(old_hub):
    data_dir, _, _ = old_hub
    app = tables(data_dir / "app.db")
    assert {r[1] for r in app["mcp_servers"]} == {"convo", "notes"}
    assert {r[0] for r in sqlite3.connect(str(data_dir / "app.db")).execute(
        "SELECT DISTINCT label FROM notes")} == {OLD, "calendar"}  # and a reminder
    assert {r[0] for r in sqlite3.connect(str(data_dir / "beliefs.db")).execute(
        "SELECT DISTINCT source FROM evidence")} == {OLD}
    memory = json.loads((data_dir / "memory.json").read_text())
    assert {m["source"] for m in memory} == {OLD, "user"}


def test_dry_run_lists_every_change_and_changes_nothing(old_hub, capsys):
    data_dir, _, env = old_hub
    before = files(data_dir), env.read_bytes()
    out = run(capsys, "--data-dir", data_dir, "--env-file", env)
    assert (files(data_dir), env.read_bytes()) == before
    assert "dry run: nothing is changed" in out
    assert f"mcp_servers.name convo -> athena: 1 ({SERVER_ID})" in out
    notes = sqlite3.connect(str(data_dir / "app.db")).execute(
        "SELECT id FROM notes WHERE label = ? ORDER BY id", (OLD,)).fetchall()
    assert len(notes) == 3  # a to-do, a note and the Voice notes list
    assert f"notes.label {OLD} -> athena: {len(notes)} (" in out
    assert "evidence.source odysseus-sound -> athena: 3 (1, 2, 3)" in out
    assert "subject_sources.source odysseus-sound -> athena: 2" in out
    assert "events.detail naming odysseus-sound" in out
    assert "memory.json: 4 to change" in out  # 3 mirrored beliefs and the Phase 0 copy
    assert out.count("requires_toolsets mcp:convo -> mcp:athena: 1 (line ") == 2
    assert f"{env}: 3 to change" in out
    assert "OTHER_SETTING" not in out
    assert "Run again with --apply --i-stopped-the-hub" in out


def test_apply_backs_up_changes_and_a_second_apply_changes_nothing(old_hub, capsys):
    data_dir, _, env = old_hub
    app_before = tables(data_dir / "app.db")
    out = run(capsys, "--data-dir", data_dir, "--env-file", env, "--apply",
              "--i-stopped-the-hub")
    backups = list((data_dir / migrate.BACKUPS).iterdir())
    assert len(backups) == 1 and f"backup: {backups[0]}" in out
    # app.db: only the server's name and the notes' labels changed
    app = tables(data_dir / "app.db")
    for name, rows in app_before.items():
        if name in ("mcp_servers", "notes"):
            continue
        assert app[name] == rows, name
    assert sorted(r[1] for r in app["mcp_servers"]) == ["athena", "notes"]
    server = next(r for r in app["mcp_servers"] if r[0] == SERVER_ID)
    old_server = next(r for r in app_before["mcp_servers"] if r[0] == SERVER_ID)
    assert [v for v in server if v != "athena"] == [v for v in old_server if v != "convo"]
    with cdb.SessionLocal() as db:
        assert {n.label for n in db.query(cdb.Note).all()} == {NEW, "calendar"}
        assert json.loads(db.get(cdb.McpServer, SERVER_ID).disabled_tools) == ["reassign_line"]
        message = db.get(cdb.ChatMessage, "msg1")
        assert message.content == "From convo (odysseus-sound): Priya lives in Porto"
    beliefs = tables(data_dir / "beliefs.db")
    assert {r[3] for r in beliefs["evidence"]} == {NEW}
    assert all(OLD not in str(r) for r in beliefs["events"] + beliefs["subject_sources"])
    memory = json.loads((data_dir / "memory.json").read_text())
    assert {m["source"] for m in memory} == {NEW, "user"}
    for skill in ("athena", "context"):
        text = next((data_dir / "skills").rglob(f"{skill}/SKILL.md")).read_text()
        assert "mcp:athena" in text and "mcp:convo" not in text
    assert "convo" in next((data_dir / "skills").rglob("context/SKILL.md")).read_text()
    assert env.read_text() == ENV_TEXT.replace("=convo,notes", "=athena,notes").replace(
        '"convo"', '"athena"').replace("SOUND_MODULE_SERVER=convo", "SOUND_MODULE_SERVER=athena")

    after = files(data_dir), env.read_bytes()
    out = run(capsys, "--data-dir", data_dir, "--env-file", env, "--apply",
              "--i-stopped-the-hub")
    assert "Nothing to change" in out
    assert (files(data_dir), env.read_bytes()) == after
    assert len(list((data_dir / migrate.BACKUPS).iterdir())) == 1


def test_rollback_restores_every_file_byte_for_byte(old_hub, capsys):
    data_dir, _, env = old_hub
    before = files(data_dir), env.read_bytes()
    run(capsys, "--data-dir", data_dir, "--env-file", env, "--apply", "--i-stopped-the-hub")
    assert files(data_dir) != before[0]
    (backup,) = (data_dir / migrate.BACKUPS).iterdir()
    out = run(capsys, "--data-dir", data_dir, "--rollback", backup, "--i-stopped-the-hub")
    assert "restored" in out
    assert (files(data_dir), env.read_bytes()) == before
    # and it can be applied again
    run(capsys, "--data-dir", data_dir, "--env-file", env, "--apply", "--i-stopped-the-hub")
    assert "Nothing to change" in run(capsys, "--data-dir", data_dir, "--env-file", env)


HOLD = ("import sys, time; f = open(sys.argv[1], 'rb'); print('open', flush=True); "
        "time.sleep(60)")


def test_apply_and_rollback_need_the_hub_stopped(old_hub, capsys):
    data_dir, _, _ = old_hub
    before = files(data_dir)
    with pytest.raises(SystemExit, match="--i-stopped-the-hub"):
        migrate.main(["--data-dir", str(data_dir), "--apply"])
    with pytest.raises(SystemExit, match="--i-stopped-the-hub"):
        migrate.main(["--data-dir", str(data_dir), "--rollback", str(data_dir)])
    holder = subprocess.Popen(  # a running hub holds its files open
        [sys.executable, "-c", HOLD, str(data_dir / "app.db")],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "open"
        with pytest.raises(SystemExit, match=f"processes .*{holder.pid}"):
            migrate.main(["--data-dir", str(data_dir), "--apply", "--i-stopped-the-hub"])
    finally:
        holder.kill()
        holder.wait()
    assert files(data_dir) == before


def test_a_server_already_called_athena_is_left_alone(old_hub, capsys):
    data_dir, factory, _ = old_hub
    with factory() as db:
        db.add(cdb.McpServer(id="b2", name="Athena", transport="http", url="http://x"))
        db.commit()
    out = run(capsys, "--data-dir", data_dir)
    assert "both a 'convo' (a1b2c3d4) and an 'athena' server (b2) are registered" in out
    assert "mcp_servers.name" not in out


# ------------------------------------------- the hub on old data and on migrated data
def outcome(data_dir: Path, factory) -> dict:
    """What the hub does next on this data: Athena's server and its tools, the jobs."""
    with factory() as db:
        server = db.get(cdb.McpServer, SERVER_ID)
        name = server.name
    manager = FakeMcpManager(
        tools={SERVER_ID: [{"name": t, "annotations": {"readOnlyHint": t != "reassign_line"}}
                           for t in TOOLS]},
        connections={SERVER_ID: {"status": "connected", "name": name}})
    from services.memory.skills import SkillsManager

    (skill,) = [s for s in SkillsManager(str(data_dir)).load_all() if s["name"] == "athena"]
    required = skill["requires_toolsets"]
    from fork.mcp import trust

    out = {
        "server": client.ATHENA.server_id(manager),
        "recorder": client.RECORDER.server_id(manager),
        "trusted": sorted(trust.trusted_readonly_mcp_tools(manager)),
        "skill": required,
        "skill tools": sorted(expand_toolsets(required, manager)[0]),
        "uids": [mai._event_uid(12), mai._event_uid(12, 1)],
    }
    with factory() as db:
        notes = sorted((n.title, n.note_type, n.label) for n in db.query(cdb.Note).all())
        events = sorted(e.uid for e in db.query(cdb.CalendarEvent).all())
    # the jobs again: the same items and facts, and one new undated task and fact
    run_hub(data_dir, [*ITEMS, row(27, "Fix the fence", explicit=True)],
            [*FACTS, fact(4, "Priya is allergic to peanuts")],
            T0 + datetime.timedelta(hours=1))
    with factory() as db:
        notes_after = sorted((n.title, n.note_type, n.label) for n in db.query(cdb.Note).all())
        voice = [json.loads(n.items or "[]") for n in db.query(cdb.Note).all()
                 if n.title == mai.VOICE_LIST]
        events_after = sorted(e.uid for e in db.query(cdb.CalendarEvent).all())
    out.update(notes=notes, new_notes=sorted(set(notes_after) - set(notes)),
               voice_lists=[[i["text"] for i in v] for v in voice],
               events_unchanged=events_after == events, events=len(events),
               beliefs=[(b["text"], b["status"], len(b["evidence"]),
                         sorted({e["source"] for e in b["evidence"]}))
                        for b in ledger.list_beliefs("alice")],
               memory=sorted((m.get("text"), m.get("source")) for m in json.loads(
                   (data_dir / "memory.json").read_text())))
    return out


def _renamed(value):
    if isinstance(value, str):
        return value.replace(OLD, NEW)
    if isinstance(value, (list, tuple)):
        return [_renamed(v) for v in value]
    if isinstance(value, dict):
        return {k: _renamed(v) for k, v in value.items()}
    return value


def test_the_hub_does_the_same_on_old_and_on_migrated_data(old_hub, capsys, monkeypatch):
    data_dir, factory, _ = old_hub
    snapshot = {p: (data_dir / p).read_bytes() for p in files(data_dir)}
    old = outcome(data_dir, factory)
    for path, data in snapshot.items():  # the same old data again
        (data_dir / path).write_bytes(data)
    for path in files(data_dir):
        if path not in snapshot:
            (data_dir / path).unlink()
    run(capsys, "--data-dir", data_dir, "--apply", "--i-stopped-the-hub")
    migrated = outcome(data_dir, factory)

    assert old["server"] == migrated["server"] == SERVER_ID
    assert old["recorder"] == migrated["recorder"] == SERVER_ID
    assert old["trusted"] == migrated["trusted"] == [f"mcp__{SERVER_ID}__get_transcript",
                                                     f"mcp__{SERVER_ID}__search_transcripts"]
    assert old["skill tools"] == migrated["skill tools"] == [  # its per-tool disable kept
        f"mcp__{SERVER_ID}__get_transcript", f"mcp__{SERVER_ID}__search_transcripts"]
    assert old["uids"] == migrated["uids"]
    assert (old.pop("skill"), migrated.pop("skill")) == (["mcp:convo"], ["mcp:athena"])
    # nothing filed twice, the one Voice notes list reused, no fact counted twice
    assert old["events_unchanged"] and migrated["events_unchanged"] and old["events"] >= 1
    assert len(old["voice_lists"]) == len(migrated["voice_lists"]) == 1
    assert "Fix the fence" in old["voice_lists"][0]
    assert _renamed(old) == json.loads(json.dumps(migrated))
    assert {s for b in migrated["beliefs"] for s in b[3]} == {NEW}
    assert {s for b in old["beliefs"] for s in b[3]} == {OLD}


def test_an_idle_folder_has_no_open_files(tmp_path):
    (tmp_path / "x").write_text("x")
    assert migrate.open_by_others(tmp_path) == []
