#!/usr/bin/env python3
"""Fork: the belief ledger at scale (personal-context plan stage 2.5, "Measure").

Seeds a deterministic fictional corpus (seeded RNG; default 300 subjects and 3000
beliefs) through the ledger's public API in ``src/evidence_intake.py``, with every ``now``
given, and measures it:

* **Ledger**: size (beliefs, evidence, subjects, ``beliefs.db`` bytes), time to submit per
  1000 items (new and unchanged, per chunk so a slowdown with size shows), the owner's
  actions, ``reconcile_source``, ``apply_decay``, ``list_beliefs``, ``list_subjects``,
  ``stats``, ``sync_memory`` (and that a second sync writes nothing, byte-identical), the
  ``memory.json`` size. Every belief must end in its planned status.
* **Structure**: ``src.belief_structure.run`` over every confirmed belief with a
  deterministic fake model (it maps each text template to its attribute): how many got
  structured, how many as planned, and the time per batch (``BELIEF_STRUCTURE_BATCH``, 5).
* **Retrieval** (stage 2.3): a fixed set of generated questions with known expected
  belief ids (by subject name, by attribute, by alias, about the owner) asked of
  ``src.belief_search.search_beliefs`` (confirmed only): top-1, top-5 recall and p50/p95
  latency, with the embedding lane and with the keyword fallback forced. Skipped with a
  message while ``src/belief_search.py`` isn't importable; then the run still passes.

No model and no network are involved. Fictional data only.

Usage:
    venv/bin/python scripts/belief_eval_scale.py                  # medium, temp dir
    venv/bin/python scripts/belief_eval_scale.py --size small
    venv/bin/python scripts/belief_eval_scale.py --size large --out /tmp/scale.json
    venv/bin/python scripts/belief_eval_scale.py --data-dir /tmp/belief-scale --keep

Exit code 0 when every check passes (and, when retrieval runs, top-5 recall of the
default lane is at least ``--min-top5``), 1 otherwise, 2 when the data dir already holds
beliefs for the owner.

Docs: ``docs/fork/beliefs.md`` ("Measure").
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import random
import re
import shutil
import statistics
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

OWNER = "scale-owner"
OWNER_NAME = "Robin Vale"
MODULE, NOTES = "odysseus-sound", "notes"
SEED = 7
NOW = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)
RECENT = (date(2026, 5, 1), date(2026, 10, 20))  # sightings that stay current
OLD = (date(2026, 1, 5), date(2026, 3, 31))      # more than 180 days before NOW
CHUNK = 1000                                      # items per submit call
SETTINGS_ENV = ("BELIEF_CONFIRM_SOURCES", "BELIEF_CONFIRM_TIMES", "BELIEF_CONFIRM_DAYS",
                "BELIEF_STALE_DAYS")
DEFAULT_OUT = REPO / "data" / "eval"  # gitignored (data/)


@dataclass(frozen=True)
class Size:
    people: int
    projects: int
    things: int
    beliefs: int
    owner_beliefs: int
    queries: int

    @property
    def subjects(self) -> int:
        return self.people + self.projects + self.things


SIZES = {
    "small": Size(people=20, projects=4, things=6, beliefs=300, owner_beliefs=12, queries=60),
    "medium": Size(people=200, projects=40, things=60, beliefs=3000, owner_beliefs=40,
                   queries=200),
    "large": Size(people=666, projects=134, things=200, beliefs=10000, owner_beliefs=60,
                  queries=200),
}
PER_PROJECT, PER_THING = 6, 4

# --------------------------------------------------------------- fictional vocabulary
FIRST = ("Ada", "Bram", "Cleo", "Dario", "Edda", "Felix", "Greta", "Hugo", "Ines", "Jonas",
         "Kaia", "Lars", "Mila", "Nico", "Odile", "Pavel", "Quinn", "Rosa", "Silas", "Tilde",
         "Ugo", "Vera", "Wim", "Xenia", "Yara", "Zeno", "Anouk", "Basil", "Carmen", "Dewi",
         "Elio", "Fenna", "Gideon", "Hanne", "Ivo", "Jette", "Kofi", "Liesel", "Matteo",
         "Nadia", "Oskar", "Pilar", "Rafael", "Saskia", "Teun", "Ulla", "Viggo", "Wanda",
         "Yusuf", "Zora", "Amara", "Bjorn", "Cosima", "Dmitri", "Esme", "Florin", "Gaia",
         "Hamid", "Ilse", "Joost")
LAST = ("Achterberg", "Brannigan", "Castellano", "Dalgaard", "Eriksen", "Fairweather",
        "Gallardo", "Hollis", "Ivanova", "Jaramillo", "Kowalczyk", "Lindqvist", "Marchetti",
        "Nakamura", "Oyelaran", "Petrovic", "Quist", "Rasmussen", "Sorensen", "Tanaka",
        "Uribe", "Vasquez", "Wennberg", "Yilmaz", "Zuidema", "Abernathy", "Blomqvist",
        "Cardenas", "Dijkstra", "Esposito", "Fitzgerald", "Grunewald", "Halloran",
        "Iversen", "Jankowski", "Kristiansen", "Lombardi", "Moreau", "Nieminen", "Ostrowski",
        "Pellegrini", "Rautio", "Santangelo", "Thorsen", "Ulrich", "Valentin", "Whitcombe",
        "Yamada", "Zielinski", "Amundsen", "Bergstrom", "Cavanagh", "Delacroix", "Engstrom",
        "Falkenberg", "Gustafsson", "Haverkamp", "Ingram", "Jovanovic", "Kalinowski")
NICK = {"Ada": "Addie", "Bram": "Brammie", "Cleo": "Clee", "Dario": "Dar", "Felix": "Fix",
        "Greta": "Gret", "Hugo": "Hugs", "Jonas": "Jo", "Lars": "Lasse", "Mila": "Milly",
        "Nico": "Nicky", "Pavel": "Pasha", "Rosa": "Rosie", "Silas": "Si", "Vera": "Vee",
        "Matteo": "Teo", "Nadia": "Nadi", "Oskar": "Ossi", "Rafael": "Rafa", "Saskia": "Sas",
        "Viggo": "Vig", "Gideon": "Gid", "Dmitri": "Dima", "Esme": "Ez", "Florin": "Flo"}
PROJECT_WORDS = ("Kestrel", "Heron", "Osprey", "Lark", "Wren", "Plover", "Bittern", "Curlew",
                 "Dunlin", "Egret", "Fulmar", "Gannet", "Harrier", "Ibis", "Jacana", "Kittiwake",
                 "Lapwing", "Merlin", "Nightjar", "Oriole", "Petrel", "Quail", "Redstart",
                 "Siskin", "Tern", "Vireo", "Warbler", "Avocet", "Bunting", "Crake", "Dipper",
                 "Eider", "Firecrest", "Godwit", "Hobby", "Kingfisher", "Linnet", "Martin",
                 "Nuthatch", "Pipit", "Rosefinch", "Shrike", "Thrush", "Twite", "Whimbrel",
                 "Yellowhammer", "Brambling", "Chiffchaff", "Goldcrest", "Hawfinch")
PROJECT_FORMS = ("Project {w}", "{w} launch", "{w} migration", "{w} release", "{w} plan")
THING_ADJ = ("blue", "red", "old", "small", "green", "grey", "white", "black", "yellow",
             "silver", "wooden", "folding", "electric", "vintage", "spare", "big", "orange",
             "striped", "copper", "brown")
THING_NOUN = ("van", "canoe", "cabin", "piano", "tent", "bicycle", "trailer", "boat", "sofa",
              "camera", "drone", "kayak", "lawnmower", "workbench", "telescope", "caravan",
              "scooter", "guitar", "barbecue", "greenhouse")
CITIES = ("Porto", "Lisbon", "Ghent", "Utrecht", "Bergen", "Aarhus", "Turku", "Tallinn",
          "Krakow", "Brno", "Graz", "Lyon", "Nantes", "Bilbao", "Valencia", "Bologna",
          "Trieste", "Ljubljana", "Zagreb", "Malmo", "Uppsala", "Tampere", "Riga", "Vilnius",
          "Gdansk", "Leipzig", "Freiburg", "Basel", "Lausanne", "Leiden", "Delft", "Haarlem",
          "Cork", "Galway", "Bristol", "Leeds", "Dundee", "Cardiff", "Bruges", "Namur")
COMPANIES = ("Halvorsen Freight", "Brightwater Labs", "Kettle and Pine", "Northgate Analytics",
             "Saltmarsh Studio", "Copperleaf Bank", "Driftwood Games", "Larkspur Health",
             "Ironbark Builders", "Bluefin Logistics", "Mossgrove School", "Tidewater Energy",
             "Quarry Lane Bakery", "Silverbirch Media", "Foxglove Pharma", "Granite Peak Law",
             "Harbourlight Hotel", "Juniper Robotics", "Meadowsweet Farm", "Oakhaven Library",
             "Pebble Street Cafe", "Riverstone Clinic", "Sandpiper Travel", "Thistle Design",
             "Upland Water", "Westwind Airlines", "Yarrow Insurance", "Zephyr Cycles",
             "Amberline Rail", "Birchwood Archive")
JOBS = ("nurse", "carpenter", "data analyst", "teacher", "architect", "pharmacist",
        "electrician", "translator", "chef", "veterinarian", "librarian", "pilot",
        "physiotherapist", "civil engineer", "journalist", "baker", "accountant",
        "lab technician", "gardener", "paramedic", "radio host", "tax advisor",
        "set designer", "social worker", "beekeeper")
ALLERGENS = ("peanuts", "shellfish", "cats", "pollen", "penicillin", "dust mites", "eggs",
             "sesame", "latex", "wasp stings", "kiwi fruit", "hazelnuts")
DIETS = ("vegetarian", "vegan", "gluten-free", "low-sugar", "pescatarian", "dairy-free")
HOBBIES = ("trail running", "chess", "birdwatching", "jazz", "pottery", "rock climbing",
           "sailing", "board games", "baking bread", "cycling", "opera", "gardening",
           "photography", "crosswords", "salsa dancing", "fishing", "knitting", "astronomy",
           "karaoke", "camping", "watercolour painting", "skiing", "tennis", "podcasts",
           "woodworking", "rowing", "poetry", "yoga", "cooking curries", "old films")
DISLIKES = ("early meetings", "coriander", "loud music", "flying", "olives", "crowds",
            "horror films", "cold weather", "spicy food", "phone calls", "mushrooms",
            "traffic", "small talk", "camping in the rain", "cats", "jazz", "running",
            "beetroot", "airports", "long emails")
RELATIONS = ("cousin", "neighbour", "colleague", "old school friend", "landlord",
             "sister-in-law", "brother-in-law", "climbing partner", "accountant", "godparent")
SPECIES = ("dog", "cat", "rabbit", "parrot", "tortoise", "horse", "hamster", "goldfish")
PET_NAMES = ("Biscuit", "Pepper", "Juno", "Maple", "Otis", "Pickle", "Luna", "Toffee",
             "Bruno", "Clover", "Ziggy", "Nutmeg", "Rufus", "Willow", "Mochi", "Socks")
HEALTH = ("asthma", "a bad knee", "hay fever", "migraines", "a back injury", "diabetes",
          "high blood pressure", "a broken wrist", "tinnitus", "eczema")
PLANS = ("move abroad next year", "run a marathon", "learn Portuguese", "buy a house",
         "start a bakery", "walk the Camino", "retire early", "write a novel",
         "open a bike shop", "take a sabbatical", "build a treehouse", "study law",
         "visit Japan", "adopt a dog", "renovate the kitchen", "learn to sail",
         "start a podcast", "go back to university", "climb Kilimanjaro", "learn the cello")
OWNED = ("a sailing boat", "an electric bike", "a vintage camera", "a holiday flat",
         "a piano", "a camper van", "a telescope", "a motorbike", "an allotment",
         "a record collection", "a 3D printer", "a canoe")
DECISIONS = ("stop eating meat", "sell the car", "change jobs", "move to the coast",
             "go part-time", "take the train more", "cancel the gym membership",
             "learn to drive", "renovate the attic", "save for a sabbatical")
PHASES = ("planning", "design", "build", "testing", "review", "rollout")
TECH = ("a shared calendar", "open-source tooling", "an external auditor", "weekly demos",
        "a new supplier", "a smaller team", "a phased rollout", "paper prototypes",
        "a fixed budget", "a public beta")
EVENTS = ("a kickoff", "a demo", "a retrospective", "an offsite", "a launch party",
          "a user test")
PLACES = ("the garage", "the cellar", "the boathouse", "the allotment", "the attic",
          "the storage unit", "the barn", "the back garden", "the marina", "the workshop")
SERVICES = ("a service", "new tyres", "repainting", "a new battery", "a deep clean",
            "new strings", "a safety check", "re-varnishing")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December")


def _dates() -> tuple[str, ...]:
    return tuple(f"{d} {m}" for m in MONTHS for d in range(1, 29))


def _due() -> tuple[str, ...]:
    return tuple(f"{d} {m} 2027" for m in MONTHS for d in (3, 10, 17, 24))


def _pets() -> tuple[str, ...]:
    return tuple(f"a {s} called {n}" for s in SPECIES for n in PET_NAMES)


def _month_plans() -> tuple[str, ...]:
    return tuple(f"{e} in {m}" for e in EVENTS for m in MONTHS)


def _month_services() -> tuple[str, ...]:
    return tuple(f"{s} in {m}" for s in SERVICES for m in MONTHS)


# ---------------------------------------------------------------------- templates
@dataclass(frozen=True)
class Template:
    """One kind of belief: ``text`` with ``{s}`` (the subject's name) and ``{v}`` (the
    value), the ``attribute`` it structures to, whether a subject has at most one, and the
    question that asks for it (``{s}`` the name; owner questions say I/my)."""

    key: str
    kinds: tuple[str, ...]
    attribute: str
    text: str
    values: Callable[[], tuple[str, ...]]
    single: bool
    question: str
    owner_question: str = ""


def _v(items: tuple[str, ...]) -> Callable[[], tuple[str, ...]]:
    return lambda: items


P, O, PR, T = "person", "owner", "project", "thing"
TEMPLATES = (
    Template("lives_in", (P, O), "lives_in", "{s} lives in {v}", _v(CITIES), True,
             "Where does {s} live?", "Where do I live?"),
    Template("works_at", (P, O), "works_at", "{s} works at {v}", _v(COMPANIES), True,
             "Where does {s} work?", "Where do I work?"),
    Template("role", (P, O), "role", "{s} works as a {v}", _v(JOBS), True,
             "What does {s} do for a living?", "What is my job?"),
    Template("birthday", (P, O), "birthday", "{s}'s birthday is on {v}", _dates, True,
             "When is {s}'s birthday?", "When is my birthday?"),
    Template("allergy", (P, O), "allergy", "{s} is allergic to {v}", _v(ALLERGENS), False,
             "Is {s} allergic to anything?", "Am I allergic to anything?"),
    Template("diet", (P, O), "diet", "{s} follows a {v} diet", _v(DIETS), True,
             "What diet does {s} follow?", "What diet do I follow?"),
    Template("likes", (P, O), "likes", "{s} likes {v}", _v(HOBBIES), False,
             "What does {s} like?", "What do I like?"),
    Template("dislikes", (P, O), "dislikes", "{s} dislikes {v}", _v(DISLIKES), False,
             "What does {s} dislike?", "What do I dislike?"),
    Template("relationship", (P,), "relationship", "{s} is " + OWNER_NAME + "'s {v}",
             _v(RELATIONS), True, "How is {s} related to me?"),
    Template("pet", (P, O), "pet", "{s} has {v}", _pets, False,
             "Does {s} have a pet?", "Do I have any pets?"),
    Template("contact_info", (P,), "contact_info", "{s}'s email address is {v}",
             lambda: (), True, "What is {s}'s email address?"),
    Template("health", (P, O), "health", "{s} lives with {v}", _v(HEALTH), False,
             "Does {s} have any health issues?", "Do I have any health issues?"),
    Template("plans", (P, O), "plans", "{s} plans to {v}", _v(PLANS), False,
             "What does {s} plan to do?", "What are my plans?"),
    Template("owns", (P, O), "owns", "{s} owns {v}", _v(OWNED), False,
             "What does {s} own?", "What do I own?"),
    Template("decision", (O,), "decision", "{s} decided to {v}", _v(DECISIONS), False,
             "", "What did I decide?"),
    Template("deadline", (PR,), "deadline", "{s} is due on {v}", _due, True,
             "When is {s} due?"),
    Template("status", (PR,), "project_status", "{s} is in the {v} phase", _v(PHASES), True,
             "What is the status of {s}?"),
    Template("choice", (PR,), "decision", "{s} will use {v}", _v(TECH), False,
             "What was decided for {s}?"),
    Template("lead", (PR,), "other", "{s} is led by {v}", lambda: (), True,
             "Who leads {s}?"),
    Template("event", (PR,), "plans", "{s} has {v} on the calendar", _month_plans, False,
             "What is planned for {s}?"),
    Template("kept", (T,), "other", "The {s} is kept in {v}", _v(PLACES), True,
             "Where is the {s} kept?"),
    Template("upkeep", (T,), "plans", "The {s} needs {v}", _month_services, False,
             "What does the {s} need?"),
    Template("gift", (T,), "other", "The {s} was a gift from {v}", lambda: (), True,
             "Who gave me the {s}?"),
)
BY_KEY = {t.key: t for t in TEMPLATES}

# How each belief ends up, with its share of the corpus. "confirmed" fates are the ones
# memory and retrieval expect.
FATES = (
    ("source", 0.42, "confirmed"),      # the module confirms it (3+ times on 2+ days)
    ("two_sources", 0.08, "confirmed"),  # once in a conversation, once in a note
    ("rule", 0.05, "confirmed"),         # two module ids, 3 sightings on 2 days
    ("corrected", 0.03, "confirmed"),    # said once, the owner corrects it
    ("owner", 0.02, "confirmed"),        # said once, the owner confirms it
    ("aged", 0.05, "stale"),             # module-confirmed, last heard over 180 days ago
    ("gone", 0.03, "stale"),             # module-confirmed, the module stops listing it
    ("rejected", 0.04, "rejected"),      # module-confirmed, the owner forgets it
    ("once", 0.28, "tentative"),         # said once (a conversation or a note)
)
FATE_STATUS = {name: status for name, _share, status in FATES}


@dataclass
class Subject:
    idx: int
    kind: str  # owner, person, project, thing
    name: str
    aliases: list[str] = field(default_factory=list)
    person_id: str | None = None

    @property
    def about(self) -> str:
        return {"owner": "owner", "person": "person"}.get(self.kind, "other")


@dataclass
class Item:
    """One planned belief: its text, structure, fate, and the evidence that makes it."""

    idx: int
    subject: Subject
    template: str
    value: str
    text: str
    fate: str
    corrected_value: str = ""
    corrected_text: str = ""
    evidence: list = field(default_factory=list)  # filled by plan_evidence

    @property
    def shown_text(self) -> str:
        return self.corrected_text or self.text

    @property
    def shown_value(self) -> str:
        return self.corrected_value or self.value

    @property
    def expected_status(self) -> str:
        return FATE_STATUS[self.fate]


@dataclass
class Query:
    kind: str  # name, attribute, alias, owner
    text: str
    expected: list[int]  # item indexes (mapped to belief ids after seeding)


@dataclass
class Corpus:
    size: Size
    seed: int
    subjects: list[Subject]
    items: list[Item]
    queries: list[Query]

    def digest(self) -> str:
        h = hashlib.sha256()
        for s in self.subjects:
            h.update(f"{s.idx}|{s.kind}|{s.name}|{','.join(s.aliases)}\n".encode())
        for i in self.items:
            h.update(f"{i.idx}|{i.subject.idx}|{i.template}|{i.shown_text}|{i.fate}|"
                     f"{json.dumps(i.evidence, sort_keys=True)}\n".encode())
        for q in self.queries:
            h.update(f"{q.kind}|{q.text}|{q.expected}\n".encode())
        return h.hexdigest()[:16]


# --------------------------------------------------------------------- generation
def _day(rng: random.Random, window: tuple[date, date]) -> date:
    return window[0] + timedelta(days=rng.randrange((window[1] - window[0]).days + 1))


def make_subjects(size: Size, rng: random.Random) -> list[Subject]:
    subjects = [Subject(0, "owner", OWNER_NAME, person_id="p0")]
    taken = {OWNER_NAME.lower()}
    names = [f"{f} {last}" for f in FIRST for last in LAST]
    rng.shuffle(names)
    for name in names:
        if len(subjects) > size.people:
            break
        if name.lower() in taken:
            continue
        taken.add(name.lower())
        subjects.append(Subject(len(subjects), "person", name,
                                person_id=f"p{len(subjects)}"))
    projects = [f.format(w=w) for f in PROJECT_FORMS for w in PROJECT_WORDS]
    rng.shuffle(projects)
    for name in projects[:size.projects]:
        taken.add(name.lower())
        subjects.append(Subject(len(subjects), "project", name))
    things = [f"{a.capitalize()} {n}" for a in THING_ADJ for n in THING_NOUN]
    rng.shuffle(things)
    for name in things[:size.things]:
        taken.add(name.lower())
        subjects.append(Subject(len(subjects), "thing", name))
    if len(subjects) != size.subjects + 1:
        raise ValueError("the vocabulary is too small for this size")
    # Aliases: a nickname plus last name for some people (never another subject's name).
    for s in subjects:
        if s.kind != "person":
            continue
        first, last = s.name.split(" ", 1)
        if first in NICK and rng.random() < 0.6:
            alias = f"{NICK[first]} {last}"
            if alias.lower() not in taken:
                taken.add(alias.lower())
                s.aliases.append(alias)
    return subjects


def _per_subject(size: Size, subjects: list[Subject], rng: random.Random) -> dict[int, int]:
    counts = {s.idx: 0 for s in subjects}
    counts[0] = size.owner_beliefs
    people = [s.idx for s in subjects if s.kind == "person"]
    for s in subjects:
        if s.kind == "project":
            counts[s.idx] = PER_PROJECT
        elif s.kind == "thing":
            counts[s.idx] = PER_THING
    rest = size.beliefs - sum(counts.values())
    if rest < len(people):
        raise ValueError("too few beliefs for this many people")
    for idx in people:
        counts[idx] = 1
    for _ in range(rest - len(people)):
        counts[rng.choice(people)] += 1
    return counts


def _values(t: Template, s: Subject, subjects: list[Subject]) -> tuple[str, ...]:
    if t.key == "contact_info":
        first, last = s.name.split(" ", 1)
        return (f"{first.lower()}.{last.lower()}@example.org",
                f"{first.lower()}{len(last)}@example.net")
    if t.key in ("lead", "gift"):  # six people, fixed per subject
        people = [p.name for p in subjects if p.kind == "person"]
        return tuple(people[(s.idx * 7 + n * 13) % len(people)] for n in range(6))
    return t.values()


def make_items(size: Size, subjects: list[Subject], rng: random.Random) -> list[Item]:
    counts = _per_subject(size, subjects, rng)
    names, shares = zip(*[(n, w) for n, w, _ in FATES])
    items: list[Item] = []
    for s in subjects:
        templates = [t for t in TEMPLATES if s.kind in t.kinds]
        used: dict[str, set[str]] = {t.key: set() for t in templates}
        for _ in range(counts[s.idx]):
            open_ = [t for t in templates
                     if not (t.single and used[t.key])
                     and len(used[t.key]) < len(_values(t, s, subjects)) - 1]
            if not open_:
                break
            t = rng.choice(open_)
            pool = [v for v in _values(t, s, subjects) if v not in used[t.key]]
            value = rng.choice(pool)
            used[t.key].add(value)
            fate = rng.choices(names, weights=shares)[0]
            item = Item(len(items), s, t.key, value, t.text.format(s=s.name, v=value), fate)
            if fate == "corrected":
                fix = [v for v in pool if v != value and v not in used[t.key]]
                item.corrected_value = rng.choice(fix)
                used[t.key].add(item.corrected_value)
                item.corrected_text = t.text.format(s=s.name, v=item.corrected_value)
            items.append(item)
    if len(items) < size.beliefs:
        raise ValueError("could not place every belief; enlarge the vocabulary")
    return items


def module_link(fact_id: int) -> str:
    return f"http://127.0.0.1:8090/view/c{fact_id}?sig=scale#t={fact_id % 97}"


def note_link(note_id: str) -> str:
    return f"/#open=notes&note={note_id}"


def plan_evidence(items: list[Item], rng: random.Random) -> None:
    """The evidence each item gets, as plain dicts (``source``, ``id``, ``subject``,
    ``text``, ``first``, ``last``, ``times``, ``days``, ``confirmed``, ``present``).
    Module fact ids are 100000+, note ids ``scale-note-<n>``."""
    fact = 100000
    note = 0

    def module(item: Item, text: str, first: date, last: date, times: int, days: int,
               confirmed: bool, present: bool = True, alias: bool = False) -> dict:
        nonlocal fact
        fact += 1
        s = item.subject
        name = s.name
        if alias and s.aliases and rng.random() < 0.25:
            name = s.aliases[0]  # the module heard the nickname
            text = text.replace(s.name, name)
        return {"source": MODULE, "id": str(fact), "subject": name, "text": text,
                "first": first.isoformat(), "last": last.isoformat(), "times": times,
                "days": days, "confirmed": confirmed, "present": present}

    def notes(item: Item, text: str, day: date) -> dict:
        nonlocal note
        note += 1
        return {"source": NOTES, "id": f"scale-note-{note}", "subject": item.subject.name,
                "text": text, "first": day.isoformat(), "last": day.isoformat(), "times": 1,
                "days": 1, "confirmed": False, "present": True}

    for item in items:
        fate, text = item.fate, item.text
        if fate in ("source", "gone", "rejected", "aged"):
            window = OLD if fate == "aged" else RECENT
            last = _day(rng, window)
            first = max(window[0], last - timedelta(days=rng.randrange(1, 40)))
            days = rng.randrange(2, 5)
            item.evidence = [module(item, text, first, last, rng.randrange(3, 7), days, True,
                                    present=fate != "gone", alias=True)]
        elif fate == "two_sources":
            d1 = _day(rng, RECENT)
            d2 = min(RECENT[1], d1 + timedelta(days=rng.randrange(1, 30)))
            item.evidence = [module(item, text, d1, d1, 1, 1, False),
                             notes(item, text.rstrip(".") + ".", d2)]
        elif fate == "rule":  # two different days, so the rule's day count is met
            d1 = _day(rng, (RECENT[0], RECENT[1] - timedelta(days=1)))
            d2 = min(RECENT[1], d1 + timedelta(days=rng.randrange(1, 30)))
            item.evidence = [module(item, text, d1, d1, 2, 1, False),
                             module(item, text.lower() + ".", d2, d2, 1, 1, False)]
        else:  # once, corrected, owner: one sighting
            d1 = _day(rng, RECENT)
            item.evidence = [notes(item, text, d1) if rng.random() < 0.2
                             else module(item, text, d1, d1, 1, 1, False, alias=True)]


def make_queries(size: Size, subjects: list[Subject], items: list[Item],
                 rng: random.Random) -> list[Query]:
    """The fixed question set: about the owner (one per attribute the owner has confirmed),
    by alias (about 15 %), by subject name (about 25 %), the rest by attribute."""
    confirmed = [i for i in items if i.expected_status == "confirmed"]
    by_subject: dict[int, list[Item]] = {}
    by_template: dict[tuple[int, str], list[Item]] = {}
    for i in confirmed:
        by_subject.setdefault(i.subject.idx, []).append(i)
        by_template.setdefault((i.subject.idx, i.template), []).append(i)
    queries: list[Query] = []
    for t in TEMPLATES:
        hits = by_template.get((0, t.key))
        if hits and t.owner_question:
            queries.append(Query("owner", t.owner_question, [i.idx for i in hits]))
    if by_subject.get(0):
        queries.append(Query("owner", "What do you know about me?",
                             [i.idx for i in by_subject[0]]))
    n_alias = round(size.queries * 0.15)
    n_name = round(size.queries * 0.25)
    aliased = [s for s in subjects if s.aliases and by_subject.get(s.idx)]
    for s in rng.sample(aliased, min(n_alias, len(aliased))):
        alias = s.aliases[0]
        own = by_subject[s.idx]
        if rng.random() < 0.5:
            queries.append(Query("alias", f"What do you know about {alias}?",
                                 [i.idx for i in own]))
        else:
            pick = rng.choice(own)
            t = BY_KEY[pick.template]
            queries.append(Query("alias", t.question.format(s=alias),
                                 [i.idx for i in by_template[(s.idx, t.key)]]))
    named = [s for s in subjects if s.idx and by_subject.get(s.idx)]
    for s in rng.sample(named, min(n_name, len(named))):
        queries.append(Query("name", f"What do you know about {s.name}?",
                             [i.idx for i in by_subject[s.idx]]))
    pairs = sorted(k for k in by_template if k[0])
    rng.shuffle(pairs)
    for subject_idx, key in pairs:
        if len(queries) >= size.queries:
            break
        s = subjects[subject_idx]
        queries.append(Query("attribute", BY_KEY[key].question.format(s=s.name),
                             [i.idx for i in by_template[(subject_idx, key)]]))
    return queries[:size.queries]


def build_corpus(size: Size, seed: int = SEED) -> Corpus:
    """The whole fictional corpus for ``size``; the same seed gives the same corpus."""
    rng = random.Random(seed)
    subjects = make_subjects(size, rng)
    items = make_items(size, subjects, rng)
    plan_evidence(items, rng)
    queries = make_queries(size, subjects, items, rng)
    return Corpus(size, seed, subjects, items, queries)


# ---------------------------------------------------------------------- the fake model
def _template_patterns() -> list[tuple[re.Pattern, Template]]:
    """One anchored pattern per template, the most specific (longest fixed text) first,
    so "{s} has {v} on the calendar" wins over "{s} has {v}"."""
    out = []
    for t in sorted(TEMPLATES, key=lambda t: -len(t.text.replace("{s}", "").replace("{v}", ""))):
        pattern = re.escape(t.text).replace(re.escape("{s}"), r"(?P<s>.+?)").replace(
            re.escape("{v}"), r"(?P<v>.+?)")
        out.append((re.compile(rf"^{pattern}\.?$", re.IGNORECASE), t))
    return out


PATTERNS = _template_patterns()


def classify(text: str) -> tuple[str, str]:
    """(attribute, value) for a corpus text, as the fake model answers it."""
    for pattern, t in PATTERNS:
        m = pattern.match(text.strip())
        if m:
            return t.attribute, m.group("v")
    return "other", text


async def fake_llm(messages: list[dict]) -> str:
    """The Belief Structure task's model, deterministic: each fact's attribute and value
    from the template its text was made with."""
    body = messages[-1]["content"]
    rows = [json.loads(line) for line in body.split("<<<\n", 1)[1].rsplit("\n>>>", 1)[0]
            .splitlines() if line.strip()]
    items = []
    for row in rows:
        attribute, value = classify(row["text"])
        items.append({"n": row["n"], "attribute": attribute, "value": value,
                      "valid_from": None, "valid_to": None})
    return json.dumps({"items": items})


# ---------------------------------------------------------------------- measuring
@dataclass
class Check:
    name: str
    expected: str
    actual: str

    @property
    def ok(self) -> bool:
        return self.expected == self.actual


class SetupError(RuntimeError):
    """The data dir already holds beliefs for the owner."""


class Timer:
    def __init__(self) -> None:
        self.ms: dict[str, float] = {}

    @contextmanager
    def __call__(self, name: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.ms[name] = round((time.perf_counter() - start) * 1000, 1)


def _evidence(ledger, item: Item, ev: dict):
    s = item.subject
    return ledger.Evidence(
        source=ev["source"], source_id=ev["id"], subject=ev["subject"], about=s.about,
        text=ev["text"],
        link=module_link(int(ev["id"])) if ev["source"] == MODULE else note_link(ev["id"]),
        seen_first=ev["first"], seen_last=ev["last"], times=ev["times"], days=ev["days"],
        source_confirmed=ev["confirmed"],
        person_id=s.person_id if ev["source"] == MODULE else None)


def _chunks(items: list, n: int = CHUNK) -> Iterator[list]:
    for start in range(0, len(items), n):
        yield items[start:start + n]


def _per_1000(ms: float, n: int) -> float:
    return round(ms * 1000 / n, 1) if n else 0.0


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile (``p`` in 0..100)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def submit_timed(ledger, owner: str, evs: list, timer_key: str, results: dict) -> list[float]:
    """Submit in chunks of 1000 with ``mirror=False``; returns ms per chunk."""
    per_chunk = []
    for chunk in _chunks(evs):
        start = time.perf_counter()
        ledger.submit(owner, chunk, now=NOW, mirror=False)
        per_chunk.append(round((time.perf_counter() - start) * 1000, 1))
    total = sum(per_chunk)
    results[timer_key] = {"items": len(evs), "ms": round(total, 1),
                          "ms_per_1000": _per_1000(total, len(evs)),
                          "chunks_ms": per_chunk}
    return per_chunk


def seed_and_measure(corpus: Corpus, owner: str, checks: list[Check]) -> dict:
    """Seed the corpus into the current DATA_DIR and measure the ledger. Returns the
    results dict (``ledger``, ``timings``, ``memory``) and the item -> belief id map
    under ``_ids``."""
    from src import constants
    from src import evidence_intake as ledger

    if ledger.stats(owner)["total"]:
        raise SetupError(f"{constants.DATA_DIR} already has beliefs for owner {owner!r}; use "
                         "a fresh --data-dir or --owner")
    timer = Timer()
    timings: dict = {}
    people = [{"id": s.person_id, "name": s.name, "aliases": s.aliases,
               "is_owner": s.kind == "owner"}
              for s in corpus.subjects if s.person_id]
    with timer("sync_people"):
        ledger.sync_people(owner, MODULE, people, now=NOW, mirror=False)

    module_evs, note_evs, present = [], [], set()
    for item in corpus.items:
        for ev in item.evidence:
            e = _evidence(ledger, item, ev)
            (module_evs if ev["source"] == MODULE else note_evs).append(e)
            if ev["source"] == MODULE and ev["present"]:
                present.add(ev["id"])
    submit_timed(ledger, owner, module_evs, "submit_new", timings)
    submit_timed(ledger, owner, note_evs, "submit_notes", timings)
    with timer("reconcile_source"):
        reconcile = ledger.reconcile_source(owner, MODULE, present, now=NOW, mirror=False)
    with timer("apply_decay"):
        decay = ledger.apply_decay(owner, now=NOW, mirror=False)
    with timer("sync_memory_first"):
        first_sync = ledger.sync_memory(owner, now=NOW)

    # item -> belief id, through the evidence the item submitted first
    by_source = {(e["source"], e["source_id"]): b["id"]
                 for b in ledger.list_beliefs(owner, limit=10**7) for e in b["evidence"]}
    ids = {i.idx: by_source.get((i.evidence[0]["source"], i.evidence[0]["id"]))
           for i in corpus.items}

    # The owner's decisions, one call each (each mirrors memory, as on the review page).
    actions = {"corrected": [], "rejected": [], "owner": []}
    for item in corpus.items:
        if item.fate not in actions or ids[item.idx] is None:
            continue
        start = time.perf_counter()
        if item.fate == "corrected":
            ledger.correct(owner, ids[item.idx], item.corrected_text, now=NOW)
        elif item.fate == "rejected":
            ledger.reject(owner, ids[item.idx], "scale eval", now=NOW)
        else:
            ledger.confirm(owner, ids[item.idx], now=NOW)
        actions[item.fate].append((time.perf_counter() - start) * 1000)
    all_actions = [ms for v in actions.values() for ms in v]
    timings["owner_actions"] = {
        "count": len(all_actions),
        "ms_mean": round(statistics.fmean(all_actions), 1) if all_actions else 0.0,
        "ms_p95": round(percentile(all_actions, 95), 1),
        "by_kind": {k: len(v) for k, v in actions.items()}}

    # The hourly facts pass with nothing new: the same module items again.
    resubmit = [e for e in module_evs if e.source_id in present]
    submit_timed(ledger, owner, resubmit, "submit_unchanged", timings)
    with timer("reconcile_unchanged"):
        ledger.reconcile_source(owner, MODULE, present, now=NOW, mirror=False)
    with timer("apply_decay_unchanged"):
        ledger.apply_decay(owner, now=NOW, mirror=False)
    memory_path = Path(constants.DATA_DIR) / "memory.json"
    with timer("sync_memory_settle"):
        ledger.sync_memory(owner, now=NOW)
    before = memory_path.read_bytes()
    with timer("sync_memory_second"):
        second = ledger.sync_memory(owner, now=NOW)
    checks.append(Check("second sync_memory writes nothing", "0/0/0",
                        f"{second['added']}/{second['updated']}/{second['removed']}"))
    checks.append(Check("second sync_memory: memory.json byte-identical", "True",
                        str(memory_path.read_bytes() == before)))

    with timer("list_beliefs_all"):
        everything = ledger.list_beliefs(owner, limit=10**7)
    with timer("list_beliefs_confirmed"):
        confirmed = ledger.list_beliefs(owner, status="confirmed", limit=10**7)
    with timer("list_beliefs_page"):
        ledger.list_beliefs(owner, status=["tentative", "confirmed", "stale"])
    with timer("stats"):
        st = ledger.stats(owner)
    with timer("list_subjects"):
        subjects = ledger.list_subjects(owner)
    some = next((s for s in subjects if s["kind"] == "person"), subjects[0])
    with timer("list_beliefs_subject"):
        ledger.list_beliefs(owner, subject_id=some["id"])

    status_of = {b["id"]: b["status"] for b in everything}
    wrong = [i.idx for i in corpus.items if status_of.get(ids[i.idx]) != i.expected_status]
    checks.append(Check("every belief ends in its planned status", "0 wrong",
                        f"{len(wrong)} wrong"))
    checks.append(Check("one belief per planned item", str(len(corpus.items)),
                        str(len(set(ids.values()) - {None}))))
    checks.append(Check("subjects (owner + people + projects + things)",
                        str(corpus.size.subjects + 1), str(len(subjects))))
    checks.append(Check("projects inferred from their names", str(corpus.size.projects),
                        str(sum(s["kind"] == "project" for s in subjects))))
    memories = [m for m in json.loads(memory_path.read_text(encoding="utf-8"))
                if (m.get("owner") or "") == owner and "belief_id" in m]
    checks.append(Check("one memory per confirmed belief", str(len(confirmed)),
                        str(len(memories))))

    for key in ("sync_people", "reconcile_source", "apply_decay", "sync_memory_first",
                "reconcile_unchanged", "apply_decay_unchanged", "sync_memory_settle",
                "sync_memory_second", "list_beliefs_all", "list_beliefs_confirmed",
                "list_beliefs_page", "list_beliefs_subject", "stats", "list_subjects"):
        timings[key] = {"ms": timer.ms[key]}
    db = ledger.db_path()
    db_bytes = sum(p.stat().st_size for p in (db, Path(f"{db}-wal")) if p.exists())
    return {
        "ledger": {
            "beliefs": st["total"], **{k: st[k] for k in ledger.STATUSES},
            "evidence": sum(len(b["evidence"]) for b in everything),
            "subjects": len(subjects),
            "db_bytes": db_bytes,
            "first_pass": {"gone": reconcile["gone"], "stale_on_reconcile": reconcile["stale"],
                           "stale_by_decay": decay["stale"],
                           "memories_added": first_sync["added"]},
        },
        "timings": timings,
        "memory": {"entries": len(memories), "bytes": memory_path.stat().st_size},
        "_ids": ids,
    }


def measure_structure(corpus: Corpus, owner: str, ids: dict[int, int | None],
                      checks: list[Check]) -> dict:
    """The Belief Structure task over every confirmed belief, with the fake model."""
    from src import belief_structure
    from src import evidence_intake as ledger

    todo = len(ledger.structure_candidates(owner, limit=10**7))
    calls = 0

    async def counting(messages: list[dict]) -> str:
        nonlocal calls
        calls += 1
        return await fake_llm(messages)

    start = time.perf_counter()
    result = asyncio.run(belief_structure.run(owner, llm=counting, now=NOW, limit=max(1, todo)))
    ms = (time.perf_counter() - start) * 1000
    by_id = {b["id"]: b for b in ledger.list_beliefs(owner, status="confirmed", limit=10**7)}
    planned = 0
    for item in corpus.items:
        b = by_id.get(ids[item.idx])
        if b is None:
            continue
        t = BY_KEY[item.template]
        planned += (b["attribute"] == t.attribute and b["value"] == item.shown_value)
    checks.append(Check("structure: every confirmed belief structured", str(todo),
                        str(result["structured"])))
    checks.append(Check("structure: attribute and value as planned", str(todo), str(planned)))
    runs = math.ceil(todo / belief_structure.MAX_PER_RUN) if todo else 0
    return {"candidates": todo, "structured": result["structured"], "other": result["other"],
            "as_planned": planned, "waiting": result["waiting"], "failed": result["failed"],
            "batches": calls, "ms": round(ms, 1),
            "ms_per_batch": round(ms / calls, 1) if calls else 0.0,
            "hourly_runs_to_finish": runs}


# ------------------------------------------------------------------------ retrieval
# Names the retrieval module may use for its embedding lane (``embedder()`` in
# src/belief_search.py: the hub's local lane, or None); the keyword-only run replaces each
# one that exists with a stand-in that answers None, "no lane".
EMBEDDING_HOOKS = ("embedder", "_embedder", "get_embedder", "_get_embedder", "embed_query",
                   "_embed_query")


def load_search():
    """``src.belief_search`` or the reason it can't be imported."""
    try:
        from src import belief_search
    except Exception as e:  # noqa: BLE001  (ImportError, or a broken module)
        return None, f"{type(e).__name__}: {e}"
    if not callable(getattr(belief_search, "search_beliefs", None)):
        return None, "src.belief_search has no search_beliefs"
    return belief_search, ""


