#!/usr/bin/env python3
"""Fork: the memory eval for the belief ledger (personal-context plan stage 1.6).

Seeds a fictional evidence history (one owner, three people, a project; sources
``odysseus-sound`` and ``notes``) through the ledger's public API in
``src/evidence_intake.py``, with every ``now`` given, and checks each case: status,
``confirmed_by`` and whether the belief is in Odysseus memory. Then the memory mirror:
exactly the confirmed beliefs are in ``memory.json``, as ``<text> (source: <link>)``,
and a second ``sync_memory`` leaves the file byte-identical. Prints expected vs actual
per check and a score (``N/N``); exit code 1 on any mismatch. No model is involved.

The Athena side is simulated the way the facts job drives it
(``src/module_facts.py``): each pass submits every fact the module currently lists,
reconciles the source with those ids, applies decay and mirrors memory once.

``--chat`` (optional, the only non-deterministic part) then asks a running Odysseus
"What do you know about Priya Nair?" with Athena's bakeoff client
(``--bakeoff``, env ``ODYSSEUS_URL`` and ``ODYSSEUS_PASSWORD``/``_FILE`` or
``ODYSSEUS_API_TOKEN``, as for ``scripts/module_slash_eval.py``) and reports whether the
answer has the expected facts and their links. It needs ``--data-dir`` set to that
instance's data directory (sandbox only) and ``--owner`` set to the login user.

Every run writes a manifest (``belief_eval.<owner>.json`` in the data dir) of the beliefs
and the one own memory it created; ``--cleanup`` deletes exactly those (beliefs whose
evidence is all the eval's own) and their memory copies, nothing else.

Usage:
    venv/bin/python scripts/belief_eval.py                      # temp dir, removed after
    venv/bin/python scripts/belief_eval.py --data-dir /tmp/belief-eval --keep
    venv/bin/python scripts/belief_eval.py --data-dir <instance data dir> --owner admin \\
        --chat --bakeoff <odysseus-sound>/server/eval/bakeoff.py
    venv/bin/python scripts/belief_eval.py --cleanup --data-dir <dir> --owner admin
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

OWNER = "eval-owner"
ME, PRIYA, TOMAS, LENA, HERON = ("Sam Rivera", "Priya Nair", "Tomas Berg", "Lena Okafor",
                                 "Project Heron")
MODULE, NOTES = "odysseus-sound", "notes"
NOTE_PREFIX = "belief-eval-note-"  # the eval's note ids: never a real note's id
SETTINGS_ENV = ("BELIEF_CONFIRM_SOURCES", "BELIEF_CONFIRM_TIMES", "BELIEF_CONFIRM_DAYS",
                "BELIEF_STALE_DAYS")
OWN_MEMORY = "Sam Rivera keeps the spare bike key in the blue drawer"

# The fictional claims, by case. Module fact ids are 9001+ (an eval range), notes n1+.
GYM = "Sam Rivera's climbing gym is Riverside Climbing"
VEGGIE = "Sam Rivera is vegetarian"
CELLO = "Tomas Berg plays the cello"
PEANUTS = "Priya Nair is allergic to peanuts"
LAUNCH = "Project Heron launches in March"
EMPLOYER = "Tomas Berg works at Halvorsen Freight"
BIRTHDAY = "Priya Nair's birthday is on 12 May"
BIRTHDAY_FIX = "Priya Nair's birthday is on 21 May"
MEETINGS = "Sam Rivera prefers meetings before noon"
DAUGHTER = "Lena Okafor's daughter is called Mira"
RUNS = "Tomas Berg runs the Heron project."
RUNS_VARIANT = "tomas berg   RUNS the Heron Project"
REVIEW = "Priya Nair leads the Heron design review"
DELFT = "Lena Okafor works at the Delft lab"
ROTTERDAM = "Lena Okafor moved to Rotterdam"
TRAIL = "Priya Nair likes trail running"
DEMO = "Project Heron's demo is on 4 November"
LEAVE = "Sam Rivera is on parental leave until 15 October"
ICELANDIC = "Tomas Berg is learning Icelandic"
# What memory must hold at the end (shown texts of the confirmed beliefs).
CONFIRMED = (PEANUTS, ROTTERDAM, BIRTHDAY_FIX, RUNS, VEGGIE,
             DAUGHTER, REVIEW, MEETINGS)
ALL_TEXTS = (GYM, VEGGIE, CELLO, PEANUTS, LAUNCH, EMPLOYER, BIRTHDAY, MEETINGS, DAUGHTER,
             RUNS, REVIEW, DELFT, ROTTERDAM, TRAIL, DEMO, LEAVE, ICELANDIC)

# One line per case: what it shows (the table groups checks by these names).
CASES = {
    "said-once": "a fact said once stays tentative",
    "source-confirmed": "said 3 times on 2 days: the module confirms it",
    "two-sources": "once in a conversation, once in a note: the two-source rule",
    "gone-upstream": "the module stops returning it: stale on reconcile",
    "forgotten": "the owner forgets it: rejected, and resubmits never bring it back",
    "corrected": "the owner corrects it: memory shows the correction; both texts join it",
    "valid-until": "valid_until in the past: stale",
    "decay": "confirmed long ago, nothing since: stale after 180 days, not before",
    "variant": "casing/punctuation variant joins the belief; 3 sightings on 2 days: rule",
    "owner-confirmed": "owner-confirmed: stays confirmed when the module drops it and by age",
    "restored": "forgotten by mistake, restored: confirmed by its source again",
    "erased": "forgotten for good (4.2): no words kept; said again under a new id, it "
              "joins the erased belief without its words",
    "lost-a-source": "two-source belief loses one source: back to tentative",
    "seen-again": "gone, then reported again with a new sighting: confirmed again",
    "one-day": "two notes on one day: still tentative",
    "note-once": "a single note stays tentative",
    "preference": "an owner preference is mirrored with category preference",
    "about-priya": "what the ledger knows about Priya Nair (confirmed, shown text)",
    "memory": "the mirror: exactly the confirmed beliefs, linked, idempotent",
}


def at(month: int, day: int, hour: int = 9) -> datetime:
    return datetime(2026, month, day, hour, 0, tzinfo=timezone.utc)


def module_link(fact_id: int) -> str:
    return f"http://127.0.0.1:8090/view/c{fact_id}?sig=eval#t={fact_id % 97}"


def note_link(note: str) -> str:
    return f"/notes#{NOTE_PREFIX}{note}"


@dataclass
class Check:
    case: str
    step: str
    expected: str
    actual: str

    @property
    def ok(self) -> bool:
        return self.expected == self.actual


class SetupError(RuntimeError):
    """The data dir already holds this eval's beliefs for the owner."""


