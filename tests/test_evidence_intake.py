"""Fork: the belief ledger (src/evidence_intake.py).

Fictional data only.
"""
import datetime
import sqlite3

import pytest

from src import evidence_intake as ei
from src.memory import MemoryManager

T0 = datetime.datetime(2026, 10, 21, 9, 0, tzinfo=datetime.timezone.utc)
T1 = T0 + datetime.timedelta(hours=1)
LATER = T0 + datetime.timedelta(days=400)


def ev(source="notes", source_id="n1", text="Likes jazz", *, about="owner", subject="Me",
       seen_first="2026-10-01", seen_last="2026-10-01", **kw):
    return ei.Evidence(source=source, source_id=source_id, subject=subject, about=about,
                       text=text, link=kw.pop("link", f"http://h/{source}/{source_id}"),
                       seen_first=seen_first, seen_last=seen_last, **kw)


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    for var in ("BELIEF_CONFIRM_SOURCES", "BELIEF_CONFIRM_TIMES", "BELIEF_CONFIRM_DAYS",
                "BELIEF_STALE_DAYS"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def memories(path, owner="alice"):
    return [m for m in MemoryManager(str(path)).load_all() if (m.get("owner") or "") == owner]


def mirrored(path, owner="alice"):
    return {m["belief_id"]: m for m in memories(path, owner) if "belief_id" in m}


def only(owner="alice", **kw):
    (belief,) = ei.list_beliefs(owner, **kw)
    return belief


# ------------------------------------------------------------------- intake
def test_new_evidence_is_a_tentative_belief_outside_memory(data):
    counts = ei.submit("alice", [ev()], now=T0)
    assert counts == {"beliefs_added": 1, "evidence_added": 1, "evidence_updated": 0,
                      "confirmed": 0, "unconfirmed": 0, "unchanged": 0, "skipped": 0}
    belief = only()
    assert (belief["status"], belief["confirmed_by"], belief["text"]) == \
        ("tentative", None, "Likes jazz")
    assert (belief["category"], belief["first_seen"], belief["last_seen"]) == \
        ("preference", "2026-10-01", "2026-10-01")
    assert belief["link"] == "http://h/notes/n1" and belief["source"] == "notes"
    (e,) = belief["evidence"]
    assert (e["source"], e["source_id"], e["times"], e["days"], e["gone"]) == \
        ("notes", "n1", 1, 1, False)
    assert memories(data) == []
    assert ei.db_path() == data / "beliefs.db"
    with sqlite3.connect(data / "beliefs.db") as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_source_confirmed_evidence_is_confirmed_and_mirrored(data):
    counts = ei.submit("alice", [ev("odysseus-sound", "7", "Prefers oat milk in coffee",
                                    source_confirmed=True, times=3, days=2)], now=T0)
    assert (counts["beliefs_added"], counts["confirmed"]) == (1, 1)
    belief = only()
    assert (belief["status"], belief["confirmed_by"]) == ("confirmed", "source")
    entry = mirrored(data)[belief["id"]]
    assert entry["text"] == "Prefers oat milk in coffee (source: http://h/odysseus-sound/7)"
    assert (entry["source"], entry["category"], entry["owner"]) == \
        ("odysseus-sound", "preference", "alice")
    assert (entry["module_fact_id"], entry["times_said"], entry["days"]) == (7, 3, 2)
    assert ei.get_belief("alice", belief["id"])["memory_id"] == entry["id"]


def test_evidence_without_provenance_is_skipped(data):
    counts = ei.submit("alice", [ev(link=""), ev(text="  "), ev(source_id="")], now=T0)
    assert counts["skipped"] == 3 and ei.stats("alice")["total"] == 0


def test_same_text_from_two_sources_is_one_belief_confirmed_by_rule(data):
    ei.submit("alice", [ev("odysseus-sound", "7", "Likes jazz.", seen_last="2026-10-02")],
              now=T0)
    assert only()["status"] == "tentative"
    counts = ei.submit("alice", [ev("notes", "n9", "  likes   JAZZ ", seen_last="2026-10-09")],
                       now=T1)
    assert counts == {"beliefs_added": 0, "evidence_added": 1, "evidence_updated": 0,
                      "confirmed": 1, "unconfirmed": 0, "unchanged": 0, "skipped": 0}
    belief = only()
    assert (belief["status"], belief["confirmed_by"]) == ("confirmed", "rule")
    assert {e["source"] for e in belief["evidence"]} == {"odysseus-sound", "notes"}
    assert belief["last_seen"] == "2026-10-09"
    entry = mirrored(data)[belief["id"]]
    # the module's absolute link outranks the newer note's; module fields stay on
    assert entry["text"] == "Likes jazz. (source: http://h/odysseus-sound/7)"
    assert (entry["source"], entry["module_fact_id"]) == ("odysseus-sound", 7)


def test_memory_link_prefers_the_transcript_link(data):
    ei.submit("alice", [ev("odysseus-sound", "4", "Jonas is allergic to peanuts",
                           about="person", subject="Jonas", source_confirmed=True,
                           seen_last="2026-10-02",
                           link="http://h:8000/view/c4?sig=abc#t=40")], now=T0)
    ei.submit("alice", [ev("notes", "n6", "Jonas is allergic to peanuts.", about="person",
                           subject="Jonas", seen_last="2026-10-20",
                           link="/#open=notes&note=n6")], now=T1)
    (entry,) = mirrored(data).values()
    assert entry["link"] == "http://h:8000/view/c4?sig=abc#t=40"
    assert entry["text"] == ("Jonas is allergic to peanuts "
                             "(source: http://h:8000/view/c4?sig=abc#t=40)")
    assert entry["source"] == "odysseus-sound"
    assert only()["link"] == "http://h:8000/view/c4?sig=abc#t=40"


def test_link_rank_order():
    module = {"source": "odysseus-sound", "link": "https://h/view/c1", "seen_last": "2026-01-01",
              "id": 1}
    web = {"source": "email", "link": "http://mail/x", "seen_last": "2026-10-01", "id": 2}
    note = {"source": "notes", "link": "/#open=notes&note=1", "seen_last": "2026-10-20",
            "id": 3}
    rel_module = {**module, "link": "/view/c1", "id": 4, "seen_last": "2026-10-21"}
    assert [ei.link_rank(e) for e in (module, web, note, rel_module)] == [2, 1, 0, 0]
    assert ei.primary_evidence([note, web, module]) is module
    assert ei.primary_evidence([note, web]) is web
    assert ei.primary_evidence([note, rel_module]) is rel_module  # newest among relative
    newer = {**module, "id": 5, "seen_last": "2026-02-01"}
    assert ei.primary_evidence([module, newer]) is newer  # newest among the same rank
    # live evidence still comes before gone evidence
    assert ei.primary_evidence([{**module, "gone": True}, note]) is note


def test_times_and_days_rule(data):
    ei.submit("alice", [ev(times=2, days=1)], now=T0)
    assert only()["status"] == "tentative"
    ei.submit("alice", [ev(times=3, days=1)], now=T0)
    assert only()["status"] == "tentative"  # three times, but on one day
    ei.submit("alice", [ev(times=3, days=2)], now=T0)
    assert (only()["status"], only()["confirmed_by"]) == ("confirmed", "rule")


def test_times_and_days_add_up_across_items_of_one_source(data):
    ei.submit("alice", [ev(source_id="a", times=2, seen_first="2026-10-01",
                           seen_last="2026-10-01"),
                        ev(source_id="b", text="likes jazz!", seen_first="2026-10-05",
                           seen_last="2026-10-05")], now=T0)
    assert only()["confirmed_by"] == "rule"


def test_confirmation_rule_directly():
    st = ei.Settings(confirm_sources=2, confirm_times=3, confirm_days=2)
    belief = {"status": "tentative", "confirmed_by": None}
    row = {"source": "notes", "times": 1, "days": 1, "source_confirmed": False,
           "seen_first": "2026-10-01", "seen_last": "2026-10-01", "gone": False}
    assert ei.confirmation_for(belief, [row], st) is None
    assert ei.confirmation_for(belief, [{**row, "source_confirmed": True}], st) == "source"
    assert ei.confirmation_for(belief, [row, {**row, "source": "chat"}], st) == "rule"
    assert ei.confirmation_for(belief, [{**row, "times": 3, "days": 2}], st) == "rule"
    # gone evidence doesn't count
    assert ei.confirmation_for(belief, [{**row, "source_confirmed": True, "gone": True}],
                               st) is None
    # the owner's decisions win over any evidence
    assert ei.confirmation_for({"status": "rejected", "confirmed_by": None},
                               [{**row, "source_confirmed": True}], st) is None
    assert ei.confirmation_for({"status": "confirmed", "confirmed_by": "owner"}, [], st) == \
        "owner"
    # thresholds are settings
    assert ei.confirmation_for(belief, [row, {**row, "source": "chat"}],
                               ei.Settings(confirm_sources=3)) is None


def test_settings_from_environment(monkeypatch):
    monkeypatch.setenv("BELIEF_CONFIRM_SOURCES", "3")
    monkeypatch.setenv("BELIEF_CONFIRM_TIMES", "5")
    monkeypatch.setenv("BELIEF_CONFIRM_DAYS", "junk")
    monkeypatch.setenv("BELIEF_STALE_DAYS", "0")
    assert ei.settings() == ei.Settings(confirm_sources=3, confirm_times=5, confirm_days=2,
                                        stale_days=1)


def test_resubmitting_is_unchanged_and_updates_count(data):
    ei.submit("alice", [ev()], now=T0)
    assert ei.submit("alice", [ev()], now=T1)["unchanged"] == 1
    counts = ei.submit("alice", [ev(times=2, seen_last="2026-10-04")], now=T1)
    assert counts["evidence_updated"] == 1
    belief = only()
    assert belief["last_seen"] == "2026-10-04" and belief["evidence"][0]["times"] == 2


def test_source_edit_follows_or_moves(data):
    ei.submit("alice", [ev("odysseus-sound", "1", "Prefers oat milk", source_confirmed=True)],
              now=T0)
    first = only()
    # the only evidence changes its text: the belief follows, memory updated in place
    memory_id = mirrored(data)[first["id"]]["id"]
    ei.submit("alice", [ev("odysseus-sound", "1", "Prefers soy milk", source_confirmed=True)],
              now=T1)
    belief = only()
    assert (belief["id"], belief["text"]) == (first["id"], "Prefers soy milk")
    assert mirrored(data)[belief["id"]]["id"] == memory_id
    # with a second source behind it, a changed text is a different claim: it moves
    ei.submit("alice", [ev("notes", "n1", "Prefers soy milk")], now=T1)
    ei.submit("alice", [ev("odysseus-sound", "1", "Prefers rice milk")], now=T1)
    beliefs = {b["text"]: b for b in ei.list_beliefs("alice")}
    assert set(beliefs) == {"Prefers soy milk", "Prefers rice milk"}
    assert [e["source"] for e in beliefs["Prefers soy milk"]["evidence"]] == ["notes"]
    assert beliefs["Prefers soy milk"]["status"] == "tentative"  # one source left
    assert [e["source_id"] for e in beliefs["Prefers rice milk"]["evidence"]] == ["1"]


def test_source_rewording_keeps_the_owners_decision(data):
    """Athena re-summarized a conversation and reworded a fact under the same id: the
    owner's confirmed (or dismissed) belief keeps it, no second belief in memory."""
    bike = "Shahyah owns a carbureted motorcycle with no computer on it."
    reworded = "Shahyah owns a carbureted motorcycle that has no computer."
    person = {"about": "person", "subject": "Shahyah"}
    ei.submit("alice", [ev("odysseus-sound", "7", bike, **person)], now=T0)
    ei.confirm("alice", only()["id"], now=T0)
    ei.submit("alice", [ev("odysseus-sound", "7", reworded, source_confirmed=True, **person)],
              now=T1)
    belief = only()
    assert (belief["text"], belief["status"], belief["confirmed_by"]) == \
        (bike, "confirmed", "owner")
    assert belief["evidence"][0]["text"] == reworded
    assert [m["belief_id"] for m in memories(data)] == [belief["id"]]

    cat = "Has a cat called Luna"
    ei.submit("alice", [ev("odysseus-sound", "8", cat, **person)], now=T0)
    luna = next(b for b in ei.list_beliefs("alice") if b["text"] == cat)
    ei.reject("alice", luna["id"], now=T0)
    ei.submit("alice", [ev("odysseus-sound", "8", "Has a cat that is called Luna", **person)],
              now=T1)
    assert [(b["text"], b["status"]) for b in ei.list_beliefs("alice", status=None)
            if b["id"] != belief["id"]] == [(cat, "rejected")]
    # new words are a different claim: it splits off, as before
    ei.submit("alice", [ev("odysseus-sound", "8", "Has a cat called Bruno", **person)],
              now=T1)
    assert len(ei.list_beliefs("alice", status=None)) == 3

    # about someone else now (a speaker renamed): a different claim, a new belief
    ei.submit("alice", [ev("odysseus-sound", "7", "Bob owns a carbureted motorcycle.",
                           about="person", subject="Bob")], now=T1)
    texts = {b["text"]: b for b in ei.list_beliefs("alice", status=None)}
    assert texts[bike]["evidence"] == []
    assert [e["source_id"] for e in texts["Bob owns a carbureted motorcycle."]["evidence"]] \
        == ["7"]


# ----------------------------------------------------------------- owner actions
def test_owner_confirm_wins_over_the_rule_and_survives_retraction(data):
    ei.submit("alice", [ev()], now=T0)
    belief = ei.confirm("alice", only()["id"], now=T0)
    assert (belief["status"], belief["confirmed_by"]) == ("confirmed", "owner")
    assert belief["id"] in mirrored(data)
    assert belief["events"][0]["kind"] == "confirmed"
    result = ei.reconcile_source("alice", "notes", set(), now=T1)
    assert result == {"gone": 1, "stale": 0, "unconfirmed": 0}
    belief = only()
    assert belief["status"] == "confirmed" and belief["evidence"][0]["gone"] is True
    assert belief["id"] in mirrored(data)
    assert ei.apply_decay("alice", now=LATER) == {"stale": 0}  # never decays
    assert only()["status"] == "confirmed"


def test_reject_leaves_memory_and_stays_rejected(data):
    ei.submit("alice", [ev("odysseus-sound", "3", "Plays the cello", source_confirmed=True)],
              now=T0)
    belief = only()
    assert belief["id"] in mirrored(data)
    evidence = ei.reject("alice", belief["id"], "never did", now=T1)
    assert [(e["source"], e["source_id"]) for e in evidence] == [("odysseus-sound", "3")]
    assert mirrored(data) == {}
    assert ei.get_belief("alice", belief["id"])["note"] == "never did"
    assert ei.get_belief("alice", belief["id"])["memory_id"] is None
    # new evidence, even confirmed by its source, doesn't bring it back
    ei.submit("alice", [ev("odysseus-sound", "3", "Plays the cello", source_confirmed=True,
                           times=5), ev("notes", "n4", "plays the cello")], now=T1)
    belief = only()
    assert belief["status"] == "rejected" and len(belief["evidence"]) == 2
    assert mirrored(data) == {}
    assert ei.stats("alice")["rejected"] == 1


def test_correct_changes_what_memory_shows(data):
    ei.submit("alice", [ev("odysseus-sound", "5", "Jenn lives in Lisbon", about="person",
                           subject="Jenn", source_confirmed=True)], now=T0)
    belief = only()
    memory_id = mirrored(data)[belief["id"]]["id"]
    MemoryManager(str(data)).increment_uses([memory_id])
    fixed = ei.correct("alice", belief["id"], "Jenn lives in  Porto", now=T1)
    assert (fixed["status"], fixed["confirmed_by"], fixed["owner_text"]) == \
        ("confirmed", "owner", "Jenn lives in Porto")
    assert fixed["text"] == "Jenn lives in Lisbon" and fixed["shown_text"] == "Jenn lives in Porto"
    entry = mirrored(data)[belief["id"]]
    assert (entry["id"], entry["uses"]) == (memory_id, 1)
    assert entry["text"] == "Jenn lives in Porto (source: http://h/odysseus-sound/5)"
    assert entry["category"] == "contact"
    # the module still says Lisbon, and is retracted later: the correction stays
    ei.submit("alice", [ev("odysseus-sound", "5", "Jenn lives in Lisbon", about="person",
                           subject="Jenn", source_confirmed=False)], now=T1)
    ei.reconcile_source("alice", "odysseus-sound", set(), now=T1)
    assert only()["status"] == "confirmed"
    # a source that now says the corrected text lands on the same belief
    ei.submit("alice", [ev("notes", "n2", "Jenn lives in Porto.")], now=T1)
    assert len(only()["evidence"]) == 2
    with pytest.raises(ValueError):
        ei.correct("alice", belief["id"], "   ")


def test_restore_puts_it_back_under_the_rule(data):
    ei.submit("alice", [ev(source_confirmed=True)], now=T0)
    belief_id = only()["id"]
    ei.reject("alice", belief_id, now=T0)
    restored = ei.restore("alice", belief_id, now=T1)
    assert (restored["status"], restored["confirmed_by"]) == ("confirmed", "source")
    assert belief_id in mirrored(data)
    assert [e["kind"] for e in restored["events"][:3]] == ["confirmed", "restored", "rejected"]
    # a restored belief without enough evidence stays tentative (and is not staled at once)
    ei.submit("alice", [ev(source_id="n2", text="Hates queues")], now=T0)
    other = only(status="tentative")["id"]
    ei.reconcile_source("alice", "notes", {"n1"}, now=T1)
    assert ei.get_belief("alice", other)["status"] == "stale"
    assert ei.restore("alice", other, now=T1)["status"] == "tentative"
    # restoring a confirmed belief changes nothing
    assert ei.restore("alice", belief_id, now=T1)["status"] == "confirmed"


def test_owner_actions_are_per_owner(data):
    ei.submit("bob", [ev()], now=T0)
    bobs = only("bob")["id"]
    for action in (ei.confirm, ei.reject, ei.restore, ei.get_belief):
        with pytest.raises(ei.BeliefNotFound):
            action("alice", bobs)
    with pytest.raises(ei.BeliefNotFound):
        ei.correct("alice", bobs, "x")
    assert only("bob")["status"] == "tentative"


# ------------------------------------------------------------- reconcile and decay
def test_reconcile_source_stales_beliefs_without_live_evidence(data):
    ei.submit("alice", [ev("odysseus-sound", "1", "Likes jazz", source_confirmed=True),
                        ev("odysseus-sound", "2", "Hates queues", source_confirmed=True)],
              now=T0)
    assert len(mirrored(data)) == 2
    result = ei.reconcile_source("alice", "odysseus-sound", {"2"}, now=T1)
    assert result == {"gone": 1, "stale": 1, "unconfirmed": 0}
    jazz = only(status="stale")
    assert jazz["text"] == "Likes jazz" and jazz["evidence"][0]["gone"] is True
    assert jazz["evidence"][0]["payload"]["gone_at"] == "2026-10-21T10:00:00+00:00"
    assert [e["kind"] for e in ei.get_belief("alice", jazz["id"])["events"][:2]] == \
        ["stale", "retired_upstream"]
    assert set(mirrored(data)) == {only(status="confirmed")["id"]}
    # running it again changes nothing
    assert ei.reconcile_source("alice", "odysseus-sound", {"2"}, now=T1) == \
        {"gone": 0, "stale": 0, "unconfirmed": 0}
    # the source reports it again: back, confirmed and in memory
    counts = ei.submit("alice", [ev("odysseus-sound", "1", "Likes jazz", source_confirmed=True)],
                       now=T1)
    assert (counts["evidence_updated"], counts["confirmed"]) == (1, 1)
    assert ei.get_belief("alice", jazz["id"])["status"] == "confirmed"
    assert len(mirrored(data)) == 2


def test_reconcile_one_source_gone_drops_a_rule_confirmation(data):
    ei.submit("alice", [ev("odysseus-sound", "1"), ev("notes", "n1")], now=T0)
    assert only()["confirmed_by"] == "rule"
    result = ei.reconcile_source("alice", "notes", set(), now=T1)
    assert result == {"gone": 1, "stale": 0, "unconfirmed": 1}
    belief = ei.get_belief("alice", only()["id"])
    assert belief["status"] == "tentative" and mirrored(data) == {}
    assert [(e["kind"], e["detail"]) for e in belief["events"][:2]] == [
        ("unconfirmed", "no longer meets the rule"), ("retired_upstream", "notes n1")]


def test_raised_threshold_unconfirms_on_submit(data, monkeypatch):
    monkeypatch.setenv("BELIEF_CONFIRM_TIMES", "1")
    monkeypatch.setenv("BELIEF_CONFIRM_DAYS", "1")
    ei.submit("alice", [ev()], now=T0)
    assert only()["confirmed_by"] == "rule" and len(mirrored(data)) == 1
    monkeypatch.setenv("BELIEF_CONFIRM_TIMES", "3")
    monkeypatch.setenv("BELIEF_CONFIRM_DAYS", "2")
    counts = ei.submit("alice", [ev()], now=T1)
    assert (counts["unchanged"], counts["unconfirmed"], counts["confirmed"]) == (1, 1, 0)
    belief = ei.get_belief("alice", only()["id"])
    assert belief["status"] == "tentative" and mirrored(data) == {}
    assert belief["events"][0]["kind"] == "unconfirmed"


def test_older_ledger_gets_the_unconfirmed_event_kind(data):
    """A ledger created before ``unconfirmed`` existed is migrated on first open."""
    old = ei.SCHEMA.replace(", 'unconfirmed'", "")
    assert "'unconfirmed'" not in old
    with sqlite3.connect(data / "beliefs.db") as db:
        db.executescript(old)
        db.execute("INSERT INTO beliefs (id, owner, text, norm, created_at, updated_at) "
                   "VALUES (1, 'alice', 'Likes jazz', 'likes jazz', 't', 't')")
        db.execute("INSERT INTO events (id, belief_id, at, kind, detail) "
                   "VALUES (7, 1, 't', 'added', 'from notes')")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO events (belief_id, at, kind) VALUES (1, 't', 'unconfirmed')")
    assert ei.stats("alice")["total"] == 1  # opening migrates
    with sqlite3.connect(data / "beliefs.db") as db:
        assert db.execute("SELECT id, kind, detail FROM events").fetchall() == \
            [(7, "added", "from notes")]
        db.execute("INSERT INTO events (belief_id, at, kind) VALUES (1, 't', 'unconfirmed')")
        assert "'unconfirmed'" in db.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'events'").fetchone()[0]
        assert db.execute("SELECT name FROM sqlite_master WHERE type = 'index' "
                          "AND tbl_name = 'events'").fetchall() == [("events_belief",)]
    ei.stats("alice")  # already migrated: nothing to do


def test_reconcile_only_touches_that_source_and_owner(data):
    ei.submit("alice", [ev("notes", "n1"), ev("chat", "c1", "Hates queues")], now=T0)
    ei.submit("bob", [ev("notes", "n1")], now=T0)
    assert ei.reconcile_source("alice", "notes", set(), now=T1)["gone"] == 1
    assert only("bob")["evidence"][0]["gone"] is False
    assert ei.get_belief("alice", only(status="tentative")["id"])["text"] == "Hates queues"


def test_apply_decay(data, monkeypatch):
    ei.submit("alice", [
        ev("odysseus-sound", "1", "Likes jazz", source_confirmed=True,
           seen_first="2026-01-01", seen_last="2026-03-01"),
        ev("odysseus-sound", "2", "Hates queues", source_confirmed=True,
           seen_first="2026-10-01", seen_last="2026-10-20"),
        ev("odysseus-sound", "3", "Plays chess", source_confirmed=True,
           seen_first="2026-01-01", seen_last="2026-03-01"),
        ev("notes", "n1", "Reads poetry", seen_first="2026-01-01", seen_last="2026-01-01"),
        ev("notes", "n2", "Trip to Oslo booked", about="other", source_confirmed=True,
           valid_until="2026-10-20"),
    ], now=T0)
    chess = next(b for b in ei.list_beliefs("alice") if b["text"] == "Plays chess")
    ei.confirm("alice", chess["id"], now=T0)
    assert len(mirrored(data)) == 4
    assert ei.apply_decay("alice", now=T0) == {"stale": 2}
    status = {b["text"]: b["status"] for b in ei.list_beliefs("alice")}
    assert status == {"Likes jazz": "stale", "Hates queues": "confirmed",
                      "Plays chess": "confirmed", "Reads poetry": "tentative",
                      "Trip to Oslo booked": "stale"}
    jazz = next(b for b in ei.list_beliefs("alice") if b["text"] == "Likes jazz")
    events = ei.get_belief("alice", jazz["id"])["events"]
    assert events[0] == {"at": "2026-10-21T09:00:00+00:00", "kind": "stale",
                         "detail": "not seen since 2026-03-01"}
    assert {m["text"].split(" (")[0] for m in mirrored(data).values()} == \
        {"Hates queues", "Plays chess"}
    # the same unchanged evidence next hour doesn't revive it
    ei.submit("alice", [ev("odysseus-sound", "1", "Likes jazz", source_confirmed=True,
                           seen_first="2026-01-01", seen_last="2026-03-01")], now=T1)
    assert status_of("Likes jazz") == "stale"
    # newly seen: back
    ei.submit("alice", [ev("odysseus-sound", "1", "Likes jazz", source_confirmed=True,
                           seen_first="2026-01-01", seen_last="2026-10-21")], now=T1)
    assert status_of("Likes jazz") == "confirmed"
    # the threshold is a setting
    monkeypatch.setenv("BELIEF_STALE_DAYS", "3")
    assert ei.apply_decay("alice", now=T1) == {"stale": 0}
    next_day = T1 + datetime.timedelta(days=1)
    assert ei.apply_decay("alice", now=next_day, stale_days=1) == {"stale": 1}
    assert status_of("Hates queues") == "stale"


def status_of(text, owner="alice"):
    return next(b["status"] for b in ei.list_beliefs(owner) if b["text"] == text)


# ------------------------------------------------------------------------ memory
def test_memory_mirroring_is_idempotent(data):
    ei.submit("alice", [ev("odysseus-sound", "1", source_confirmed=True),
                        ev("notes", "n2", "Hates queues", source_confirmed=True)], now=T0)
    before = (data / "memory.json").read_text()
    assert ei.sync_memory("alice", now=T1) == {"kept": 2, "added": 0, "updated": 0,
                                                "removed": 0}
    ei.submit("alice", [ev("odysseus-sound", "1", source_confirmed=True)], now=T1)
    assert (data / "memory.json").read_text() == before


def test_memory_keeps_own_and_other_owners_entries_and_dedupes(data):
    mm = MemoryManager(str(data))
    mine = mm.add_entry("I like hiking", source="user", owner="alice")
    bobs = mm.add_entry("Bob likes tea", source="user", owner="bob")
    mm.save([mine, bobs])
    ei.submit("alice", [ev(source_confirmed=True)], now=T0)
    belief_id = only()["id"]
    # Memory Tidy copied the entry: one copy survives
    entries = mm.load_all_for_update()
    twin = dict(next(m for m in entries if m.get("belief_id") == belief_id), id="twin")
    mm.save(entries + [twin])
    assert ei.sync_memory("alice", now=T1)["removed"] == 1
    assert len(mirrored(data)) == 1
    texts = {m["text"] for m in MemoryManager(str(data)).load_all()}
    assert {"I like hiking", "Bob likes tea"} <= texts
    # a mirrored entry of a belief that no longer exists is removed
    entries = mm.load_all_for_update()
    mm.save(entries + [{**twin, "id": "orphan", "belief_id": 999}])
    assert ei.sync_memory("alice", now=T1)["removed"] == 1


def test_unreadable_memory_store_is_not_overwritten(data):
    (data / "memory.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(RuntimeError):
        ei.submit("alice", [ev(source_confirmed=True)], now=T0)
    assert (data / "memory.json").read_text(encoding="utf-8") == "{not json"
    # an owner action still records the decision
    belief_id = only()["id"]
    assert ei.reject("alice", belief_id, now=T1)[0]["source"] == "notes"
    assert only()["status"] == "rejected"


# ------------------------------------------------------------------------- reads
def test_list_filters_and_stats(data):
    ei.submit("alice", [
        ev("notes", "1", "Jenn lives in Lisbon", about="person", subject="Jenn"),
        ev("notes", "2", "Jenn has a dog", about="person", subject="jenn",
           source_confirmed=True),
        ev("notes", "3", "Likes jazz"),
    ], now=T0)
    assert [b["text"] for b in ei.list_beliefs("alice", subject="JENN")] == \
        ["Jenn lives in Lisbon", "Jenn has a dog"]
    assert [b["text"] for b in ei.list_beliefs("alice", status="confirmed")] == \
        ["Jenn has a dog"]
    assert len(ei.list_beliefs("alice", status=["confirmed", "tentative"], limit=2)) == 2
    with pytest.raises(ValueError):
        ei.list_beliefs("alice", status="maybe")
    assert ei.stats("alice") == {"tentative": 2, "confirmed": 1, "stale": 0, "rejected": 0,
                                 "total": 3, "structured": 0}
    assert ei.stats("nobody")["total"] == 0
    assert ei.list_beliefs(None) == [] and ei.list_beliefs("bob") == []


def test_events_stay_small(data):
    ei.submit("alice", [ev()], now=T0)
    belief_id = only()["id"]
    for _ in range(40):
        ei.confirm("alice", belief_id, now=T0)
        ei.reject("alice", belief_id, now=T0)
    with sqlite3.connect(data / "beliefs.db") as db:
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == ei.EVENTS_KEPT
    assert len(ei.get_belief("alice", belief_id)["events"]) == 20


def test_normalize():
    assert ei.normalize("  Likes   JAZZ!. ") == "likes jazz"
    assert ei.normalize("Lives in St. Ives") == "lives in st. ives"


def test_normalize_owner_mentions():
    key = "owner is allergic to peanuts"
    assert ei.normalize("The owner is allergic to peanuts.", "Owner", "owner") == key
    assert ei.normalize("Me is allergic to peanuts.", "Me", "owner") == key
    assert ei.normalize("I am allergic to peanuts", None, "owner") == key
    assert ei.normalize("I’m allergic to peanuts", None, "owner") == key
    assert ei.normalize("Rosalind is allergic to peanuts", "Rosalind", "OWNER") == key
    assert ei.normalize("I have two cats", "Me", "owner") == "owner has two cats"
    assert ei.normalize("My dog is Pixel", "Me", "owner") == "owner's dog is pixel"
    assert ei.normalize("The owner's dog is Pixel.", "Owner", "owner") == "owner's dog is pixel"
    # only a leading subject mention, only for the owner
    assert ei.normalize("Meals at the office are free", "Me", "owner") == \
        "meals at the office are free"
    assert ei.normalize("Owners of the flat pay rent", None, "owner") == \
        "owners of the flat pay rent"
    assert ei.normalize("Likes jazz", "Me", "owner") == "likes jazz"
    assert ei.normalize("I am allergic to peanuts", "Jenn", "person") == \
        "i am allergic to peanuts"
    assert ei.normalize("Jenn lives in Lisbon.", "Jenn", "person") == "jenn lives in lisbon"
    assert ei.normalize("Me is allergic to peanuts.") == "me is allergic to peanuts"


def test_owner_fact_from_a_note_and_a_conversation_is_one_belief(data):
    ei.submit("alice", [ev("notes", "n-1:0", "The owner is allergic to peanuts.",
                           subject="Owner")], now=T0)
    counts = ei.submit("alice", [ev("odysseus-sound", "9", "Me is allergic to peanuts.",
                                    subject="Me")], now=T1)
    assert (counts["beliefs_added"], counts["confirmed"]) == (0, 1)
    ei.submit("alice", [ev("chat", "c-3", "I am allergic to peanuts", subject="Me")], now=T1)
    belief = only()
    assert (belief["status"], belief["confirmed_by"]) == ("confirmed", "rule")
    assert {e["source"] for e in belief["evidence"]} == {"notes", "odysseus-sound", "chat"}
    # a person's fact with the same words is a different belief
    ei.submit("alice", [ev("notes", "n-2:0", "I am allergic to peanuts", about="person",
                           subject="Jenn")], now=T1)
    assert len(ei.list_beliefs("alice")) == 2


def test_owner_correction_is_normalized_with_the_beliefs_about(data):
    ei.submit("alice", [ev("notes", "n-1:0", "The owner is vegetarian.", subject="Owner")],
              now=T0)
    belief = only()
    ei.correct("alice", belief["id"], "I am vegan", now=T1)
    with sqlite3.connect(data / "beliefs.db") as db:
        assert db.execute("SELECT owner_norm FROM beliefs").fetchone()[0] == "owner is vegan"
    ei.submit("alice", [ev("odysseus-sound", "4", "Me is vegan.", subject="Me")], now=T1)
    belief = only()
    assert len(belief["evidence"]) == 2 and belief["shown_text"] == "I am vegan"


def test_existing_ledger_owner_keys_are_migrated_once(data):
    ei.submit("alice", [ev("notes", "n-1:0", "I am allergic to peanuts", subject="Me"),
                        ev("notes", "n-2:0", "Jenn is allergic to cats", about="person",
                           subject="Jenn")], now=T0)
    with sqlite3.connect(data / "beliefs.db") as db:  # as an older ledger stored them
        db.execute("UPDATE beliefs SET norm = lower(rtrim(text, '.'))")
        db.execute("PRAGMA user_version = 0")
    counts = ei.submit("alice", [ev("odysseus-sound", "9", "The owner is allergic to peanuts.",
                                    subject="Owner")], now=T1)
    assert counts["beliefs_added"] == 0
    with sqlite3.connect(data / "beliefs.db") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == ei.ERASE_VERSION
        assert sorted(r[0] for r in db.execute("SELECT norm FROM beliefs")) == \
            ["jenn is allergic to cats", "owner is allergic to peanuts"]