@contextmanager
def keyword_only(module) -> Iterator[list[str]]:
    """Force the keyword fallback: the module's own switch when it has one
    (``force_keyword(bool)`` or ``KEYWORD_ONLY``), else every embedding hook in
    :data:`EMBEDDING_HOOKS` is replaced by one that answers None (no lane).
    Yields the names it changed (empty: nothing could be forced)."""
    saved: dict[str, object] = {}
    changed: list[str] = []
    if callable(getattr(module, "force_keyword", None)):
        module.force_keyword(True)
        changed.append("force_keyword")
    elif hasattr(module, "KEYWORD_ONLY"):
        saved["KEYWORD_ONLY"] = module.KEYWORD_ONLY
        module.KEYWORD_ONLY = True
        changed.append("KEYWORD_ONLY")
    else:
        def off(*_a, **_k):
            return None  # no embedding lane

        for name in EMBEDDING_HOOKS:
            if hasattr(module, name):
                saved[name] = getattr(module, name)
                setattr(module, name, off if callable(saved[name]) else None)
                changed.append(name)
    try:
        yield changed
    finally:
        if "force_keyword" in changed:
            module.force_keyword(False)
        for name, value in saved.items():
            setattr(module, name, value)


def semantic_used(module, owner: str, corpus: Corpus) -> bool | None:
    """Whether the module ranked with an embedding lane (``search_details(...)
    ["semantic"]`` for the first query), or None when it doesn't say."""
    details = getattr(module, "search_details", None)
    if not callable(details) or not corpus.queries:
        return None
    try:
        return bool(details(owner, corpus.queries[0].text, k=1).get("semantic"))
    except Exception:  # noqa: BLE001
        return None