class Run:
    """One seeded history: the simulated module, notes, and the checks."""

    def __init__(self, owner: str):
        from src import evidence_intake as ledger

        self.ledger = ledger
        self.owner = owner
        self.module: dict[int, dict] = {}  # what Athena currently lists
        self.checks: list[Check] = []

    # ------------------------------------------------------------------ sources
    def fact(self, fact_id: int, subject: str, about: str, text: str, first: str, last: str,
             times: int = 1, days: int = 1, confirmed: bool = False,
             valid_until: str | None = None) -> None:
        self.module[fact_id] = {"subject": subject, "about": about, "text": text,
                                "first": first, "last": last, "times": times, "days": days,
                                "confirmed": confirmed, "valid_until": valid_until}

    def module_pass(self, now: datetime) -> None:
        """What the facts job does each hour: submit everything the module lists,
        reconcile, decay, mirror once. Ids outside the eval's range are kept present, so
        a shared data dir's own module evidence is left alone."""
        ledger = self.ledger
        items = [ledger.Evidence(
            source=MODULE, source_id=str(fid), subject=f["subject"], about=f["about"],
            text=f["text"], link=module_link(fid), seen_first=f["first"], seen_last=f["last"],
            times=f["times"], days=f["days"], source_confirmed=f["confirmed"],
            valid_until=f["valid_until"]) for fid, f in sorted(self.module.items())]
        ledger.submit(self.owner, items, now=now, mirror=False)
        present = {str(fid) for fid in self.module}
        present |= {e["source_id"] for b in ledger.list_beliefs(self.owner, limit=100000)
                    for e in b["evidence"]
                    if e["source"] == MODULE and not _eval_id(e["source_id"])}
        ledger.reconcile_source(self.owner, MODULE, present, now=now, mirror=False)
        ledger.apply_decay(self.owner, now=now, mirror=False)
        ledger.sync_memory(self.owner, now=now)

    def note(self, now: datetime, note: str, subject: str, about: str, text: str) -> None:
        """A single sighting written in a note."""
        day = now.date().isoformat()
        self.ledger.submit(self.owner, [self.ledger.Evidence(
            source=NOTES, source_id=f"{NOTE_PREFIX}{note}", subject=subject, about=about, text=text,
            link=note_link(note), seen_first=day, seen_last=day)], now=now)

    # ------------------------------------------------------------------- reads
    def beliefs(self) -> list[dict]:
        return self.ledger.list_beliefs(self.owner, limit=100000)

    def find(self, text: str) -> list[dict]:
        norm = self.ledger.normalize(text)
        return [b for b in self.beliefs()
                if norm in (self.ledger.normalize(b["text"]),
                            self.ledger.normalize(b["owner_text"] or ""))]

    def belief(self, text: str) -> dict | None:
        found = self.find(text)
        return found[0] if len(found) == 1 else None

    def memories(self) -> list[dict]:
        from src import constants
        from src.memory import MemoryManager

        return [m for m in MemoryManager(constants.DATA_DIR).load_all()
                if (m.get("owner") or "") == self.owner]

    def mirrored(self) -> dict[int, dict]:
        return {m["belief_id"]: m for m in self.memories() if "belief_id" in m}

    def state(self, text: str) -> str:
        """``status/confirmed_by/mem`` of the one belief for ``text``."""
        found = self.find(text)
        if len(found) != 1:
            return f"{len(found)} beliefs"
        b = found[0]
        where = "mem" if b["id"] in self.mirrored() else "out"
        return f"{b['status']}/{b['confirmed_by'] or '-'}/{where}"

    # ------------------------------------------------------------------ checks
    def check(self, case: str, step: str, expected: object, actual: object) -> None:
        self.checks.append(Check(case, step, str(expected), str(actual)))

    def expect(self, case: str, step: str, text: str, expected: str) -> None:
        self.check(case, step, expected, self.state(text))


