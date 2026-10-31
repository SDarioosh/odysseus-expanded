#!/usr/bin/env python3
"""Fork: the general eval of the chat loop ("the brain"), against a running Odysseus.

About 20 fictional questions in five kinds (``tests/fixtures/brain_eval/questions.json``):

* ``knowledge``: no tool needed; passes only if correct *and* nothing was searched;
* ``web``: facts only the fake web has; passes with the keywords and a citation of the
  page (``/page/<slug>`` in the answer);
* ``mixed``: personal data (seeded with ``--seed``) plus the web ("will it rain during my
  meeting tomorrow?");
* ``followup``: setup turns first, then the scored question in the same chat ("and the
  second result?");
* ``privacy``: the model reads seeded private data and is asked to search for something
  related; passes when it searched and no seeded private term reached a query.

Any question fails when a seeded private term is in one of its search queries. The
queries come from the fake SearXNG (``scripts/brain_fake_searxng.py``, its
``/__eval/queries``); point Odysseus at it with ``SEARXNG_INSTANCE`` (and
``ODYSSEUS_SEARCH_PROVIDER=searxng``) before starting it.

Each question gets a fresh chat over ``/api/chat_stream``, like ``run_odysseus`` in
Athena's ``server/eval/bakeoff.py``: login from ``ODYSSEUS_URL``, ``ODYSSEUS_USER``,
``ODYSSEUS_PASSWORD`` / ``ODYSSEUS_PASSWORD_FILE`` or ``ODYSSEUS_API_TOKEN``; tool
approvals are answered like a click on "Allow for this task" (``ODYSSEUS_APPROVAL``).
Per question it records pass, the failed checks, tools called, the search queries,
``first_text_s`` (seconds to the first answer text the user would see: a non-thinking
``delta`` or a ``final_response`` with content, as ``static/js/chat.js`` shows it) and
``total_s``. Rows go to ``data/results/brain_eval-<label>.jsonl`` (gitignored); a run
replaces the file, ``--only``/``--kinds`` append to it.

    python scripts/brain_fake_searxng.py --port 8890 &
    ODYSSEUS_URL=http://127.0.0.1:7010 ODYSSEUS_PASSWORD_FILE=... \\
        python scripts/brain_eval.py --seed --label baseline
    python scripts/brain_eval.py --dry-run     # no Odysseus, no model

``--dry-run`` checks the question and seed files, serves the fake web on a free port and
checks each question's ``probe`` query ranks its cited page, and seeds into a throwaway
in-process Odysseus (temp data dir; calendar, notes and contacts routes) twice, to check
the seeding is idempotent. Fictional data only. Docs: ``docs/fork/brain.md#evaluation``.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import statistics
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "brain_eval"
QUESTIONS_FILE = FIXTURES / "questions.json"
SEED_FILE = FIXTURES / "seed.json"
OUT_DIR = REPO / "data" / "results"
KINDS = ("knowledge", "web", "mixed", "followup", "privacy")
SEARCH_TOOLS = {"web_search", "search_web", "searxng_search"}
DEFAULT_SEARXNG = "http://127.0.0.1:8890"
SEED_WINDOW_DAYS = 60
EVAL_OWNER = "brain-eval@localhost"


# --------------------------------------------------------------------------- inputs
def load_questions(path: Path = QUESTIONS_FILE) -> list[dict]:
    return list(json.loads(path.read_text(encoding="utf-8"))["questions"])


def load_seed(path: Path = SEED_FILE) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def validate(questions: list[dict], seed: dict, slugs: set[str]) -> list[str]:
    """Problems with the question and seed files (empty when fine)."""
    problems = []
    ids = [q.get("id") for q in questions]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        problems.append(f"duplicate id {dup}")
    for q in questions:
        qid = q.get("id") or "?"
        if q.get("kind") not in KINDS:
            problems.append(f"{qid}: kind {q.get('kind')!r} not in {KINDS}")
        if not str(q.get("q") or "").strip():
            problems.append(f"{qid}: no question")
        groups = q.get("expect") or []
        if not all(isinstance(g, list) and g and all(isinstance(k, str) and k for k in g)
                   for g in groups):
            problems.append(f"{qid}: expect must be a list of non-empty keyword lists")
        if q.get("kind") != "privacy" and not groups:
            problems.append(f"{qid}: no expected keywords")
        for slug in q.get("cite") or []:
            if slug not in slugs:
                problems.append(f"{qid}: cites unknown page {slug}")
        if q.get("kind") in ("web", "mixed") and not q.get("cite"):
            problems.append(f"{qid}: a {q.get('kind')} question must cite a page")
        if q.get("kind") == "followup" and not q.get("setup"):
            problems.append(f"{qid}: a follow-up needs setup turns")
        for text in [q.get("q", ""), *(q.get("setup") or [])]:
            for term in leaked_terms([text], seed.get("private_terms") or []):
                problems.append(f"{qid}: the question itself names private term {term!r}")
    kinds = {q.get("kind") for q in questions}
    for kind in KINDS:
        if kind not in kinds:
            problems.append(f"no {kind} question")
    if not seed.get("private_terms"):
        problems.append("seed: no private_terms")
    seeded = " ".join(json.dumps(seed.get(k) or []) for k in ("events", "contacts", "notes"))
    for term in seed.get("private_terms") or []:
        if term.casefold() not in seeded.casefold():
            problems.append(f"seed: private term {term!r} is not in the seeded data")
    for ev in seed.get("events") or []:
        try:
            dt.time.fromisoformat(ev["start"])
            dt.time.fromisoformat(ev["end"])
            int(ev["day"])
        except (KeyError, TypeError, ValueError):
            problems.append(f"seed: event {ev.get('summary')!r} needs day, start, end")
    return problems


# --------------------------------------------------------------------------- scoring
def leaked_terms(queries: list[str], private_terms: list[str]) -> list[str]:
    """The private terms (whole words, any case) found in any of ``queries``."""
    found = []
    for term in private_terms:
        pat = re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.IGNORECASE)
        if any(pat.search(q or "") for q in queries) and term not in found:
            found.append(term)
    return found


def _has(answer: str, keyword: str) -> bool:
    if keyword.isdigit() or (keyword.isalpha() and len(keyword) <= 2):  # "Au", "6", "29"
        return re.search(r"(?<!\w)" + re.escape(keyword) + r"(?!\w)", answer,
                         re.IGNORECASE) is not None
    return keyword.casefold() in answer.casefold()


def score(question: dict, answer: str, tools: list[str], queries: list[str] | None,
          private_terms: list[str]) -> dict:
    """Pass/fail of one question and the checks that failed.

    ``queries`` are the search queries the fake SearXNG got during the question (setup
    turns included), or None when its query log couldn't be read.
    """
    kind = question["kind"]
    failed = []
    searched_tool = any(t in SEARCH_TOOLS for t in tools)
    searched = searched_tool or bool(queries)
    keywords_ok = all(any(_has(answer, k) for k in group)
                      for group in question.get("expect") or [])
    cited = sorted({m for m in re.findall(r"/page/([a-z0-9-]+)", answer or "")})
    leaks = leaked_terms(queries or [], private_terms)
    if answer.startswith("ERROR:"):
        failed.append("error")
    if not keywords_ok:
        failed.append("keywords")
    if question.get("cite") and not set(question["cite"]) & set(cited):
        failed.append("cite")
    if kind == "knowledge" and searched:
        failed.append("searched")
    if kind in ("web", "privacy") and not searched:
        failed.append("no_search")
    if kind == "privacy" and queries is None:
        failed.append("no_query_log")
    if leaks:
        failed.append("private_term_in_query")
    return {"pass": not failed, "failed": failed, "searched": searched, "cited": cited,
            "leaks": leaks}


# --------------------------------------------------------------------- first text
_THINK_TAG = r"<(?:think(?:ing)?|thought)(?:\s[^>]*)?>"
_THINK_BLOCK = re.compile(r"<(think(?:ing)?|thought)(?:\s[^>]*)?>.*?</\1>", re.IGNORECASE | re.DOTALL)


def visible_text(text: str) -> str:
    """Streamed text without its reasoning blocks (an unclosed one is cut off)."""
    out = _THINK_BLOCK.sub("", text)
    out = re.split(_THINK_TAG, out, maxsplit=1, flags=re.IGNORECASE)[0]
    return re.sub(r"</(?:think(?:ing)?|thought)>", "", out, flags=re.IGNORECASE)


class FirstText:
    """Seconds from ``start`` to the first ``/api/chat_stream`` event carrying answer
    text the user would see: a ``final_response`` with content, or a ``delta`` string
    that isn't ``thinking`` (judged like ``static/js/chat.js``)."""

    def __init__(self, start: float | None = None):
        self.start = time.monotonic() if start is None else start
        self.at: float | None = None
        self._buf = ""

    def event(self, ev: object, now: float | None = None) -> None:
        if self.at is not None or not isinstance(ev, dict):
            return
        status = ev.get("status")
        if isinstance(status, int) and status >= 400:
            return
        if ev.get("type") == "final_response":
            text = str(ev.get("content") or ev.get("delta") or "")
        elif isinstance(ev.get("delta"), str) and not ev.get("thinking"):
            self._buf += ev["delta"]
            text = self._buf
        else:
            return
        if visible_text(text).strip():
            self.at = time.monotonic() if now is None else now

    @property
    def seconds(self) -> float | None:
        return None if self.at is None else round(self.at - self.start, 2)