def run_queries(search, owner: str, corpus: Corpus, ids: dict[int, int | None],
                confirmed: set[int], k: int = 10) -> dict:
    """Ask every query; top-1 (first hit expected), top-5 recall (expected among the
    first five, out of min(5, expected)), latency, and leaks (hits that aren't
    confirmed beliefs)."""
    latencies, top1, top5, leaks = [], [], [], 0
    per_kind: dict[str, list[float]] = {}
    misses = []
    for q in corpus.queries:
        expected = {ids[i] for i in q.expected if ids[i] is not None}
        start = time.perf_counter()
        hits = search(owner, q.text, k=k, status=("confirmed",))
        latencies.append((time.perf_counter() - start) * 1000)
        got = [h.get("id") for h in hits or []]
        leaks += sum(1 for h in got if h not in confirmed)
        first = 1.0 if got[:1] and got[0] in expected else 0.0
        recall = len(set(got[:5]) & expected) / min(5, len(expected)) if expected else 0.0
        top1.append(first)
        top5.append(recall)
        per_kind.setdefault(q.kind, []).append(recall)
        if recall < 1.0 and len(misses) < 10:
            misses.append({"query": q.text, "recall5": round(recall, 2)})
    return {
        "queries": len(corpus.queries),
        "top1": round(statistics.fmean(top1), 3) if top1 else 0.0,
        "top5": round(statistics.fmean(top5), 3) if top5 else 0.0,
        "top5_by_kind": {k: round(statistics.fmean(v), 3) for k, v in sorted(per_kind.items())},
        "p50_ms": round(percentile(latencies, 50), 1),
        "p95_ms": round(percentile(latencies, 95), 1),
        "leaks": leaks,
        "misses": misses,
    }