def _eval_id(source_id: str) -> bool:
    return source_id.isdigit() and 9001 <= int(source_id) <= 9099


def _eval_evidence(source: str, source_id: str) -> bool:
    """Evidence this eval submits: module fact ids 9001-9099, notes ``belief-eval-note-*``."""
    return ((source == MODULE and _eval_id(source_id))
            or (source == NOTES and source_id.startswith(NOTE_PREFIX)))


def scenario(r: Run) -> None:
    """The seeded history, in time order, with its checks."""
    ledger = r.ledger
    CONF_SRC, CONF_OWN, CONF_RULE = "confirmed/source/mem", "confirmed/owner/mem", \
        "confirmed/rule/mem"
    TENT, STALE, REJ = "tentative/-/out", "stale/-/out", "rejected/-/out"

    # March: a gym the module confirmed, and a fact the owner confirms.
    r.fact(9001, ME, "owner", GYM, "2026-03-02", "2026-03-20", 3, 2, True)
    r.fact(9002, ME, "owner", VEGGIE, "2026-03-18", "2026-03-18")
    r.module_pass(at(3, 20))
    r.expect("decay", "03-20 module confirms it", GYM, CONF_SRC)
    r.expect("owner-confirmed", "03-20 said once", VEGGIE, TENT)
    ledger.confirm(r.owner, r.belief(VEGGIE)["id"], now=at(3, 21))
    r.expect("owner-confirmed", "03-21 owner confirms", VEGGIE, CONF_OWN)

    # September: 179 days after the last sighting the gym is still confirmed.
    r.module_pass(at(9, 15))
    r.expect("decay", "09-15 day 179, no new sighting", GYM, CONF_SRC)

    # October 1: the module lists new facts and stops listing the vegetarian one.
    del r.module[9002]
    r.fact(9003, TOMAS, "person", CELLO, "2026-10-01", "2026-10-01")
    r.fact(9004, PRIYA, "person", PEANUTS, "2026-10-01", "2026-10-01")
    r.fact(9005, HERON, "other", LAUNCH, "2026-09-28", "2026-10-01", 3, 2, True)
    r.fact(9006, TOMAS, "person", EMPLOYER, "2026-09-25", "2026-10-01", 3, 2, True)
    r.fact(9007, PRIYA, "person", BIRTHDAY, "2026-09-20", "2026-10-01", 3, 3, True)
    r.fact(9008, ME, "owner", MEETINGS, "2026-09-10", "2026-10-01", 4, 3, True)
    r.fact(9011, LENA, "person", DAUGHTER, "2026-09-29", "2026-10-01", 3, 2, True)
    r.module_pass(at(10, 1))
    r.expect("decay", "10-01 day 195: decayed", GYM, STALE)
    r.expect("owner-confirmed", "10-01 module no longer lists it", VEGGIE, CONF_OWN)
    vegetarian = r.belief(VEGGIE)
    r.check("owner-confirmed", "10-01 its evidence shows gone",
            [True], [e["gone"] for e in vegetarian["evidence"]])
    r.expect("said-once", "10-01 said once", CELLO, TENT)
    r.expect("source-confirmed", "10-01 said once", PEANUTS, TENT)
    r.expect("gone-upstream", "10-01 module confirms it", LAUNCH, CONF_SRC)
    r.expect("forgotten", "10-01 module confirms it", EMPLOYER, CONF_SRC)
    r.expect("corrected", "10-01 module confirms it", BIRTHDAY, CONF_SRC)
    r.expect("preference", "10-01 module confirms it", MEETINGS, CONF_SRC)
    r.check("preference", "10-01 category", "preference",
            (r.belief(MEETINGS) or {}).get("category"))

    # October 2: second sighting of the allergy; new facts; a note on the Delft lab.
    r.fact(9004, PRIYA, "person", PEANUTS, "2026-10-01", "2026-10-02", 2, 2)
    r.fact(9009, TOMAS, "person", RUNS, "2026-10-02", "2026-10-02")
    r.fact(9012, PRIYA, "person", REVIEW, "2026-09-30", "2026-10-02", 3, 2, True)
    r.fact(9013, LENA, "person", DELFT, "2026-10-02", "2026-10-02")
    r.module_pass(at(10, 2))
    r.expect("source-confirmed", "10-02 said twice on 2 days", PEANUTS, TENT)
    r.expect("seen-again", "10-02 module confirms it", REVIEW, CONF_SRC)
    r.expect("lost-a-source", "10-02 module only", DELFT, TENT)
    r.note(at(10, 2, 18), "1", LENA, "person", DELFT)
    r.expect("lost-a-source", "10-02 + a note: two sources", DELFT, CONF_RULE)

    # October 3: third sighting; a variant text; the owner forgets, corrects, restores.
    r.fact(9004, PRIYA, "person", PEANUTS, "2026-10-01", "2026-10-03", 3, 2, True)
    r.fact(9010, TOMAS, "person", RUNS_VARIANT, "2026-10-03", "2026-10-03")
    r.module_pass(at(10, 3))
    r.expect("source-confirmed", "10-03 3 times on 2 days", PEANUTS, CONF_SRC)
    r.expect("variant", "10-03 variant joins: 2 sightings", RUNS, TENT)
    r.check("variant", "10-03 one belief, evidence ids",
            "9009,9010", ",".join(e["source_id"] for b in r.find(RUNS_VARIANT)
                                  for e in b["evidence"]))

    employer = r.belief(EMPLOYER)
    evidence = ledger.reject(r.owner, employer["id"], "wrong company", now=at(10, 3, 12))
    r.expect("forgotten", "10-03 owner forgets it", EMPLOYER, REJ)
    r.check("forgotten", "10-03 reject returns its evidence",
            "odysseus-sound:9006", ",".join(f"{e['source']}:{e['source_id']}" for e in evidence))

    birthday = r.belief(BIRTHDAY)
    memory_id = r.mirrored()[birthday["id"]]["id"]
    ledger.correct(r.owner, birthday["id"], BIRTHDAY_FIX, now=at(10, 3, 12))
    r.expect("corrected", "10-03 owner corrects it", BIRTHDAY, CONF_OWN)
    fixed = r.belief(BIRTHDAY) or {}
    r.check("corrected", "10-03 shown text", BIRTHDAY_FIX, fixed.get("shown_text"))
    entry = r.mirrored().get(birthday["id"], {})
    r.check("corrected", "10-03 memory text",
            f"{BIRTHDAY_FIX} (source: {module_link(9007)})", entry.get("text"))
    r.check("corrected", "10-03 same memory id", True, entry.get("id") == memory_id)

    daughter = r.belief(DAUGHTER)
    ledger.reject(r.owner, daughter["id"], now=at(10, 3, 13))
    r.expect("restored", "10-03 forgotten by mistake", DAUGHTER, REJ)
    ledger.restore(r.owner, daughter["id"], now=at(10, 3, 14))
    r.expect("restored", "10-03 restored", DAUGHTER, CONF_SRC)

    # October 5: the module drops facts, re-reports the forgotten one, repeats the old
    # birthday text in a new conversation, and hears about Rotterdam once.
    count = len(r.beliefs())
    for fid in (9005, 9012, 9013):
        del r.module[fid]
    r.fact(9006, TOMAS, "person", EMPLOYER, "2026-09-25", "2026-10-05", 4, 3, True)
    r.fact(9014, TOMAS, "person", EMPLOYER + ".", "2026-10-05", "2026-10-05", 3, 2, True)
    r.fact(9015, PRIYA, "person", BIRTHDAY, "2026-10-05", "2026-10-05")
    r.fact(9016, LENA, "person", ROTTERDAM, "2026-10-05", "2026-10-05")
    r.module_pass(at(10, 5))
    r.expect("gone-upstream", "10-05 module no longer lists it", LAUNCH, STALE)
    r.expect("seen-again", "10-05 module no longer lists it", REVIEW, STALE)
    r.expect("lost-a-source", "10-05 module drops it: note only", DELFT, TENT)
    r.expect("forgotten", "10-05 module re-reports it, 2 ids", EMPLOYER, REJ)
    r.check("forgotten", "10-05 evidence ids on the rejected belief", "9006,9014",
            ",".join(e["source_id"] for b in r.find(EMPLOYER) for e in b["evidence"]))
    r.expect("corrected", "10-05 original text again: joins", BIRTHDAY, CONF_OWN)
    r.check("corrected", "10-05 evidence ids", "9007,9015",
            ",".join(e["source_id"] for b in r.find(BIRTHDAY) for e in b["evidence"]))
    r.expect("two-sources", "10-05 said once in a conversation", ROTTERDAM, TENT)
    r.check("corrected", "10-05 new beliefs (only Rotterdam)", count + 1, len(r.beliefs()))

    # October 8: notes; the design review is mentioned again; a third "runs" sighting.
    r.note(at(10, 8, 8), "2", LENA, "person", ROTTERDAM + ".")
    r.expect("two-sources", "10-08 + a note", ROTTERDAM, CONF_RULE)
    r.note(at(10, 8, 8), "3", PRIYA, "person", BIRTHDAY_FIX)
    r.check("corrected", "10-08 note with the corrected text joins",
            f"9007,9015,{NOTE_PREFIX}3",
            ",".join(e["source_id"] for b in r.find(BIRTHDAY_FIX) for e in b["evidence"]))
    r.note(at(10, 8, 8), "4", PRIYA, "person", TRAIL)
    r.note(at(10, 8, 19), "5", PRIYA, "person", TRAIL)
    r.expect("one-day", "10-08 two notes, one day", TRAIL, TENT)
    r.note(at(10, 8, 8), "6", HERON, "other", DEMO)
    r.expect("note-once", "10-08 one note", DEMO, TENT)
    r.fact(9012, PRIYA, "person", REVIEW, "2026-09-30", "2026-10-08", 4, 3, True)
    r.fact(9017, TOMAS, "person", RUNS, "2026-10-08", "2026-10-08")
    r.module_pass(at(10, 8))
    r.expect("seen-again", "10-08 reported again, new sighting", REVIEW, CONF_SRC)
    r.expect("variant", "10-08 third sighting, 3 days", RUNS, CONF_RULE)

    # October 8: the owner forgets a confirmed fact for good (the review page's Forget,
    # stage 4.2; the module side is src/belief_forget.py and its tests).
    r.fact(9019, TOMAS, "person", ICELANDIC, "2026-10-02", "2026-10-08", 3, 2, True)
    r.module_pass(at(10, 8, 1))
    r.expect("erased", "10-08 module confirms it", ICELANDIC, CONF_SRC)
    erased_id = r.belief(ICELANDIC)["id"]
    ledger.reject(r.owner, erased_id, "private", now=at(10, 8, 12))
    ledger.erase(r.owner, erased_id, {"9019": "erased"}, now=at(10, 8, 12))
    gone = ledger.get_belief(r.owner, erased_id)
    r.check("erased", "10-08 rejected, out of memory", "rejected/out",
            f"{gone['status']}/{'mem' if erased_id in r.mirrored() else 'out'}")
    r.check("erased", "10-08 no words in the belief, evidence or events", "",
            "".join([gone["text"], gone["owner_text"] or "", gone["note"] or "",
                     gone["subject"], *(e["text"] + e["link"] for e in gone["evidence"]),
                     *(e["detail"] for e in gone["events"])]))
    r.check("erased", "10-08 no belief shows its text", 0, len(r.find(ICELANDIC)))

    # October 10: leave with an end date; the erased fact is said again in a new
    # conversation (new module id); October 21: the final pass.
    r.fact(9020, TOMAS, "person", ICELANDIC, "2026-10-10", "2026-10-10", 3, 2, True)
    r.fact(9018, ME, "owner", LEAVE, "2026-09-01", "2026-10-10", 3, 3, True,
           valid_until="2026-10-15")
    r.module_pass(at(10, 10))
    r.expect("valid-until", "10-10 before its end date", LEAVE, CONF_SRC)
    again = ledger.get_belief(r.owner, erased_id)
    r.check("erased", "10-10 said again: joins the erased belief, no words",
            "9019:done,9020:pending", ",".join(
                f"{e['source_id']}:{e['payload'].get('erase')}" for e in again["evidence"]
                if not e["text"]))
    r.check("erased", "10-10 still rejected, nothing new", "rejected/0",
            f"{again['status']}/{len(r.find(ICELANDIC))}")
    r.module_pass(at(10, 21))
    final = {
        "said-once": (CELLO, TENT), "source-confirmed": (PEANUTS, CONF_SRC),
        "two-sources": (ROTTERDAM, CONF_RULE), "gone-upstream": (LAUNCH, STALE),
        "forgotten": (EMPLOYER, REJ), "corrected": (BIRTHDAY_FIX, CONF_OWN),
        "valid-until": (LEAVE, STALE), "decay": (GYM, STALE),
        "variant": (RUNS, CONF_RULE), "owner-confirmed": (VEGGIE, CONF_OWN),
        "restored": (DAUGHTER, CONF_SRC), "lost-a-source": (DELFT, TENT),
        "seen-again": (REVIEW, CONF_SRC), "one-day": (TRAIL, TENT),
        "note-once": (DEMO, TENT), "preference": (MEETINGS, CONF_SRC),
    }
    for case, (text, expected) in final.items():
        r.expect(case, "10-21 final", text, expected)
    r.check("valid-until", "10-21 valid_until kept", "2026-10-15",
            (r.belief(LEAVE) or {}).get("valid_until"))
    r.check("decay", "10-21 old sighting re-reported: no flap", "2026-03-20",
            (r.belief(GYM) or {}).get("last_seen"))

    about = sorted(b["shown_text"] for b in ledger.list_beliefs(
        r.owner, status="confirmed", subject=PRIYA.lower()))
    r.check("about-priya", "10-21 confirmed shown texts",
            sorted([PEANUTS, BIRTHDAY_FIX, REVIEW]), about)
    mirrored = r.mirrored()
    r.check("about-priya", "10-21 memory links (what --chat expects cited)",
            [link for _, _, _, link in CHAT_EXPECT],
            [mirrored.get((r.belief(text) or {}).get("id"), {}).get("link")
             for _, text, _, _ in CHAT_EXPECT])
    memory_checks(r, at(10, 21, 12))


