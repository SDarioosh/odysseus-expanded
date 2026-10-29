"""Fork: Athena's old and new stored names, read alike (fork/common/athena_names.py).

The MCP server registered as ``convo`` or ``athena``, and the label, belief source and
memory source ``odysseus-sound`` or ``athena``: every reader takes both, and the belief
ledger never stores both names for one owner.
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from fork.athena import client
from fork.athena.action_items_state import LABEL, is_label
from fork.beliefs import evidence_intake as ledger
from fork.beliefs import notes_evidence
from fork.common import athena_names as names
from fork.insight import today_candidates
from fork.mcp import session_recovery
from fork.routing.skill_mcp_toolsets import expand_toolsets, with_mcp_toolsets
from tests.fork.fakes import FakeMcpManager

OLD, NEW = "odysseus-sound", "athena"


# ------------------------------------------------------------------------ the names
def test_the_names():
    assert (names.SERVER, names.OLD_SERVER) == ("athena", "convo")
    assert (names.SOURCE, names.OLD_SOURCE) == (NEW, OLD)
    assert names.EVENT_UID_PREFIX == "odysseus-sound:item:convo:"
    assert names.server_aliases(" Convo ") == ("convo", "athena")
    assert names.server_aliases("ATHENA") == ("athena", "convo")
    assert names.server_aliases("notes") == ("notes",)
    assert names.server_aliases("") == ()
    assert names.with_server_aliases(["convo", "x"]) == {"convo", "athena", "x"}
    assert [names.is_source(v) for v in (OLD, NEW, "notes", None, "Athena")] == \
        [True, True, False, False, False]
    assert [names.event_uid_server(v) for v in ("convo", "Athena", "a1b2c3d4", "")] == \
        ["convo", "convo", "a1b2c3d4", ""]


# ---------------------------------------------------------------- the MCP server
class Manager(FakeMcpManager):
    def __init__(self, *named):
        super().__init__(
            tools={f"s{n}": [{"name": "search_transcripts"}] for n in range(len(named))},
            connections={f"s{n}": {"status": "connected", "name": name}
                         for n, name in enumerate(named)})


@pytest.mark.parametrize("registered", ["convo", "athena", "Athena"])
@pytest.mark.parametrize("setting", ["convo", "athena"])
def test_the_server_is_found_under_either_name(registered, setting):
    assert client.server_id(Manager("notes", registered), setting) == "s1"


def test_the_exact_name_wins_when_both_are_registered():
    manager = Manager("athena", "convo")
    assert client.server_id(manager, "convo") == "s1"
    assert client.server_id(manager, "athena") == "s0"
    assert client.server_id(manager, "notes") is None


@pytest.mark.parametrize("registered", ["convo", "athena"])
def test_the_link_is_found_under_either_name(monkeypatch, registered):
    monkeypatch.setattr(client, "_server_rows", lambda: [
        ("x1", "notes", "http://n:1/mcp?access_token=n", True),
        ("x2", registered, "http://h:8001/mcp?access_token=T", True)])
    for setting in ("convo", "athena"):
        link = client.AthenaClient(server=setting).link()
        assert (link.base, link.token) == ("http://h:8001", "T")


def test_the_link_prefers_an_enabled_row_then_the_name_given(monkeypatch):
    rows = [("x1", "athena", "http://new:1/mcp?access_token=A", True),
            ("x2", "convo", "http://old:1/mcp?access_token=C", True)]
    monkeypatch.setattr(client, "_server_rows", lambda: rows)
    assert client.AthenaClient(server="convo").link().base == "http://old:1"
    rows[1] = ("x2", "convo", "http://old:1/mcp?access_token=C", False)
    assert client.AthenaClient(server="convo").link().base == "http://new:1"


@pytest.fixture
def no_disables(monkeypatch):
    monkeypatch.setattr("fork.routing.skill_mcp_toolsets._disabled_map", dict)


@pytest.mark.parametrize("registered", ["convo", "athena"])
def test_the_athena_skill_finds_the_server_under_either_name(no_disables, registered):
    manager = Manager(registered)
    for entry in ("mcp:convo", "mcp:athena", "mcp:ATHENA"):
        found, missing = expand_toolsets([entry], manager)
        assert found == {"mcp__s0__search_transcripts"} and missing == set()
    available = with_mcp_toolsets(set(), manager)
    assert {"mcp:convo", "mcp:athena"} <= available


def test_a_skill_keeps_its_exact_server_when_both_names_exist(no_disables):
    manager = Manager("athena", "convo")
    assert expand_toolsets(["mcp:convo"], manager)[0] == {"mcp__s1__search_transcripts"}
    assert expand_toolsets(["mcp:athena"], manager)[0] == {"mcp__s0__search_transcripts"}


@pytest.mark.parametrize("registered", ["convo", "athena"])
@pytest.mark.parametrize("trusted", ["convo", "athena", ""])
def test_session_recovery_knows_the_module_server_under_either_name(monkeypatch, registered,
                                                                     trusted):
    for name in ("MODULE_ACTION_ITEMS_SERVER", "BARVIS_ACTION_ITEMS_SERVER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ODYSSEUS_TRUSTED_READONLY_MCP", trusted)
    manager = Manager("notes", registered)
    assert session_recovery._is_module_server(manager, "s1")
    assert not session_recovery._is_module_server(manager, "s0")


@pytest.mark.parametrize("server", ["athena", "Athena", " ATHENA "])
def test_filed_event_uids_stay_those_of_convo(monkeypatch, server):
    from fork.athena import action_items as mai
    from tests.fork.test_athena_event_uids import TODAY

    monkeypatch.delenv("BARVIS_ACTION_ITEMS_SERVER", raising=False)
    monkeypatch.setenv("MODULE_ACTION_ITEMS_SERVER", server)
    assert {key: mai._event_uid(*key) for key in TODAY} == TODAY


# --------------------------------------------------------------- modules_setup.py
_SPEC = importlib.util.spec_from_file_location(
    "modules_setup_names", Path(__file__).resolve().parents[2] / "scripts" / "modules_setup.py")
modules_setup = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(modules_setup)


@pytest.mark.parametrize(("servers", "asked", "registered"), [
    (["convo"], "convo", "convo"), (["athena"], "convo", "athena"),
    (["Athena"], "convo", "Athena"), (["convo"], "athena", "convo"),
    (["athena", "convo"], "convo", "convo"), ([], "convo", "convo"),
    (["notes"], "notes", "notes")])
def test_modules_setup_uses_the_registered_name(servers, asked, registered):
    rows = [{"name": n} for n in servers]
    assert modules_setup.registered_name(rows, asked) == registered


# ------------------------------------------------------------ labels and sources
def test_labels_of_filed_notes():
    assert LABEL == NEW
    assert is_label(NEW) and is_label(OLD) and not is_label("handwritten")
    assert not is_label(None)


@pytest.mark.parametrize("label", [OLD, NEW, " Athena "])
def test_notes_filed_from_athena_are_not_read_for_facts(label):
    note = SimpleNamespace(label=label, title="t", content="x" * 80, items=None,
                           note_type="note", archived=False, source="user")
    assert notes_evidence.skip_reason(note) == "module"


@pytest.mark.parametrize("source", [OLD, NEW])
def test_athena_memory_is_offered_whatever_its_category(source):
    memory = [{"text": "Mira's flight lands at nine", "category": "event", "source": source},
              {"text": "An instruction", "category": "event", "source": "user"}]
    picked = today_candidates.select_memory(memory)
    assert picked == ["Mira's flight lands at nine"]


# ------------------------------------------------------------------ the ledger
def ev(source, source_id, text, **kw):
    return ledger.Evidence(source=source, source_id=str(source_id), subject="Priya",
                           about="person", text=text, link=f"http://h/view/c{source_id}",
                           seen_first="2026-10-01", seen_last="2026-10-01", **kw)


def _sources(path: Path) -> list:
    db = sqlite3.connect(str(path))
    try:
        return db.execute("SELECT owner, source, source_id FROM evidence ORDER BY id").fetchall()
    finally:
        db.close()


def test_a_fresh_ledger_is_written_as_athena(data_dir):
    ledger.submit("alice", [ev(OLD, 1, "Priya lives in Porto")], mirror=False)
    ledger.sync_people("alice", OLD, [{"id": "p1", "name": "Priya"}], mirror=False)
    assert _sources(ledger.db_path()) == [("alice", NEW, "1")]
    assert [s["source"] for s in ledger.list_subjects("alice")[0]["sources"]] == [NEW]


def test_an_old_ledger_keeps_its_name_so_no_fact_counts_twice(data_dir, monkeypatch):
    from fork.beliefs import ledger_model

    with monkeypatch.context() as m:  # the hub before the rename
        m.setattr(ledger_model, "MODULE_SOURCE", OLD)
        ledger.submit("alice", [ev(OLD, 1, "Priya lives in Porto")], mirror=False)
    monkeypatch.setenv("BELIEF_CONFIRM_SOURCES", "2")
    monkeypatch.setenv("BELIEF_CONFIRM_TIMES", "99")
    # today's code: the same fact again and a new one with the same claim
    ledger.submit("alice", [ev(ledger.MODULE_SOURCE, 1, "Priya lives in Porto"),
                            ev(ledger.MODULE_SOURCE, 2, "Priya lives in Porto")], mirror=False)
    ledger.submit("bob", [ev(ledger.MODULE_SOURCE, 1, "Tom plays the cello")], mirror=False)
    assert _sources(ledger.db_path()) == [("alice", OLD, "1"), ("alice", OLD, "2"),
                                          ("bob", NEW, "1")]
    (belief,) = ledger.list_beliefs("alice")
    assert belief["status"] == "tentative"  # one source, not two
    gone = ledger.reconcile_source("alice", ledger.MODULE_SOURCE, {"2"}, mirror=False)
    assert gone["gone"] == 1


def test_two_names_on_one_belief_are_still_one_source(data_dir, monkeypatch):
    monkeypatch.setenv("BELIEF_CONFIRM_SOURCES", "2")
    monkeypatch.setenv("BELIEF_CONFIRM_TIMES", "99")
    rows = [{"source": s, "source_confirmed": 0, "times": 1, "days": 1,
             "seen_first": "2026-10-01", "seen_last": "2026-10-01", "gone": False}
            for s in (OLD, NEW)]
    assert ledger.confirmation_for({"status": "tentative", "confirmed_by": None}, rows,
                                   ledger.settings()) is None
    rows[1]["source"] = "notes"
    assert ledger.confirmation_for({"status": "tentative", "confirmed_by": None}, rows,
                                   ledger.settings()) == "rule"