def measure_retrieval(corpus: Corpus, owner: str, ids: dict[int, int | None],
                      min_top5: float, checks: list[Check]) -> dict:
    from src import evidence_intake as ledger

    module, why = load_search()
    if module is None:
        return {"status": "skipped",
                "reason": f"src/belief_search.py is not importable yet ({why}); the "
                          "retrieval cases run once stage 2.3 is merged"}
    confirmed = {b["id"] for b in ledger.list_beliefs(owner, status="confirmed", limit=10**7)}
    lanes = {"default": run_queries(module.search_beliefs, owner, corpus, ids, confirmed)}
    lanes["default"]["semantic"] = semantic_used(module, owner, corpus)
    with keyword_only(module) as changed:
        lanes["keyword"] = run_queries(module.search_beliefs, owner, corpus, ids, confirmed)
        lanes["keyword"]["semantic"] = semantic_used(module, owner, corpus)
    lanes["keyword"]["forced_by"] = changed or ["nothing: no embedding hook found"]
    checks.append(Check(f"retrieval: top-5 recall >= {min_top5}", "True",
                        str(lanes["default"]["top5"] >= min_top5)))
    checks.append(Check("retrieval: no hit that isn't a confirmed belief", "0",
                        str(lanes["default"]["leaks"] + lanes["keyword"]["leaks"])))
    return {"status": "ran", "min_top5": min_top5, "lanes": lanes}