def memory_checks(r: Run, now: datetime) -> None:
    from src import constants

    beliefs = r.beliefs()
    confirmed = {b["id"]: b for b in beliefs if b["status"] == "confirmed"}
    mine = r.memories()
    mirrored = [m for m in mine if "belief_id" in m]
    r.check("memory", "mirrored beliefs == confirmed beliefs", sorted(confirmed),
            sorted({m["belief_id"] for m in mirrored}))
    eval_norms = {r.ledger.normalize(t) for t in (*ALL_TEXTS, BIRTHDAY_FIX)}
    r.check("memory", "the expected confirmed set is in memory", sorted(CONFIRMED),
            sorted(confirmed[m["belief_id"]]["shown_text"] for m in mirrored
                   if m["belief_id"] in confirmed
                   and r.ledger.normalize(confirmed[m["belief_id"]]["shown_text"])
                   in eval_norms))
    r.check("memory", "one memory per confirmed belief", len(confirmed), len(mirrored))
    wrong = [m["belief_id"] for m in mirrored if m["belief_id"] in confirmed
             and m["text"] != "{} (source: {})".format(confirmed[m["belief_id"]]["shown_text"],
                                                       confirmed[m["belief_id"]]["link"])]
    r.check("memory", "texts are '<shown text> (source: <link>)'", [], wrong)
    r.check("memory", "every memory link is non-empty", True,
            all(m.get("link") and "(source: " in m["text"] for m in mirrored))
    r.check("memory", "no memory keeps the corrected-away text", [],
            [m["text"] for m in mine if "12 May" in m["text"]])
    r.check("memory", "the owner's own memory is untouched", 1,
            sum(m["text"] == OWN_MEMORY and "belief_id" not in m for m in mine))
    path = Path(constants.DATA_DIR) / "memory.json"
    before = path.read_bytes()
    counts = r.ledger.sync_memory(r.owner, now=now)
    r.check("memory", "second sync changes nothing",
            {"added": 0, "updated": 0, "removed": 0},
            {k: counts[k] for k in ("added", "updated", "removed")})
    r.check("memory", "second sync: memory.json byte-identical", True,
            path.read_bytes() == before)