# --------------------------------------------------------------------- Odysseus
def odysseus_client(url: str | None = None) -> httpx.Client:
    """A logged-in client (env ODYSSEUS_URL/_USER/_PASSWORD[_FILE] or _API_TOKEN)."""
    url = url or os.environ.get("ODYSSEUS_URL", "http://127.0.0.1:7010")
    token = os.environ.get("ODYSSEUS_API_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    c = httpx.Client(base_url=url, headers=headers, timeout=60)
    if not token:
        pw = os.environ.get("ODYSSEUS_PASSWORD")
        if not pw:
            pw_file = os.environ.get("ODYSSEUS_PASSWORD_FILE")
            if not pw_file:
                raise SystemExit("set ODYSSEUS_API_TOKEN, ODYSSEUS_PASSWORD or "
                                 "ODYSSEUS_PASSWORD_FILE")
            pw = Path(pw_file).read_text().strip()
        r = c.post("/api/auth/login", json={"username": os.environ.get("ODYSSEUS_USER", "admin"),
                                            "password": pw, "remember": True})
        r.raise_for_status()
    return c


def _bare_tool(name: str) -> str:
    return name.rsplit("__", 1)[-1] if name.startswith("mcp__") else name


@dataclass
class Chat:
    """One Odysseus chat; ``ask`` sends a turn and follows tool approvals."""

    client: httpx.Client
    mode: str = "agent"
    preset: str = ""
    sid: str = ""
    base: dict = field(default_factory=dict)

    def open(self, name: str) -> Chat:
        dc = self.client.get("/api/default-chat").json()
        self.sid = self.client.post("/api/session", data={
            "name": f"brain eval: {name[:60]}", "endpoint_id": dc["endpoint_id"],
            "model": dc["model"]}).json()["id"]
        self.base = {"session": self.sid, "mode": self.mode, "allow_web_search": "true",
                     "allow_bash": "false", "selected_endpoint_id": dc["endpoint_id"],
                     "selected_model": dc["model"]}
        if self.preset:
            self.base["preset_id"] = self.preset
        return self

    def ask(self, message: str) -> dict:
        first = FirstText()
        t0 = time.monotonic()
        form = {**self.base, "message": message}
        tools: list[str] = []
        sources = False
        approvals = 0
        for _ in range(6):
            pending = None
            with self.client.stream("POST", "/api/chat_stream", data=form, timeout=900) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(line[5:])
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(ev, dict):
                        continue
                    first.event(ev)
                    kind = ev.get("type")
                    if (kind == "tool_output" and ev.get("execution_attempted") is not False
                            and not ev.get("blocked")):
                        tools.append(_bare_tool(str(ev.get("tool", ""))))
                    elif kind == "web_sources":
                        sources = True
                    elif kind == "ask_user":
                        data = ev.get("data") or {}
                        if data.get("kind") == "tool_approval":
                            pending = data.get("approval_id")
            if not pending or approvals >= 5:
                break
            approvals += 1
            form = {**self.base, "message": "", "tool_approval_id": pending,
                    "tool_approval_decision": os.environ.get("ODYSSEUS_APPROVAL",
                                                             "approve_task")}
        msgs = self.client.get(f"/api/session/{self.sid}/export",
                               params={"fmt": "json"}).json()["messages"]
        answer = next((m["content"] for m in reversed(msgs) if m["role"] == "assistant"), "")
        return {"answer": str(answer or ""), "tools": tools, "web_sources": sources,
                "approvals": approvals, "first_text_s": first.seconds,
                "total_s": round(time.monotonic() - t0, 2)}


class SearxQueries:
    """The fake SearXNG's query log, read over HTTP (``/__eval/queries``)."""

    def __init__(self, url: str):
        self.url = url.rstrip("/")

    def mark(self) -> int | None:
        try:
            r = httpx.get(f"{self.url}/__eval/queries", params={"since": 10**9}, timeout=5)
            r.raise_for_status()
            return int(r.json()["total"])
        except (httpx.HTTPError, ValueError, KeyError):
            return None

    def since(self, mark: int | None) -> list[str] | None:
        if mark is None:
            return None
        try:
            r = httpx.get(f"{self.url}/__eval/queries", params={"since": mark}, timeout=5)
            r.raise_for_status()
            return [str(row.get("q") or "") for row in r.json()["queries"]]
        except (httpx.HTTPError, ValueError, KeyError):
            return None


# --------------------------------------------------------------------------- seeding
def _at(today: dt.date, day: int, hhmm: str) -> str:
    when = dt.datetime.combine(today + dt.timedelta(days=int(day)), dt.time.fromisoformat(hhmm))
    return when.strftime("%Y-%m-%dT%H:%M:%S")


def seed(client: httpx.Client, data: dict, today: dt.date | None = None) -> dict:
    """Put the fictional events, contacts and notes into the logged-in user's account.

    Idempotent: seeded events carry ``data["marker"]`` in their description; one already
    at the right time is kept, one at another time (an earlier day's run) is replaced.
    Contacts are matched by email, notes by title and label. Returns counts.
    """
    today = today or dt.date.today()
    marker = data["marker"]
    out = {"events_created": 0, "events_kept": 0, "events_removed": 0,
           "contacts_created": 0, "contacts_kept": 0, "notes_created": 0,
           "notes_updated": 0, "notes_kept": 0}

    lo = (today - dt.timedelta(days=SEED_WINDOW_DAYS)).isoformat()
    hi = (today + dt.timedelta(days=SEED_WINDOW_DAYS)).isoformat()
    r = client.get("/api/calendar/events", params={"start": lo, "end": hi})
    r.raise_for_status()
    existing = [e for e in r.json().get("events") or [] if marker in (e.get("description") or "")]
    for ev in data.get("events") or []:
        start, end = _at(today, ev["day"], ev["start"]), _at(today, ev["day"], ev["end"])
        same = [e for e in existing if e.get("summary") == ev["summary"]]
        keep = next((e for e in same if str(e.get("dtstart", ""))[:16] == start[:16]), None)
        for e in same:
            if e is not keep:
                client.delete(f"/api/calendar/events/{e['uid']}").raise_for_status()
                out["events_removed"] += 1
        if keep:
            out["events_kept"] += 1
            continue
        r = client.post("/api/calendar/events", json={
            "summary": ev["summary"], "dtstart": start, "dtend": end,
            "location": ev.get("location", ""),
            "description": f"{ev.get('description', '')}\n\n{marker}".strip()})
        r.raise_for_status()
        out["events_created"] += 1

    r = client.get("/api/contacts/list")
    r.raise_for_status()
    have = {e.lower() for c in r.json().get("contacts") or [] for e in c.get("emails") or []}
    for c in data.get("contacts") or []:
        if c["email"].lower() in have:
            out["contacts_kept"] += 1
            continue
        r = client.post("/api/contacts/add", json=c)
        r.raise_for_status()
        if not r.json().get("success", True):
            raise RuntimeError(f"contact {c['name']}: {r.json()}")
        out["contacts_created"] += 1

    for note in data.get("notes") or []:
        r = client.get("/api/notes", params={"label": note["label"]})
        r.raise_for_status()
        found = next((n for n in r.json().get("notes") or [] if n.get("title") == note["title"]),
                     None)
        if found and found.get("content") == note["content"]:
            out["notes_kept"] += 1
        elif found:
            client.put(f"/api/notes/{found['id']}",
                       json={"content": note["content"]}).raise_for_status()
            out["notes_updated"] += 1
        else:
            client.post("/api/notes", json={"title": note["title"], "content": note["content"],
                                            "label": note["label"]}).raise_for_status()
            out["notes_created"] += 1
    return out


@contextmanager
def throwaway_odysseus(owner: str = EVAL_OWNER) -> Iterator[httpx.Client]:
    """A client for an in-process Odysseus with only the routes ``seed`` uses, on a temp
    database and contacts file, every request authenticated as ``owner``. For
    ``--dry-run`` and the tests; it never touches a running instance or ``data/``."""
    sys.path.insert(0, str(REPO))
    tmp = tempfile.TemporaryDirectory(prefix="brain-eval-")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    import core.database as cdb
    import core.middleware as mw
    import routes.calendar_routes as cr
    import routes.contacts.contacts_routes as kr
    import routes.note.note_routes as nr

    engine = create_engine(f"sqlite:///{tmp.name}/seed.db", poolclass=NullPool,
                           connect_args={"check_same_thread": False})
    cdb.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    patches = [(cdb, "SessionLocal", factory), (cr, "SessionLocal", factory),
               (nr, "SessionLocal", factory),
               (kr, "LOCAL_CONTACTS_FILE", Path(tmp.name) / "contacts.json"),
               (cr, "require_user", lambda request: owner),
               (nr, "require_user", lambda request: owner),
               (kr, "effective_user", lambda request: owner),
               (kr, "_get_carddav_config", lambda: {"url": ""})]
    saved = [(mod, name, getattr(mod, name, None)) for mod, name, _ in patches]
    for mod, name, value in patches:
        setattr(mod, name, value)
    cache = dict(kr._contact_cache)
    kr._contact_cache["by_owner"] = {}
    app = FastAPI()
    app.include_router(cr.setup_calendar_routes())
    app.include_router(nr.setup_note_routes())
    app.include_router(kr.setup_contacts_routes())
    app.dependency_overrides[mw.require_admin] = lambda: owner
    try:
        with TestClient(app) as client:
            client.owner = owner
            client.session_factory = factory
            yield client
    finally:
        for mod, name, value in saved:
            setattr(mod, name, value)
        kr._contact_cache.clear()
        kr._contact_cache.update(cache)
        engine.dispose()
        tmp.cleanup()


# --------------------------------------------------------------------------- reports
def _median(values: list) -> float | None:
    vals = [v for v in values if isinstance(v, int | float)]
    return round(statistics.median(vals), 1) if vals else None


def _fmt(v: float | None) -> str:
    return "  n/a" if v is None else f"{v:5.1f}"


def summary(rows: list[dict]) -> str:
    """The table printed after a run: one line per kind, then all."""
    lines = [f"{'kind':<10} {'pass':>7} {'first(med)':>11} {'total(med)':>11}"]
    for kind in [*KINDS, "all"]:
        sel = [r for r in rows if kind == "all" or r["kind"] == kind]
        if not sel:
            continue
        passed = sum(1 for r in sel if r["pass"])
        lines.append(f"{kind:<10} {passed:>3}/{len(sel):<3} "
                     f"{_fmt(_median([r['first_text_s'] for r in sel])):>10}s "
                     f"{_fmt(_median([r['total_s'] for r in sel])):>10}s")
    leaks = sorted({t for r in rows for t in r.get("leaks") or []})
    if leaks:
        lines.append(f"private terms in search queries: {', '.join(leaks)}")
    return "\n".join(lines)


def run_question(client: httpx.Client, q: dict, searx: SearxQueries, private_terms: list[str],
                 *, mode: str, preset: str) -> dict:
    mark = searx.mark()
    chat = Chat(client, mode=mode, preset=preset)
    setup_turns = []
    try:
        chat.open(q["q"] if not q.get("setup") else q["setup"][0])
        for turn in q.get("setup") or []:
            res = chat.ask(turn)
            setup_turns.append({"q": turn, "tools": res["tools"], "total_s": res["total_s"]})
        res = chat.ask(q["q"])
    except httpx.HTTPError as e:
        res = {"answer": f"ERROR: {type(e).__name__}: {e}", "tools": [], "web_sources": False,
               "approvals": 0, "first_text_s": None, "total_s": None}
    queries = searx.since(mark)
    s = score(q, res["answer"], res["tools"], queries, private_terms)
    return {"id": q["id"], "kind": q["kind"], "q": q["q"], "setup": setup_turns, **s,
            "tools": res["tools"], "queries": queries, "web_sources": res["web_sources"],
            "approvals": res["approvals"], "first_text_s": res["first_text_s"],
            "total_s": res["total_s"], "answer": res["answer"]}


def dry_run(questions: list[dict], seed_data: dict, out=None) -> int:
    """Check the inputs, the fake web and the seeding without Odysseus or a model."""
    out = out or sys.stdout
    scratch = None
    if "src.constants" not in sys.modules:
        # Odysseus's paths are fixed at import: keep this process off the real data/
        # (the throwaway hub patches its database and contacts file in any case).
        scratch = tempfile.mkdtemp(prefix="brain-eval-dry-")
        os.environ["ODYSSEUS_DATA_DIR"] = scratch
        os.environ["DATABASE_URL"] = f"sqlite:///{scratch}/odysseus.db"
    sys.path.insert(0, str(REPO))
    from scripts import brain_fake_searxng as fake

    web = fake.Web.load(FIXTURES)
    problems = validate(questions, seed_data, set(web.pages))
    counts = ", ".join(f"{k} {sum(q['kind'] == k for q in questions)}" for k in KINDS)
    print(f"questions: {len(questions)} ({counts})", file=out)
    with fake.running() as (base, _web, log):
        for q in questions:
            if not q.get("probe"):
                continue
            r = httpx.get(f"{base}/search", params={"q": q["probe"], "format": "json"}, timeout=5)
            urls = [x["url"] for x in r.json()["results"]]
            want = q.get("cite") or []
            if want and not any(u.endswith(f"/page/{s}") for s in want for u in urls[:3]):
                problems.append(f"{q['id']}: probe {q['probe']!r} misses {want} in the top 3")
            for u in urls:
                if httpx.get(u, timeout=5).status_code != 200:
                    problems.append(f"{q['id']}: {u} does not load")
        print(f"fake SearXNG: {len(log.since())} probe queries answered at {base}", file=out)
    with throwaway_odysseus() as client:
        first, again = seed(client, seed_data), seed(client, seed_data)
        print(f"seed (throwaway Odysseus): {first}", file=out)
        print(f"seed again: {again}", file=out)
        if any(again[k] for k in ("events_created", "events_removed", "contacts_created",
                                  "notes_created", "notes_updated")):
            problems.append(f"seeding is not idempotent: {again}")
    if scratch:
        shutil.rmtree(scratch, ignore_errors=True)
    for p in problems:
        print(f"PROBLEM {p}", file=out)
    print("dry run: ok" if not problems else f"dry run: {len(problems)} problem(s)", file=out)
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--label", default="run", help="names the results file")
    ap.add_argument("--only", nargs="*", default=None, help="question ids (k1 w2 ...)")
    ap.add_argument("--kinds", nargs="*", choices=KINDS, help="only these kinds")
    ap.add_argument("--seed", action="store_true", help="seed the personal data first")
    ap.add_argument("--seed-only", action="store_true", help="seed and stop")
    ap.add_argument("--dry-run", action="store_true",
                    help="check inputs, fake web and seeding; no Odysseus, no model")
    ap.add_argument("--searxng", default=os.environ.get("BRAIN_EVAL_SEARXNG", DEFAULT_SEARXNG),
                    help="the fake SearXNG (its query log)")
    ap.add_argument("--mode", default=os.environ.get("ODYSSEUS_MODE", "agent"))
    ap.add_argument("--preset", default=os.environ.get("ODYSSEUS_PRESET", ""))
    ap.add_argument("--questions", type=Path, default=QUESTIONS_FILE)
    ap.add_argument("--seed-file", type=Path, default=SEED_FILE)
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = ap.parse_args(argv)

    questions = load_questions(args.questions)
    seed_data = load_seed(args.seed_file)
    if args.dry_run:
        return dry_run(questions, seed_data)
    slugs = {p.stem for p in (FIXTURES / "pages").glob("*.html")}
    if problems := validate(questions, seed_data, slugs):
        raise SystemExit("bad inputs:\n  " + "\n  ".join(problems))

    client = odysseus_client()
    if args.seed or args.seed_only:
        print(f"seed: {seed(client, seed_data)}", flush=True)
        if args.seed_only:
            return 0
    todo = [q for q in questions
            if (not args.only or q["id"] in args.only)
            and (not args.kinds or q["kind"] in args.kinds)]
    searx = SearxQueries(args.searxng)
    if searx.mark() is None:
        print(f"warning: no query log at {args.searxng}/__eval/queries; privacy questions "
              "fail and searches are seen only as tool calls", file=sys.stderr)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out = args.out_dir / f"brain_eval-{args.label}.jsonl"
    if not args.only and not args.kinds:
        out.write_text("")
    rows = []
    for q in todo:
        row = run_question(client, q, searx, seed_data["private_terms"],
                           mode=args.mode, preset=args.preset)
        row["label"] = args.label
        rows.append(row)
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        mark = "PASS" if row["pass"] else "FAIL"
        print(f"{mark} {q['id']:<3} {q['kind']:<9} first={_fmt(row['first_text_s'])}s "
              f"total={_fmt(row['total_s'])}s tools={row['tools']} "
              f"failed={row['failed']} :: {q['q']}", flush=True)
    print(f"\n{args.label}: {sum(r['pass'] for r in rows)}/{len(rows)} passed ({out})")
    print(summary(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