# ---------------------------------------------------------------------------- run
def run_scale(data_dir: str | Path, size: str = "medium", owner: str = OWNER,
              seed: int = SEED, min_top5: float = 0.9) -> tuple[dict, list[Check]]:
    """Build the corpus, seed it into ``data_dir`` and measure. DATA_DIR and the BELIEF_*
    settings are set for the run (defaults) and restored afterwards."""
    from src import constants

    cfg = SIZES[size]
    corpus = build_corpus(cfg, seed)
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    saved_dir = constants.DATA_DIR
    saved_env = {k: os.environ.pop(k) for k in SETTINGS_ENV if k in os.environ}
    constants.DATA_DIR = str(data_dir)
    checks: list[Check] = []
    try:
        results = seed_and_measure(corpus, owner, checks)
        ids = results.pop("_ids")
        results["structure"] = measure_structure(corpus, owner, ids, checks)
        results["retrieval"] = measure_retrieval(corpus, owner, ids, min_top5, checks)
    finally:
        constants.DATA_DIR = saved_dir
        os.environ.update(saved_env)
    planned: dict[str, int] = {}
    for item in corpus.items:
        planned[item.fate] = planned.get(item.fate, 0) + 1
    results = {
        "size": size, "seed": seed, "owner": owner, "data_dir": str(data_dir),
        "corpus": {"digest": corpus.digest(), "subjects": len(corpus.subjects),
                   "beliefs": len(corpus.items), "queries": len(corpus.queries),
                   "aliases": sum(len(s.aliases) for s in corpus.subjects),
                   "fates": planned},
        **results,
        "checks": [{"name": c.name, "expected": c.expected, "actual": c.actual, "ok": c.ok}
                   for c in checks],
    }
    return results, checks