def seed_own_memory(owner: str) -> str:
    """One memory the owner wrote: the mirror must never touch it. Returns its id."""
    from src import constants
    from src.memory import MemoryManager

    manager = MemoryManager(constants.DATA_DIR)
    entries = manager.load_all_for_update()
    for e in entries:
        if e.get("text") == OWN_MEMORY and (e.get("owner") or "") == owner:
            return str(e["id"])
    entry = manager.add_entry(OWN_MEMORY, source="user", category="fact", owner=owner or None)
    manager.save([*entries, entry])
    return entry["id"]


def manifest_path(data_dir: str | Path, owner: str) -> Path:
    slug = "".join(c if (c.isalnum() or c in "-_.@") else "_" for c in (owner or "default"))
    return Path(data_dir) / f"belief_eval.{slug}.json"


def _created(ledger, owner: str, before: set[int]) -> list[int]:
    """Beliefs that are new since ``before`` and backed only by the eval's evidence."""
    return sorted(b["id"] for b in ledger.list_beliefs(owner, limit=100000)
                  if b["id"] not in before and b["evidence"]
                  and all(_eval_evidence(e["source"], e["source_id"]) for e in b["evidence"]))


def run_eval(data_dir: str | Path, owner: str = OWNER) -> list[Check]:
    """Seed and check in ``data_dir``. DATA_DIR and the BELIEF_* settings are set for the
    run (defaults) and restored afterwards. Raises :class:`SetupError` when the dir
    already has this eval's beliefs for ``owner``."""
    from src import constants
    from src import evidence_intake as ledger

    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    saved_dir = constants.DATA_DIR
    saved_env = {k: os.environ.pop(k) for k in SETTINGS_ENV if k in os.environ}
    constants.DATA_DIR = str(data_dir)
    try:
        norms = {ledger.normalize(t) for t in ALL_TEXTS}
        taken = [b["text"] for b in ledger.list_beliefs(owner, limit=100000)
                 if ledger.normalize(b["text"]) in norms]
        existing = ledger.list_beliefs(owner, limit=100000)
        taken += [f"{e['source']}:{e['source_id']}" for b in existing for e in b["evidence"]
                  if _eval_evidence(e["source"], e["source_id"])]
        if taken:
            raise SetupError(f"{data_dir} already has this eval's beliefs or evidence ids for "
                             f"owner {owner!r} ({len(taken)}); run --cleanup, or use a fresh "
                             "--data-dir or --owner")
        before = {b["id"] for b in existing}
        memory_id = seed_own_memory(owner)
        run = Run(owner)
        try:
            scenario(run)
        finally:  # what to remove with --cleanup, even after a failed run
            manifest_path(data_dir, owner).write_text(json.dumps(
                {"owner": owner, "belief_ids": _created(ledger, owner, before),
                 "memory_id": memory_id}, indent=2) + "\n", encoding="utf-8")
        return run.checks
    finally:
        constants.DATA_DIR = saved_dir
        os.environ.update(saved_env)


