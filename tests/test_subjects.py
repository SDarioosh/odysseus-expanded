"""Fork: subjects, contact links, structure and projects in the belief ledger
(src/evidence_intake.py, src/module_facts.py; plan stages 2.1, 2.2 and 2.4).

Fictional data only.
"""
import datetime
import json
import sqlite3

import pytest

from src import evidence_intake as ei
from src import module_facts as mf
from src.memory import MemoryManager

T0 = datetime.datetime(2026, 10, 21, 9, 0, tzinfo=datetime.timezone.utc)
T1 = T0 + datetime.timedelta(hours=1)


def ev(source="notes", source_id="n1", text="Likes jazz", *, about="owner", subject="Me",
       **kw):
    return ei.Evidence(source=source, source_id=source_id, subject=subject, about=about,
                       text=text, link=kw.pop("link", f"http://h/{source}/{source_id}"),
                       seen_first=kw.pop("seen_first", "2026-10-01"),
                       seen_last=kw.pop("seen_last", "2026-10-01"), **kw)


def module_ev(fid, text, *, about="person", subject="Priya Nair", person_id=None, **kw):
    return ev("odysseus-sound", str(fid), text, about=about, subject=subject,
              person_id=person_id, source_confirmed=kw.pop("source_confirmed", True), **kw)


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    monkeypatch.setattr(mf, "_LOCKS", {})
    for var in ("BELIEF_CONFIRM_SOURCES", "BELIEF_CONFIRM_TIMES", "BELIEF_CONFIRM_DAYS",
                "BELIEF_STALE_DAYS", "MODULE_FACTS_SYNC"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def belief(text, owner="alice"):
    (b,) = [b for b in ei.list_beliefs(owner) if b["text"] == text]
    return b


def subjects(owner="alice"):
    return {s["display"]: s for s in ei.list_subjects(owner)}


def mirrored(path, owner="alice"):
    return {m["belief_id"]: m for m in MemoryManager(str(path)).load_all()
            if (m.get("owner") or "") == owner and "belief_id" in m}


# ------------------------------------------------------------------ resolution
def test_every_belief_gets_a_subject_by_about(data):
    ei.submit("alice", [ev("notes", "a", "I am vegetarian", subject="Me"),
                        ev("notes", "b", "Priya lives in Porto", about="person",
                           subject="Priya Nair"),
                        ev("notes", "c", "The red bike needs new brakes", about="other",
                           subject="Red bike"),
                        ev("notes", "d", "Something with no subject", about="other",
                           subject="")], now=T0)
    got = subjects()
    assert {k: v["kind"] for k, v in got.items()} == {
        "Owner": "owner", "Priya Nair": "person", "Red bike": "thing", "General": "thing"}
    assert belief("I am vegetarian")["subject_id"] == got["Owner"]["id"]
    b = belief("Priya lives in Porto")
    assert (b["subject_id"], b["subject_display"], b["subject_kind"]) == \
        (got["Priya Nair"]["id"], "Priya Nair", "person")
    assert got["Priya Nair"]["counts"] == {"tentative": 1, "confirmed": 0, "stale": 0,
                                           "rejected": 0, "total": 1}
    assert ei.list_subjects("alice")[0]["kind"] == "owner"  # owner first
    assert [s["display"] for s in ei.list_subjects("alice", kind="person")] == ["Priya Nair"]
    with pytest.raises(ValueError):
        ei.list_subjects("alice", kind="robot")


def test_one_owner_subject_takes_real_names_as_aliases(data):
    ei.submit("alice", [ev("notes", "a", "I am vegetarian", subject="Me"),
                        ev("odysseus-sound", "1", "Sam Rivera runs on Saturdays",
                           subject="Sam Rivera"),
                        ev("notes", "b", "The owner hates queues", subject="Owner")], now=T0)
    (owner,) = ei.list_subjects("alice", kind="owner")
    assert owner["display"] == "Owner" and owner["aliases"] == ["Sam Rivera"]
    assert owner["counts"]["total"] == 3
    assert ei.list_subjects("bob") == []


def test_alias_and_key_match_case_insensitively(data):
    ei.submit("alice", [ev("notes", "a", "Priya lives in Porto", about="person",
                           subject="Priya Nair")], now=T0)
    ei.submit("alice", [ev("notes", "b", "Priya likes figs", about="person",
                           subject="  priya   NAIR ")], now=T0)
    assert len(ei.list_subjects("alice")) == 1
    sid = subjects()["Priya Nair"]["id"]
    ei.rename_subject("alice", sid, "Priya N.")
    ei.submit("alice", [ev("notes", "c", "Priya plays chess", about="person",
                           subject="priya nair")], now=T0)
    assert belief("Priya plays chess")["subject_id"] == sid
    assert ei.find_subject("alice", "PRIYA NAIR")["id"] == sid
    assert ei.find_subject("alice", "nobody") is None


def test_person_id_wins_over_names_and_adds_the_new_name_as_alias(data):
    ei.submit("alice", [module_ev(1, "Priya is allergic to peanuts", person_id="p7")], now=T0)
    sid = belief("Priya is allergic to peanuts")["subject_id"]
    # the module renamed the speaker: same person id, another name
    ei.submit("alice", [module_ev(2, "Priya runs the design review", subject="Priya N",
                                  person_id="p7")], now=T0)
    assert belief("Priya runs the design review")["subject_id"] == sid
    subject = ei.get_subject("alice", sid)
    assert subject["display"] == "Priya Nair" and subject["aliases"] == ["Priya N"]
    assert subject["sources"] == [{"source": "odysseus-sound", "person_id": "p7",
                                   "name": "Priya Nair"}]
    # a different person with the same name as the alias of nobody: its own subject
    ei.submit("alice", [module_ev(3, "Tomas likes sailing", subject="Tomas Berg",
                                  person_id="p8")], now=T0)
    assert belief("Tomas likes sailing")["subject_id"] != sid
    with pytest.raises(ei.SubjectNotFound):
        ei.get_subject("bob", sid)


def test_person_id_on_evidence_joining_an_existing_belief_maps_its_subject(data):
    ei.submit("alice", [ev("notes", "a", "Kath lives in Lisbon", about="person",
                           subject="Kath")], now=T0)
    ei.submit("alice", [module_ev(7, "Kath lives in Lisbon", subject="Katherine",
                                  person_id="p3")], now=T0)
    (kath,) = ei.list_subjects("alice")
    assert kath["sources"][0]["person_id"] == "p3" and kath["aliases"] == ["Katherine"]
    ei.submit("alice", [module_ev(8, "Kath has a dog", subject="Katherine", person_id="p3")],
              now=T0)
    assert belief("Kath has a dog")["subject_id"] == kath["id"]


def test_project_inferred_from_the_subject_name(data):
    ei.submit("alice", [ev("notes", "a", "Project Heron ships in March", about="other",
                           subject="Project Heron"),
                        ev("notes", "b", "The spring launch needs a venue", about="other",
                           subject="Spring launch"),
                        ev("notes", "c", "The planter is broken", about="other",
                           subject="Planter")], now=T0)
    kinds = {k: v["kind"] for k, v in subjects().items()}
    assert kinds == {"Project Heron": "project", "Spring launch": "project",
                     "Planter": "thing"}


# ------------------------------------------------------------------ migration
def test_migration_backfills_subjects_and_adds_columns(data):
    old = ei.SCHEMA.split("CREATE TABLE IF NOT EXISTS subjects")[0]
    for col in ("    subject_id INTEGER,\n", "    attribute TEXT,\n", "    value TEXT,\n",
                "    valid_from TEXT,\n"):
        old = old.replace(col, "")
    old = old.replace("    note TEXT,\n    structured_at TEXT\n", "    note TEXT\n")
    assert "subject_id" not in old and "structured_at" not in old
    with sqlite3.connect(data / "beliefs.db") as db:
        db.executescript(old)
        rows = [(1, "alice", "Me", "owner", "Likes jazz"),
                (2, "alice", "Sam Rivera", "owner", "Sam Rivera hates queues"),
                (3, "alice", "Priya Nair", "person", "Priya lives in Porto"),
                (4, "alice", "Project Heron", "other", "Heron ships in March"),
                (5, "bob", "Me", "owner", "Likes tea")]
        for i, owner, subject, about, text in rows:
            db.execute("INSERT INTO beliefs (id, owner, subject, about, text, norm, "
                       "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 't', 't')",
                       (i, owner, subject, about, text, text.lower()))
        db.execute("PRAGMA user_version = 1")
    got = subjects()
    assert {k: v["kind"] for k, v in got.items()} == {
        "Owner": "owner", "Priya Nair": "person", "Project Heron": "project"}
    assert got["Owner"]["aliases"] == ["Sam Rivera"] and got["Owner"]["counts"]["total"] == 2
    assert [s["kind"] for s in ei.list_subjects("bob")] == ["owner"]
    assert all(b["subject_id"] for b in ei.list_beliefs("alice") + ei.list_beliefs("bob"))
    assert all(b["attribute"] is None and b["structured_at"] is None
               for b in ei.list_beliefs("alice"))
    with sqlite3.connect(data / "beliefs.db") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == ei.ERASE_VERSION
    ei.stats("alice")  # migrated once: opening again changes nothing
    assert len(ei.list_subjects("alice")) == 3


# ------------------------------------------------------------------ owner edits
def test_merge_moves_beliefs_sources_and_aliases(data):
    ei.submit("alice", [module_ev(1, "Priya is allergic to peanuts", person_id="p7"),
                        ev("notes", "a", "Pri likes figs", about="person", subject="Pri"),
                        ev("notes", "b", "Pri plays chess", about="other", subject="Pri N")],
              now=T0)
    got = subjects()
    keep, drop = got["Priya Nair"]["id"], got["Pri"]["id"]
    ei.link_contact("alice", drop, "uid-pri")
    merged = ei.merge_subjects("alice", keep, drop, now=T1)
    assert merged["aliases"] == ["Pri"] and merged["contact_id"] == "uid-pri"
    assert merged["counts"]["total"] == 2 and merged["kind"] == "person"
    assert belief("Pri likes figs")["subject_id"] == keep
    with pytest.raises(ei.SubjectNotFound):
        ei.get_subject("alice", drop)
    # a thing merged into ... the thing takes the person's kind when it is kept
    thing = subjects()["Pri N"]["id"]
    merged = ei.merge_subjects("alice", thing, keep, now=T1)
    assert merged["kind"] == "person" and merged["sources"][0]["person_id"] == "p7"
    assert set(merged["aliases"]) == {"Priya Nair", "Pri"}
    # a later fact with that person id lands on the merged subject
    ei.submit("alice", [module_ev(2, "Priya runs the review", person_id="p7")], now=T1)
    assert belief("Priya runs the review")["subject_id"] == thing
    with pytest.raises(ValueError):
        ei.merge_subjects("alice", thing, thing)


def test_owner_subject_can_be_kept_not_dropped(data):
    ei.submit("alice", [ev("notes", "a", "I am vegetarian"),
                        ev("notes", "b", "Sam runs", about="person", subject="Sam")], now=T0)
    got = subjects()
    with pytest.raises(ValueError):
        ei.merge_subjects("alice", got["Sam"]["id"], got["Owner"]["id"])
    merged = ei.merge_subjects("alice", got["Owner"]["id"], got["Sam"]["id"])
    assert merged["kind"] == "owner" and merged["aliases"] == ["Sam"]


def test_rename_keeps_the_old_name_as_alias(data):
    ei.submit("alice", [ev("notes", "a", "Tomas likes sailing", about="person",
                           subject="Tomas"),
                        ev("notes", "b", "Lena likes tea", about="person", subject="Lena")],
              now=T0)
    sid = subjects()["Tomas"]["id"]
    renamed = ei.rename_subject("alice", sid, "Tomas Berg")
    assert (renamed["display"], renamed["key"], renamed["aliases"]) == \
        ("Tomas Berg", "tomas berg", ["Tomas"])
    assert ei.rename_subject("alice", sid, "Tomas")["aliases"] == ["Tomas Berg"]
    with pytest.raises(ValueError):
        ei.rename_subject("alice", sid, "lena")  # another subject's name: merge instead
    with pytest.raises(ValueError):
        ei.rename_subject("alice", sid, "  ")


def test_set_kind_by_owner_sticks(data):
    ei.submit("alice", [ev("notes", "a", "Heron needs a logo", about="other",
                           subject="Heron")], now=T0)
    sid = subjects()["Heron"]["id"]
    assert ei.set_subject_kind("alice", sid, "project")["kind_by"] == "owner"
    with pytest.raises(ValueError):
        ei.set_subject_kind("alice", sid, "owner")
    owner = ei.list_subjects("alice")
    assert all(s["kind"] != "owner" for s in owner)
    ei.submit("alice", [ev("notes", "b", "I like figs")], now=T0)
    (me,) = ei.list_subjects("alice", kind="owner")
    with pytest.raises(ValueError):
        ei.set_subject_kind("alice", me["id"], "person")


def test_link_and_unlink_contact_and_beliefs_for_contact(data):
    ei.submit("alice", [module_ev(1, "Priya is allergic to peanuts", person_id="p7"),
                        ev("notes", "a", "Priya likes figs", about="person",
                           subject="Priya Nair"),
                        ev("notes", "b", "Lena likes tea", about="person", subject="Lena")],
              now=T0)
    got = subjects()
    priya = got["Priya Nair"]["id"]
    assert ei.link_contact("alice", priya, " uid-priya ")["contact_id"] == "uid-priya"
    assert {b["text"] for b in ei.beliefs_for_contact("alice", "uid-priya")} == {
        "Priya is allergic to peanuts", "Priya likes figs"}
    assert [b["text"] for b in ei.beliefs_for_contact("alice", "uid-priya",
                                                      status="confirmed")] == \
        ["Priya is allergic to peanuts"]
    assert ei.beliefs_for_contact("bob", "uid-priya") == []
    assert ei.beliefs_for_contact("alice", "uid-nobody") == []
    with pytest.raises(ValueError):
        ei.link_contact("alice", got["Lena"]["id"], "uid-priya")
    with pytest.raises(ValueError):
        ei.link_contact("alice", priya, "")
    with pytest.raises(ei.SubjectNotFound):
        ei.link_contact("bob", priya, "uid-x")
    assert ei.unlink_contact("alice", priya)["contact_id"] is None
    assert ei.beliefs_for_contact("alice", "uid-priya") == []


# ------------------------------------------------------------------ memory
def test_memory_entries_carry_subject_fields_without_text_change(data):
    ei.submit("alice", [module_ev(1, "Priya is allergic to peanuts", person_id="p7")], now=T0)
    b = belief("Priya is allergic to peanuts")
    entry = mirrored(data)[b["id"]]
    assert entry["text"] == "Priya is allergic to peanuts (source: http://h/odysseus-sound/1)"
    assert (entry["subject_id"], entry["subject_kind"]) == (b["subject_id"], "person")
    assert "contact_id" not in entry
    memory_id = entry["id"]
    ei.link_contact("alice", b["subject_id"], "uid-priya", now=T1)
    entry = mirrored(data)[b["id"]]
    assert entry["contact_id"] == "uid-priya" and entry["id"] == memory_id
    assert entry["text"] == "Priya is allergic to peanuts (source: http://h/odysseus-sound/1)"
    ei.set_subject_kind("alice", b["subject_id"], "thing", now=T1)
    assert mirrored(data)[b["id"]]["subject_kind"] == "thing"
    ei.unlink_contact("alice", b["subject_id"], now=T1)
    assert "contact_id" not in mirrored(data)[b["id"]]
    before = (data / "memory.json").read_bytes()
    assert ei.sync_memory("alice", now=T1)["updated"] == 0
    assert (data / "memory.json").read_bytes() == before


# ------------------------------------------------------------------ people sync
class Module:
    def __init__(self, facts, people):
        self.facts, self.people, self.calls = facts, people, []

    async def __call__(self, tool, args):
        self.calls.append((tool, args))
        if tool == "list_people":
            if isinstance(self.people, Exception):
                raise self.people
            return self.people if isinstance(self.people, str) else json.dumps(
                {"people": self.people})
        assert tool == "list_facts"
        return json.dumps({"facts": self.facts})


def fact(fid, text, subject, about="person", person_id=None):
    row = {"id": fid, "subject": subject, "about": about, "fact": text, "times_said": 3,
           "days": 2, "first_said": "2026-10-01", "last_said": "2026-10-15",
           "link": f"http://h:8000/view/c{fid}?sig=abc#t=10", "confirmed": True}
    if person_id is not None:
        row["person_id"] = person_id
    return row


PEOPLE = [{"id": 1, "name": "Sam Rivera", "aliases": [], "is_owner": True},
          {"id": 2, "name": "Priya Nair", "aliases": ["Priya", "P. Nair"], "is_owner": False},
          {"id": 3, "name": "Tomas Berg", "aliases": [], "is_owner": False}]


@pytest.mark.asyncio
async def test_sync_maps_people_and_facts_land_on_them(data):
    ei.submit("alice", [ev("notes", "a", "I like figs", subject="Me")], now=T0)
    module = Module([fact(10, "Priya is allergic to peanuts", "Priya", person_id=2),
                     fact(11, "Sam Rivera keeps bees", "Sam Rivera", about="owner",
                          person_id=1),
                     fact(12, "Tomas likes sailing", "Speaker 3", person_id=3)], PEOPLE)
    stats = await mf.sync("alice", module, now=T0)
    assert (stats["people"], stats["subjects_added"]) == (3, 2)
    assert "; people: 3 seen, 2 new" in mf.summary(stats)
    got = subjects()
    assert set(got) == {"Sam Rivera", "Priya Nair", "Tomas Berg"}
    owner = got["Sam Rivera"]
    assert owner["kind"] == "owner" and "Owner" in owner["aliases"]
    assert owner["sources"] == [{"source": "odysseus-sound", "person_id": "1",
                                 "name": "Sam Rivera"}]
    assert set(got["Priya Nair"]["aliases"]) == {"Priya", "P. Nair"}
    assert belief("Priya is allergic to peanuts")["subject_id"] == got["Priya Nair"]["id"]
    assert belief("Sam Rivera keeps bees")["subject_id"] == owner["id"]
    assert belief("I like figs")["subject_id"] == owner["id"]
    assert belief("Tomas likes sailing")["subject_id"] == got["Tomas Berg"]["id"]
    assert "Speaker 3" in got["Tomas Berg"]["aliases"]
    # second pass: nothing new; a person merged away in the module keeps its mapping
    module.people = PEOPLE[:2]
    stats = await mf.sync("alice", module, now=T1)
    assert (stats["subjects_added"], stats["mapped"], stats["aliases_added"]) == (0, 0, 0)
    assert ei.get_subject("alice", got["Tomas Berg"]["id"])["sources"][0]["person_id"] == "3"


@pytest.mark.asyncio
async def test_sync_people_matches_existing_subjects_by_alias(data):
    ei.submit("alice", [ev("notes", "a", "Priya likes figs", about="other",
                           subject="P. Nair")], now=T0)
    (thing,) = ei.list_subjects("alice")
    assert thing["kind"] == "thing"
    await mf.sync("alice", Module([], PEOPLE[1:2]), now=T0)
    (priya,) = ei.list_subjects("alice")
    assert priya["id"] == thing["id"] and priya["kind"] == "person"
    assert priya["display"] == "P. Nair" and set(priya["aliases"]) == {"Priya Nair", "Priya"}


@pytest.mark.asyncio
@pytest.mark.parametrize("people", ["No people enrolled yet.",
                                    RuntimeError("list_people: Unknown tool: list_people")])
async def test_sync_tolerates_a_module_without_people_json(data, people):
    module = Module([fact(10, "Priya is allergic to peanuts", "Priya Nair")], people)
    stats = await mf.sync("alice", module, now=T0)
    assert stats["beliefs_added"] == 1 and "people" not in stats
    assert "people:" not in mf.summary(stats)
    assert subjects()["Priya Nair"]["kind"] == "person"
    state = json.loads((data / "module_facts_alice.json").read_text())
    assert state["people_text_only"] is True
    module.people = PEOPLE
    await mf.sync("alice", module, now=T1)
    state = json.loads((data / "module_facts_alice.json").read_text())
    assert "people_text_only" not in state


def test_parse_people_json_shapes():
    assert mf.parse_people_json('{"people": [{"id": 1, "name": "A"}, {"name": "x"}]}') == \
        [{"id": 1, "name": "A"}]
    assert mf.parse_people_json('[{"id": "p1"}]') == [{"id": "p1"}]
    assert mf.parse_people_json("Speaker 1: Sam") is None
    assert mf.parse_people_json('{"facts": []}') is None


# ------------------------------------------------------------------ structure
def confirmed(text, fid=1, **kw):
    ei.submit("alice", [module_ev(fid, text, **kw)], now=T0)
    return belief(text)


def test_apply_structure_writes_and_validates(data):
    b = confirmed("Priya is allergic to peanuts")
    assert ei.structure_candidates("alice") == [{
        "id": b["id"], "subject": "Priya Nair", "kind": "person",
        "text": "Priya is allergic to peanuts", "first_seen": "2026-10-01",
        "last_seen": "2026-10-01"}]
    assert ei.apply_structure("alice", b["id"], "Allergy", "peanuts", "2020-01-01",
                              "not a date", expected_text=b["text"], now=T1)
    got = ei.get_belief("alice", b["id"])
    assert (got["attribute"], got["value"], got["valid_from"], got["valid_until"]) == \
        ("allergy", "peanuts", "2020-01-01", None)
    assert got["structured_at"] == "2026-10-21T10:00:00+00:00"
    assert ei.structure_candidates("alice") == []
    assert ei.stats("alice")["structured"] == 1
    assert [x["id"] for x in ei.beliefs_by_attribute("alice", "allergy")] == [b["id"]]
    assert ei.beliefs_by_attribute("alice", "allergy", subject_id=b["subject_id"] + 99) == []
    with pytest.raises(ValueError):
        ei.beliefs_by_attribute("alice", "shoe_size")
    # unknown attribute -> other; long value clipped; valid_to only when none yet
    b2 = confirmed("Priya moves to Porto in May", fid=2)
    assert ei.apply_structure("alice", b2["id"], "shoe_size", "x" * 300, None, "2027-05-31")
    got = ei.get_belief("alice", b2["id"])
    assert got["attribute"] == "other" and len(got["value"]) == 120
    assert got["valid_until"] == "2027-05-31"
    ei.apply_structure("alice", b2["id"], "plans", "Porto", None, "2028-01-01")
    assert ei.get_belief("alice", b2["id"])["valid_until"] == "2027-05-31"


def test_apply_structure_skips_changed_or_unconfirmed(data):
    b = confirmed("Priya is allergic to peanuts")
    assert not ei.apply_structure("alice", b["id"], "allergy", "peanuts",
                                  expected_text="something else")
    ei.reject("alice", b["id"])
    assert not ei.apply_structure("alice", b["id"], "allergy", "peanuts")
    assert not ei.apply_structure("bob", b["id"], "allergy", "peanuts")
    assert ei.structure_candidates("alice") == []


def test_correct_and_source_edit_reset_structure(data):
    b = confirmed("Priya is allergic to peanuts")
    ei.apply_structure("alice", b["id"], "allergy", "peanuts")
    ei.correct("alice", b["id"], "Priya is allergic to tree nuts")
    assert [c["text"] for c in ei.structure_candidates("alice")] == \
        ["Priya is allergic to tree nuts"]
    assert ei.get_belief("alice", b["id"])["attribute"] == "allergy"  # kept until redone
    b2 = confirmed("Tomas likes sailing", fid=5, subject="Tomas Berg")
    ei.apply_structure("alice", b2["id"], "likes", "sailing")
    ei.submit("alice", [module_ev(5, "Tomas likes sailing boats", subject="Tomas Berg")],
              now=T1)
    assert ei.get_belief("alice", b2["id"])["structured_at"] is None


def test_project_inferred_from_structure_attributes(data):
    ei.submit("alice", [ev("odysseus-sound", "1", "Heron is in beta", about="other",
                           subject="Heron", source_confirmed=True),
                        ev("odysseus-sound", "2", "Bolt is due in March", about="other",
                           subject="Bolt", source_confirmed=True),
                        ev("odysseus-sound", "3", "Vera's deadline is Friday",
                           about="person", subject="Vera", source_confirmed=True)], now=T0)
    for text, attribute in (("Heron is in beta", "project_status"),
                            ("Bolt is due in March", "deadline"),
                            ("Vera's deadline is Friday", "deadline")):
        ei.apply_structure("alice", belief(text)["id"], attribute, "x")
    got = subjects()
    assert (got["Heron"]["kind"], got["Bolt"]["kind"], got["Vera"]["kind"]) == \
        ("project", "project", "person")
    ei.set_subject_kind("alice", got["Bolt"]["id"], "thing")
    ei.apply_structure("alice", belief("Bolt is due in March")["id"], "deadline", "March")
    assert subjects()["Bolt"]["kind"] == "thing"  # the owner's choice sticks


def test_subject_summary_groups_confirmed_beliefs(data):
    ei.submit("alice", [
        module_ev(1, "Priya is allergic to peanuts"),
        module_ev(2, "Priya lives in Porto"),
        module_ev(3, "Priya ran the design review"),
        module_ev(4, "Priya likes figs", source_confirmed=False)], now=T0)
    sid = belief("Priya is allergic to peanuts")["subject_id"]
    ei.apply_structure("alice", belief("Priya lives in Porto")["id"], "lives_in", "Porto",
                       "2025-03-01")
    ei.apply_structure("alice", belief("Priya is allergic to peanuts")["id"], "allergy",
                       "peanuts")
    assert ei.subject_summary("alice", sid) == (
        "Priya Nair (person). lives in: Porto (since 2025-03-01). allergy: peanuts. "
        "Also: Priya ran the design review.")
    ei.submit("alice", [ev("notes", "z", "Lena likes tea", about="person", subject="Lena")],
              now=T0)
    assert ei.subject_summary("alice", subjects()["Lena"]["id"]) == ""
    with pytest.raises(ei.SubjectNotFound):
        ei.subject_summary("bob", sid)


@pytest.mark.asyncio
@pytest.mark.parametrize("word", ["Me", "the user", "Myself"])
async def test_an_owner_word_person_is_the_owner_when_no_one_is_marked(data, word):
    """P2-D1: a module that marks no ``is_owner`` person but lists one named "Me"."""
    people = [{"id": 2, "name": word, "aliases": [], "is_owner": False},
              {"id": 3, "name": "Alice", "aliases": [], "is_owner": False}]
    module = Module([fact(10, "Me likes figs", word, about="owner", person_id=2),
                     fact(11, "Alice plays chess", "Alice", person_id=3)], people)
    stats = await mf.sync("alice", module, now=T0)
    assert stats["people"] == 2
    kinds = sorted(s["kind"] for s in ei.list_subjects("alice"))
    assert kinds == ["owner", "person"]
    (owner,) = ei.list_subjects("alice", kind="owner")
    assert owner["sources"] == [{"source": "odysseus-sound", "person_id": "2", "name": word}]
    assert word in owner["aliases"] and owner["counts"]["total"] == 1
    assert belief("Me likes figs")["subject_id"] == owner["id"]


@pytest.mark.asyncio
async def test_an_owner_word_person_stays_a_person_when_the_owner_is_marked(data):
    people = [{"id": 1, "name": "Sam Rivera", "aliases": [], "is_owner": True},
              {"id": 2, "name": "Me", "aliases": [], "is_owner": False}]
    await mf.sync("alice", Module([], people), now=T0)
    got = subjects()
    assert got["Sam Rivera"]["kind"] == "owner" and got["Me"]["kind"] == "person"


def test_a_stray_owner_word_subject_from_an_earlier_pass_is_folded_in(data):
    ei.submit("alice", [ev("notes", "a", "I like figs")], now=T0)
    stray = ei.list_subjects("alice")[0]["id"]
    with sqlite3.connect(data / "beliefs.db") as db:  # as the old code left it
        db.execute("INSERT INTO subjects (id, owner, key, display, kind, created_at, "
                   "updated_at) VALUES (50, 'alice', 'me', 'Me', 'person', 't', 't')")
        db.execute("INSERT INTO subject_sources (subject_id, owner, source, "
                   "source_person_id, name, updated_at) VALUES "
                   "(50, 'alice', 'odysseus-sound', '2', 'Me', 't')")
    counts = ei.sync_people("alice", "odysseus-sound",
                            [{"id": 2, "name": "Me", "is_owner": False}], now=T0)
    assert counts["mapped"] == 1
    (owner,) = ei.list_subjects("alice")
    assert owner["id"] == stray and owner["sources"][0]["person_id"] == "2"
    assert ei.find_subject("alice", "me")["id"] == stray


def test_apply_structure_never_sets_a_past_or_pre_sighting_end(data):
    b = confirmed("Priya rents the flat until 2026-10-18")  # last seen 2026-10-01
    assert ei.apply_structure("alice", b["id"], "plans", "flat", None, "2026-09-01", now=T0)
    assert ei.get_belief("alice", b["id"])["valid_until"] is None  # before last seen
    assert ei.apply_structure("alice", b["id"], "plans", "flat", None, "2026-10-18", now=T0)
    assert ei.get_belief("alice", b["id"])["valid_until"] is None  # in the past
    assert ei.apply_structure("alice", b["id"], "plans", "flat", None, "2026-12-01", now=T0)
    assert ei.get_belief("alice", b["id"])["valid_until"] == "2026-12-01"