def _kb(n: int) -> str:
    return f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


def report(results: dict, checks: list[Check], out=None) -> int:
    """Print the measurements and checks; returns the number of failed checks."""
    out = out or sys.stdout
    c, lg, t = results["corpus"], results["ledger"], results["timings"]

    def row(label: str, value: str) -> None:
        print(f"  {label:<38} {value}", file=out)

    print(f"belief eval at scale: size {results['size']}, seed {results['seed']}, "
          f"corpus {c['digest']}", file=out)
    print("\nledger", file=out)
    row("subjects / aliases", f"{lg['subjects']} / {c['aliases']}")
    row("beliefs", f"{lg['beliefs']} (confirmed {lg['confirmed']}, tentative "
                   f"{lg['tentative']}, stale {lg['stale']}, rejected {lg['rejected']})")
    row("confirmed share", f"{lg['confirmed'] / max(1, lg['beliefs']):.0%}")
    row("evidence rows", str(lg["evidence"]))
    row("beliefs.db (+ wal)", _kb(lg["db_bytes"]))
    print("\ntimings", file=out)
    for key, label in (("submit_new", "submit, new evidence"),
                       ("submit_notes", "submit, notes"),
                       ("submit_unchanged", "submit, unchanged (hourly pass)")):
        s = t[key]
        chunks = s["chunks_ms"]
        trend = f", chunks {chunks[0]:.0f}→{chunks[-1]:.0f} ms" if len(chunks) > 1 else ""
        row(label, f"{s['ms_per_1000']:.0f} ms per 1000 ({s['items']} items{trend})")
    for key, label in (("sync_people", "sync_people"),
                       ("reconcile_source", "reconcile_source"),
                       ("apply_decay", "apply_decay"),
                       ("reconcile_unchanged", "reconcile_source, nothing gone"),
                       ("apply_decay_unchanged", "apply_decay, nothing due"),
                       ("list_beliefs_all", "list_beliefs, all"),
                       ("list_beliefs_confirmed", "list_beliefs, confirmed"),
                       ("list_beliefs_page", "list_beliefs, review page (500)"),
                       ("list_beliefs_subject", "list_beliefs, one subject"),
                       ("list_subjects", "list_subjects"),
                       ("stats", "stats"),
                       ("sync_memory_first", "sync_memory, first (writes all)"),
                       ("sync_memory_second", "sync_memory, second (no-op)")):
        row(label, f"{t[key]['ms']:.0f} ms")
    oa = t["owner_actions"]
    row("owner action (correct/reject/confirm)",
        f"{oa['ms_mean']:.0f} ms mean, p95 {oa['ms_p95']:.0f} ms ({oa['count']} calls)")
    m = results["memory"]
    print("\nmemory", file=out)
    row("memory.json", f"{_kb(m['bytes'])}, {m['entries']} mirrored beliefs")
    s = results["structure"]
    print("\nstructure (fake model)", file=out)
    row("structured / candidates", f"{s['structured']} / {s['candidates']} "
                                   f"({s['other']} other, {s['as_planned']} as planned)")
    row("time per batch", f"{s['ms_per_batch']:.0f} ms ({s['batches']} batches, "
                                f"{s['ms'] / 1000:.1f} s)")
    row("hourly runs to structure all (40/run)", str(s["hourly_runs_to_finish"]))
    r = results["retrieval"]
    print("\nretrieval", file=out)
    if r["status"] == "skipped":
        row("skipped", r["reason"])
    else:
        for lane, v in r["lanes"].items():
            row(f"{lane}: top-1 / top-5", f"{v['top1']:.2f} / {v['top5']:.2f} "
                                          f"({v['queries']} queries)")
            row(f"{lane}: p50 / p95", f"{v['p50_ms']:.1f} / {v['p95_ms']:.1f} ms")
            row(f"{lane}: top-5 by kind", ", ".join(f"{k} {x:.2f}"
                                                   for k, x in v["top5_by_kind"].items()))
        row("keyword lane forced by", ", ".join(r["lanes"]["keyword"]["forced_by"]))
        used = {True: "yes", False: "no (no local lane loaded: same as keyword)",
                None: "unknown"}[r["lanes"]["default"]["semantic"]]
        row("default lane used embeddings", used)
    print("\nchecks", file=out)
    for chk in checks:
        print(f"  {'ok  ' if chk.ok else 'FAIL'} {chk.name}"
              + ("" if chk.ok else f" (expected {chk.expected}, got {chk.actual})"), file=out)
    failed = sum(not chk.ok for chk in checks)
    print(f"\nchecks: {len(checks) - failed}/{len(checks)}", file=out)
    return failed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--size", choices=sorted(SIZES), default="medium",
                    help="small (30 subjects, 300 beliefs), medium (300, 3000; default), "
                         "large (1000, 10000)")
    ap.add_argument("--seed", type=int, default=SEED, help=f"corpus seed (default {SEED})")
    ap.add_argument("--data-dir", type=Path,
                    help="where to seed beliefs.db and memory.json (default: a temp dir)")
    ap.add_argument("--keep", action="store_true",
                    help="keep the temp dir (an explicit --data-dir is always kept)")
    ap.add_argument("--owner", default=OWNER, help=f"owner to seed (default {OWNER!r})")
    ap.add_argument("--min-top5", type=float, default=0.9,
                    help="pass threshold for top-5 recall when retrieval runs (default 0.9)")
    ap.add_argument("--out", type=Path,
                    help="results JSON (default data/eval/belief_eval_scale.<size>.json, "
                         "gitignored)")
    args = ap.parse_args(argv)
    temp = args.data_dir is None
    data_dir = Path(tempfile.mkdtemp(prefix="belief-scale-")) if temp else args.data_dir
    try:
        try:
            results, checks = run_scale(data_dir, args.size, args.owner, args.seed,
                                        args.min_top5)
        except SetupError as e:
            print(f"belief_eval_scale: {e}", file=sys.stderr)
            return 2
        failed = report(results, checks)
    finally:
        if temp and not args.keep:
            shutil.rmtree(data_dir, ignore_errors=True)
    out = args.out or DEFAULT_OUT / f"belief_eval_scale.{args.size}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(f"results: {out}")
    if not temp or args.keep:
        print(f"ledger kept: {data_dir / 'beliefs.db'} (owner {args.owner!r})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