def cleanup(data_dir: str | Path, owner: str) -> dict[str, int]:
    """Delete what the eval recorded in its manifest for ``owner``: those beliefs (with
    their evidence and events) while every evidence is still the eval's own, their memory
    copies (``sync_memory``), and the seeded own memory. Nothing else. Raises
    :class:`SetupError` without a manifest."""
    from src import constants
    from src import evidence_intake as ledger
    from src.memory import MemoryManager

    path = manifest_path(data_dir, owner)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SetupError(f"no readable eval manifest at {path}: {e}") from e
    if manifest.get("owner") != owner:
        raise SetupError(f"{path} is for owner {manifest.get('owner')!r}, not {owner!r}")
    counts = {"beliefs": 0, "kept": 0, "missing": 0, "memories": 0, "own_memory": 0}
    saved_dir = constants.DATA_DIR
    constants.DATA_DIR = str(data_dir)
    try:
        db = sqlite3.connect(str(ledger.db_path()), timeout=30, isolation_level=None)
        try:
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("BEGIN IMMEDIATE")
            for belief_id in manifest.get("belief_ids") or []:
                if db.execute("SELECT 1 FROM beliefs WHERE id = ? AND owner = ?",
                              (int(belief_id), owner)).fetchone() is None:
                    counts["missing"] += 1
                    continue
                rows = db.execute("SELECT source, source_id FROM evidence WHERE belief_id = ?",
                                  (int(belief_id),)).fetchall()
                if not all(_eval_evidence(src, sid) for src, sid in rows):
                    counts["kept"] += 1  # another source supports it now: leave it
                    continue
                for table_name in ("events", "evidence"):
                    db.execute(f"DELETE FROM {table_name} WHERE belief_id = ?",
                               (int(belief_id),))
                db.execute("DELETE FROM beliefs WHERE id = ? AND owner = ?",
                           (int(belief_id), owner))
                counts["beliefs"] += 1
            db.execute("COMMIT")
        finally:
            db.close()
        counts["memories"] = ledger.sync_memory(owner)["removed"]
        manager = MemoryManager(constants.DATA_DIR)
        entries = manager.load_all_for_update()
        keep = [e for e in entries if not (
            str(e.get("id")) == str(manifest.get("memory_id"))
            and e.get("text") == OWN_MEMORY and (e.get("owner") or "") == owner)]
        if len(keep) != len(entries):
            manager.save(keep)
            counts["own_memory"] = len(entries) - len(keep)
        path.unlink()
    finally:
        constants.DATA_DIR = saved_dir
    return counts


def cleanup_command(data_dir: Path, owner: str) -> str:
    return shlex.join([sys.executable, str(Path(__file__).resolve()), "--cleanup",
                       "--data-dir", str(data_dir), "--owner", owner])


def table(checks: list[Check], out=None) -> int:
    """Print expected vs actual per check and the score; returns the failures."""
    out = out or sys.stdout
    rows = [("case", "step", "expected", "actual", "")]
    rows += [(c.case, c.step, c.expected, c.actual, "ok" if c.ok else "FAIL") for c in checks]
    width = [min(48, max(len(row[i]) for row in rows)) for i in range(4)]
    for row in rows:
        cells = [row[i][:width[i]].ljust(width[i]) for i in range(4)]
        print("  ".join(cells + [row[4]]).rstrip(), file=out)
    passed = sum(c.ok for c in checks)
    cases = {}
    for c in checks:
        cases[c.case] = cases.get(c.case, True) and c.ok
    print(f"\ncases: {sum(cases.values())}/{len(cases)}   checks: score {passed}/{len(checks)}",
          file=out)
    for c in checks:
        if not c.ok:
            print(f"FAIL {c.case}: {c.step}\n  expected: {c.expected}\n  actual:   {c.actual}",
                  file=out)
    return len(checks) - passed


# ------------------------------------------------------------------------- chat
CHAT_QUESTION = f"What do you know about {PRIYA}?"
MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december")


def mentions_date(text: str, month: int, day: int) -> bool:
    """Whether ``text`` names this day of the year in a common form, any case: "21 May",
    "21st of May", "May 21", "May 21st", "Sept. 3", "2026-05-21"."""
    name = MONTHS[month - 1]
    abbr = [name[:3]] + (["sept"] if month == 9 else [])
    mon = "(?:" + "|".join([name] + [rf"{a}\.?" for a in abbr]) + ")"
    num = rf"0?{day}(?:st|nd|rd|th)?"
    patterns = (rf"\b{num}\s+(?:of\s+)?{mon}(?![a-z])",
                rf"\b{mon}\s+(?:the\s+)?{num}\b",
                rf"\b\d{{4}}-{month:02d}-{day:02d}\b")
    return any(re.search(p, text or "", re.IGNORECASE) for p in patterns)


def citation(link: str) -> str:
    """The part of a link an answer must contain to cite it: the path of a module link
    (``/view/<id>``; the signed query string may differ in the hub's copy), else the
    fragment of a note link."""
    parts = urlsplit(link)
    return parts.path if parts.path.startswith("/view/") else (parts.fragment or parts.path)


def cites(answer: str, link: str) -> bool:
    return re.search(re.escape(citation(link)) + r"(?![\w-])", answer or "",
                     re.IGNORECASE) is not None


CHAT_EXPECT = (  # (label, belief text, does the answer state it, the link to cite)
    ("peanut allergy", PEANUTS, lambda a: "peanut" in a.lower(), module_link(9004)),
    # the transcript link outranks the newer note's relative link (link_rank)
    ("birthday (corrected)", BIRTHDAY_FIX, lambda a: mentions_date(a, 5, 21), module_link(9015)),
    ("design review", REVIEW, lambda a: "design review" in a.lower(), module_link(9012)),
)


def score_answer(answer: str) -> list[tuple[str, bool, bool]]:
    """Per expected fact: (label, fact in the answer, its link cited in the answer)."""
    answer = answer or ""
    return [(label, bool(states(answer)), cites(answer, link))
            for label, _text, states, link in CHAT_EXPECT]


def chat(bakeoff: Path) -> int:
    """Ask the running Odysseus; print the answer and what it contains. Not scored."""
    sys.path.insert(0, str(bakeoff.resolve().parent))
    import bakeoff as client  # Athena's eval script, unmodified

    answer, tools = client.run_odysseus(CHAT_QUESTION)
    print(f"\nchat (not scored): {CHAT_QUESTION}\n  tools: {', '.join(tools) or '-'}")
    print("  answer: " + " ".join(answer.split())[:600])
    results = score_answer(answer)
    for label, fact, link in results:
        print(f"  {label}: fact {'yes' if fact else 'no'}, link {'yes' if link else 'no'}")
    old = mentions_date(answer, 5, 12)
    print(f"  pre-correction birthday mentioned: {'yes' if old else 'no'}")
    print(f"chat: facts {sum(f for _, f, _ in results)}/{len(results)}, "
          f"links {sum(k for _, _, k in results)}/{len(results)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", type=Path,
                    help="where to seed beliefs.db and memory.json (default: a temp dir)")
    ap.add_argument("--keep", action="store_true",
                    help="keep the temp dir (an explicit --data-dir is always kept)")
    ap.add_argument("--owner", help=f"owner to seed (default {OWNER!r}; with --chat, "
                                    "ODYSSEUS_USER or 'admin')")
    ap.add_argument("--chat", action="store_true",
                    help="also ask a running Odysseus (needs --data-dir and --bakeoff)")
    ap.add_argument("--bakeoff", type=Path, help="Athena's server/eval/bakeoff.py")
    ap.add_argument("--cleanup", action="store_true",
                    help="delete what an earlier run seeded for --owner in --data-dir")
    args = ap.parse_args(argv)
    if args.cleanup:
        if not args.data_dir:
            ap.error("--cleanup needs --data-dir")
        owner = OWNER if args.owner is None else args.owner
        try:
            counts = cleanup(args.data_dir, owner)
        except SetupError as e:
            print(f"belief_eval: {e}", file=sys.stderr)
            return 2
        print("cleanup: " + ", ".join(f"{v} {k}" for k, v in counts.items()))
        return 0
    if args.chat and not (args.data_dir and args.bakeoff):
        ap.error("--chat needs --data-dir (the instance's data dir) and --bakeoff")
    owner = args.owner
    if owner is None:
        owner = os.environ.get("ODYSSEUS_USER", "admin") if args.chat else OWNER

    temp = args.data_dir is None
    data_dir = Path(tempfile.mkdtemp(prefix="belief-eval-")) if temp else args.data_dir
    try:
        try:
            checks = run_eval(data_dir, owner)
        except SetupError as e:
            print(f"belief_eval: {e}", file=sys.stderr)
            return 2
        failed = table(checks)
        if args.chat:
            chat(args.bakeoff)
    finally:
        if temp and not args.keep:
            shutil.rmtree(data_dir, ignore_errors=True)
    if not temp or args.keep:
        print(f"ledger kept: {data_dir / 'beliefs.db'} (owner {owner!r})")
        print(f"cleanup: {cleanup_command(data_dir, owner)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
