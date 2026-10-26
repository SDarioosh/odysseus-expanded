"""Fork: one ranker over the belief ledger (personal-context plan stage 2.3, retrieval).

:func:`search_beliefs` ranks the owner's beliefs (``src/evidence_intake.py``) for a query.
The Today insight (``today_insight.select_memory``), chat (``today_insight.context_block``
through :func:`chat_block`) and the review page (``GET /api/beliefs/search``,
``routes/belief_search_routes.py``) all use it. The score (0..1) adds, in this order of
weight:

1. **subject** (:data:`W_SUBJECT`): the query names the belief's subject (its name or an
   alias, case-insensitive; a person's unique first name; "I", "me", "my" for the owner
   when no one else is named);
2. **attribute** (:data:`W_ATTRIBUTE`): the query uses a word of the belief's attribute
   (:data:`SYNONYMS`: "allergic" -> ``allergy``, "lives" -> ``lives_in`` ...); an
   unstructured belief whose text has such a word gets part of it;
3. **semantic** (:data:`W_SEMANTIC`): cosine similarity of the query to the belief's text,
   through the hub's own local embedding lane (the one the memory index uses), only
   when it is loaded, healthy and local; never a remote API;
4. **keyword** (:data:`W_KEYWORD`): token Jaccard overlap, the fallback and tie-breaker.

Each weight is larger than the sum of the ones after it, so a subject match always comes
first. Ties: ``last_seen`` (newest first), then id.

Vectors are cached in ``belief_vectors`` in ``beliefs.db`` (per belief: text hash, model,
float32 vector), computed lazily for beliefs whose text changed, at most
:data:`EMBED_PER_CALL` per call (the rest on the next call); :func:`reindex` recomputes
an owner's. Docs: ``docs/fork/beliefs.md`` ("Retrieval (2.3)").
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import sys
from array import array
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlparse

from src import evidence_intake as ledger

logger = logging.getLogger(__name__)

W_SUBJECT = 0.55
W_ATTRIBUTE = 0.25
W_SEMANTIC = 0.12
W_KEYWORD = 0.08
TEXT_ATTRIBUTE = 0.6  # share of W_ATTRIBUTE for an unstructured belief whose text has the word
MIN_SIMILARITY = 0.5  # cosine below this adds nothing (local models rate unrelated text ~0.3-0.5)
EMBED_PER_CALL = 200
SCAN_LIMIT = 2000  # newest beliefs scored when the query names no subject or attribute
EMBED_BATCH = 50
CONTEXT_K = 8
CONTEXT_CHARS = 1200
CONTEXT_HEADER = ("What I know (confirmed). Use your tools for anything else, and cite the "
                  "link next to each fact you use:")

# query words -> attribute (``evidence_intake.ATTRIBUTES``)
SYNONYMS: dict[str, tuple[str, ...]] = {
    "allergy": ("allergy", "allergies", "allergic"),
    "lives_in": ("live", "lives", "living", "home", "address", "based"),
    "works_at": ("work", "works", "working", "job", "employer", "employed", "company",
                 "profession", "occupation"),
    "role": ("role", "title", "position", "job", "profession", "occupation"),
    "birthday": ("birthday", "birthdays", "born"),
    "diet": ("diet", "vegetarian", "vegan", "pescatarian", "eat", "eats", "alcohol"),
    "likes": ("like", "likes", "love", "loves", "favourite", "favorite", "enjoy", "enjoys"),
    "dislikes": ("dislike", "dislikes", "hate", "hates"),
    "relationship": ("wife", "husband", "partner", "sister", "brother", "mother", "father",
                     "son", "daughter", "friend", "colleague", "boss", "manager", "married",
                     "related", "relation", "relationship", "relative"),
    "owns": ("own", "owns"),
    "pet": ("pet", "pets", "dog", "dogs", "cat", "cats"),
    "plans": ("plan", "plans", "planning", "planned"),
    "decision": ("decide", "decides", "decided", "deciding", "decision", "decisions"),
    "routine": ("routine", "routines", "habit", "habits", "usually", "regularly"),
    "contact_info": ("phone", "email", "number", "handle"),
    "health": ("health", "medication", "condition"),
    "project_status": ("status", "progress"),
    "deadline": ("deadline", "due"),
}
# phrases read before single words (their words then count no more): "for a living" is
# a job, not a home
PHRASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (r"\b(?:do|does|did)\s+for\s+(?:a\s+)?(?:living|work)\b|\bfor\s+a\s+living\b|"
     r"\bjob\s+title\b|\bwork(?:s|ing)?\s+as\b", ("role", "works_at")),
    (r"\brelated\s+to\b|\brelationship\s+(?:with|to)\b|\bwho\s+is\s+.+?\s+to\s+me\b",
     ("relationship",)),
    (r"\bgoing\s+to\b|\bplann?(?:ing|ed)?\s+to\b", ("plans",)),
)
_PHRASES = [(re.compile(p, re.IGNORECASE), set(a)) for p, a in PHRASES]
_WORD_ATTRIBUTES: dict[str, set[str]] = {}
for _attr, _words in SYNONYMS.items():
    for _w in _words:
        _WORD_ATTRIBUTES.setdefault(_w, set()).add(_attr)

OWNER_WORDS = frozenset({"i", "me", "my", "myself", "mine"})
STOPWORDS = frozenset("""a an and are as at be been but by did do does for from had has have
he her him his how i in is it its me my of on or our she so that the their them they this
to was we were what when where which who whom why will with you your about any anything
know tell""".split())
VECTOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS belief_vectors (
    belief_id INTEGER PRIMARY KEY REFERENCES beliefs (id) ON DELETE CASCADE,
    text_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    vector BLOB NOT NULL,
    updated_at TEXT NOT NULL
)
"""


# ---------------------------------------------------------------------- embedding
class Embedder(Protocol):
    """A local text encoder: ``model`` names it (vectors of another model are redone)."""

    model: str

    def encode(self, texts: Sequence[str]) -> list[list[float]]: ...


class _LaneEmbedder:
    def __init__(self, lane: Any):
        self._lane = lane
        self.model = f"{lane.name}:{getattr(lane, 'fingerprint', '') or lane.model}"

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        return self._lane.encode(list(texts))


def _local_lane(lane: Any) -> bool:
    """Only lanes on this machine or the owner's network (never a remote API)."""
    from src.embedding_lanes import LANE_FASTEMBED

    url = str(getattr(lane, "url", "") or "")
    if getattr(lane, "name", "") == LANE_FASTEMBED or url.startswith("local://"):
        return True
    try:
        from src.private_endpoint_thinking import is_private_host

        if is_private_host(url):
            return True
        host = (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return False
    # a container name (no dot) or an mDNS name is on the owner's network
    return bool(host) and ("." not in host or host.endswith(".local"))


def embedder() -> Embedder | None:
    """The hub's embedding lane, as the memory index (``MemoryVectorStore``, built by
    ``embedding_lanes.build_embedding_lanes``) loaded it: its first healthy, local lane
    (the configured endpoint, else FastEmbed). None when the hub has no healthy index
    (then semantic scoring is skipped)."""
    ai = sys.modules.get("src.ai_interaction")
    store = getattr(ai, "_memory_vector", None) if ai is not None else None
    if store is None or not getattr(store, "healthy", False):
        return None
    for lane in getattr(store, "_lanes", None) or []:
        if getattr(lane, "healthy", False) and _local_lane(lane):
            return _LaneEmbedder(lane)
    return None


def _pack(vector: Sequence[float]) -> bytes:
    return array("f", [float(x) for x in vector]).tobytes()


def _unpack(blob: bytes) -> list[float]:
    out = array("f")
    out.frombytes(blob)
    return out.tolist()


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _embed_text(belief: dict) -> str:
    name = belief.get("subject_display") or belief.get("subject") or ""
    shown = belief.get("shown_text") or belief.get("text") or ""
    return f"{name}: {shown}" if name else shown


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _stamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _encode(emb: Embedder, texts: list[str]) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        vecs = emb.encode(texts[i:i + EMBED_BATCH])
        out.extend(list(v) for v in vecs)
    if len(out) != len(texts):
        raise RuntimeError("embedding lane returned the wrong number of vectors")
    return out


def _vectors(beliefs: list[dict], emb: Embedder,
             budget: int | None) -> tuple[dict[int, list[float]], int, int]:
    """Cached vectors for these beliefs; missing or outdated ones are computed (at most
    ``budget``, lowest ids first). Returns (vectors, embedded now, still pending)."""
    texts = {b["id"]: _embed_text(b) for b in beliefs}
    hashes = {i: _hash(t) for i, t in texts.items()}
    have: dict[int, list[float]] = {}
    with ledger._connect() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND "
                            "name = 'belief_vectors'").fetchone()
        if exists and texts:
            ids = sorted(texts)
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                for row in db.execute(
                        "SELECT belief_id, text_hash, model, vector FROM belief_vectors "
                        f"WHERE belief_id IN ({','.join('?' * len(chunk))})", chunk):
                    if row["model"] == emb.model and row["text_hash"] == hashes[row["belief_id"]]:
                        have[row["belief_id"]] = _unpack(row["vector"])
    todo = sorted(i for i in texts if i not in have)
    take = todo if budget is None else todo[:max(0, budget)]
    if take:
        fresh = _encode(emb, [texts[i] for i in take])
        stamp = _stamp()
        with ledger._WRITE:
            with ledger._connect(write=True) as db:
                db.execute(VECTOR_SCHEMA)
                db.executemany(
                    "INSERT INTO belief_vectors (belief_id, text_hash, model, vector, "
                    "updated_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT (belief_id) DO UPDATE "
                    "SET text_hash = excluded.text_hash, model = excluded.model, "
                    "vector = excluded.vector, updated_at = excluded.updated_at",
                    [(i, hashes[i], emb.model, _pack(v), stamp) for i, v in zip(take, fresh)])
        have.update(zip(take, fresh))
    return have, len(take), len(todo) - len(take)


def reindex(owner: str | None, *, emb: Embedder | None = None) -> dict[str, Any]:
    """Recompute every vector of this owner's beliefs (all but rejected; no per-call cap)
    and drop vectors of beliefs that no longer exist. ``{"embedded", "removed",
    "model"}``; ``embedded`` is 0 and ``model`` None when no local lane is available."""
    owner = owner or ""
    emb = emb if emb is not None else embedder()
    with ledger._WRITE:
        with ledger._connect(write=True) as db:
            db.execute(VECTOR_SCHEMA)
            removed = db.execute(
                "DELETE FROM belief_vectors WHERE belief_id NOT IN (SELECT id FROM beliefs) "
                "OR belief_id IN (SELECT id FROM beliefs WHERE owner = ?)", (owner,)).rowcount
    if emb is None:
        return {"embedded": 0, "removed": removed, "model": None}
    beliefs = ledger.list_beliefs(owner, status=["tentative", "confirmed", "stale"],
                                  limit=1_000_000)
    _, embedded, _ = _vectors(beliefs, emb, budget=None)
    return {"embedded": embedded, "removed": removed, "model": emb.model}


# -------------------------------------------------------------------------- query
def tokens(text: Any) -> list[str]:
    """Lowercase words without a trailing ``'s``."""
    words = re.findall(r"[a-z0-9]+(?:['’][a-z]+)?", str(text or "").lower())
    return [re.sub(r"['’]s$", "", w) for w in words]


def _keywords(text: Any) -> set[str]:
    return {w for w in tokens(text) if w not in STOPWORDS and len(w) > 1}


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def query_attributes(query: Any) -> set[str]:
    """Attributes whose phrases (:data:`PHRASES`) or words (:data:`SYNONYMS`) the query
    uses."""
    out: set[str] = set()
    text = str(query or "")
    for pattern, attrs in _PHRASES:
        if pattern.search(text):
            out |= attrs
            text = pattern.sub(" ", text)
    for w in tokens(text):
        out |= _WORD_ATTRIBUTES.get(w, set())
    return out


def _subjects(owner: str, confirmed_only: bool = False) -> list[dict]:
    """``[{id, display, kind, names}]``: light, no counts (chat calls this per message)."""
    sql = "SELECT id, display, kind, aliases FROM subjects WHERE owner = ?"
    if confirmed_only:
        sql += (" AND EXISTS (SELECT 1 FROM beliefs b WHERE b.subject_id = subjects.id "
                "AND b.status = 'confirmed')")
    with ledger._connect() as db:
        rows = db.execute(sql + " ORDER BY id", (owner,)).fetchall()
    out = []
    for r in rows:
        names = [r["display"], *ledger._aliases(r["aliases"])]
        names = [n.strip() for n in names if n and len(n.strip()) >= 2]
        if r["kind"] != "owner" and r["display"] == ledger.GENERIC_SUBJECT:
            continue
        out.append({"id": r["id"], "display": r["display"], "kind": r["kind"],
                    "names": names})
    # a person's first name, when no other subject has it
    firsts: dict[str, list[int]] = {}
    for s in out:
        if s["kind"] == "person":
            parts = s["display"].split()
            if len(parts) >= 2 and len(parts[0]) >= 3:
                firsts.setdefault(parts[0].lower(), []).append(s["id"])
    taken = {n.lower() for s in out for n in s["names"]}
    for s in out:
        parts = s["display"].split()
        if s["kind"] == "person" and len(parts) >= 2:
            first = parts[0].lower()
            if firsts.get(first) == [s["id"]] and first not in taken:
                s["names"].append(parts[0])
    return out


def _name_pattern(name: str) -> re.Pattern:
    return re.compile(r"(?<![\w])" + re.escape(name) + r"(?:['’]s)?(?![\w])", re.IGNORECASE)


def named_subjects(query: Any, subjects: list[dict]) -> set[int]:
    """Ids of the subjects the query names; the owner's for "I/me/my" when it names no
    one else."""
    text = str(query or "")
    found = {s["id"] for s in subjects
             if s["kind"] != "owner" and any(_name_pattern(n).search(text) for n in s["names"])}
    owner_ids = [s["id"] for s in subjects if s["kind"] == "owner"]
    for s in subjects:
        if s["kind"] == "owner" and s["display"] != ledger.OWNER_DISPLAY and any(
                _name_pattern(n).search(text) for n in s["names"]):
            found.add(s["id"])
    if not found and owner_ids and OWNER_WORDS & set(tokens(text)):
        found.update(owner_ids)
    return found


_LIGHT = ("SELECT b.id, b.subject, b.text, b.owner_text, b.attribute, b.last_seen, "
          "b.subject_id, s.display AS subject_display, s.kind AS subject_kind "
          "FROM beliefs b LEFT JOIN subjects s ON s.id = b.subject_id")


def _pool(owner: str, statuses: list[str] | None, subject_id: int | None,
          attribute: str | None, kinds: list[str] | None, subjects: set[int],
          attrs: set[str]) -> list[dict]:
    """The beliefs worth scoring, as light rows (no evidence): the named subjects'
    beliefs when the query names any; else those with a queried attribute and the
    unstructured ones; else the newest :data:`SCAN_LIMIT`. The filters always apply."""
    where, args = ["b.owner = ?"], [owner]
    if statuses:
        where.append(f"b.status IN ({','.join('?' * len(statuses))})")
        args += statuses
    if subject_id is not None:
        where.append("b.subject_id = ?")
        args.append(ledger._int(subject_id, -1))
    if attribute is not None:
        where.append("b.attribute = ?")
        args.append(attribute)
    if kinds is not None:
        where.append(f"s.kind IN ({','.join('?' * len(kinds)) or 'NULL'})")
        args += kinds
    limit = ""
    if subjects:
        where.append(f"b.subject_id IN ({','.join('?' * len(subjects))})")
        args += sorted(subjects)
    elif attrs:
        where.append(f"(b.attribute IN ({','.join('?' * len(attrs))}) OR b.attribute IS NULL "
                     "OR b.attribute = 'other')")
        args += sorted(attrs)
    else:
        limit = f" LIMIT {int(SCAN_LIMIT)}"
    with ledger._connect() as db:
        rows = db.execute(f"{_LIGHT} WHERE {' AND '.join(where)} "
                          f"ORDER BY b.last_seen DESC, b.id DESC{limit}", args).fetchall()
    return [{**dict(r), "shown_text": r["owner_text"] or r["text"]} for r in rows]


def _full(owner: str, ids: list[int]) -> dict[int, dict]:
    """The belief dicts (as ``list_beliefs`` returns them) of these ids."""
    if not ids:
        return {}
    with ledger._connect() as db:
        rows = db.execute(f"{ledger._BELIEF_SELECT} WHERE b.owner = ? AND b.id IN "
                          f"({','.join('?' * len(ids))})", (owner, *ids)).fetchall()
        evidence = ledger._evidence_rows(db, [r["id"] for r in rows])
    return {r["id"]: ledger._belief_dict(r, evidence[r["id"]]) for r in rows}


def search_details(owner: str | None, query: str, *, k: int = 10,
                   status: str | Iterable[str] | None = ("confirmed",),
                   subject_id: int | None = None, attribute: str | None = None,
                   kinds: Iterable[str] | None = None,
                   emb: Embedder | None | bool = True) -> dict[str, Any]:
    """:func:`search_beliefs` with how it ranked: ``{"results", "semantic"`` (a local
    lane was used), ``"embedded"`` (vectors computed in this call), ``"pending"`` (left
    for the next call), ``"candidates"`` (beliefs scored)``}``. ``emb``: True for the
    hub's lane, None/False for none."""
    owner = owner or ""
    out: dict[str, Any] = {"results": [], "semantic": False, "embedded": 0, "pending": 0,
                           "candidates": 0}
    if isinstance(status, str):
        status = None if status == "all" else (status,)
    statuses = list(status) if status else None
    bad = [x for x in statuses or [] if x not in ledger.STATUSES]
    if bad:
        raise ValueError(f"unknown status: {', '.join(map(str, bad))}")
    if attribute is not None and attribute not in ledger.ATTRIBUTES:
        raise ValueError(f"unknown attribute: {attribute}")
    query = str(query or "").strip()
    if not query or not ledger.db_path().exists():
        return out
    known = _subjects(owner)
    subjects = named_subjects(query, known)
    pool_subjects = set(subjects)
    if OWNER_WORDS & set(tokens(query)):  # "I/my" next to a name: the owner's beliefs too
        pool_subjects.update(s["id"] for s in known if s["kind"] == "owner")
    attrs = query_attributes(query)
    beliefs = _pool(owner, statuses, subject_id, attribute,
                    None if kinds is None else sorted(set(kinds)), pool_subjects, attrs)
    out["candidates"] = len(beliefs)
    if not beliefs:
        return out
    qwords = _keywords(query)

    lane = embedder() if emb is True else (emb or None)
    sims: dict[int, float] = {}
    if lane is not None:
        try:
            qvec = _encode(lane, [query])[0]
            vecs, out["embedded"], out["pending"] = _vectors(beliefs, lane, EMBED_PER_CALL)
            sims = {i: _cosine(qvec, v) for i, v in vecs.items()}
            out["semantic"] = True
        except Exception as e:  # noqa: BLE001 -- keyword ranking still answers
            logger.warning("belief search: embedding failed, keyword only: %s", e)
            sims = {}
    hits = []
    for b in beliefs:
        score, why = 0.0, []
        if b.get("subject_id") in subjects:
            score += W_SUBJECT
            why.append("subject match")
        if attrs:
            if b.get("attribute") in attrs:
                score += W_ATTRIBUTE
                why.append("attribute")
            elif attrs & query_attributes(b.get("shown_text")):
                score += W_ATTRIBUTE * TEXT_ATTRIBUTE
                why.append("attribute")
        sim = sims.get(b["id"])
        if sim is not None and sim >= MIN_SIMILARITY:
            score += W_SEMANTIC * (sim - MIN_SIMILARITY) / (1 - MIN_SIMILARITY)
            why.append("semantic")
        overlap = jaccard(qwords, _keywords(_embed_text(b)))
        if overlap:
            score += W_KEYWORD * overlap
            why.append("keyword")
        score = round(min(score, 1.0), 4)
        if score <= 0:
            continue
        reason = ", ".join(why)
        if not out["semantic"]:
            reason += "; no embeddings"
        hits.append({"id": b["id"], "last_seen": b.get("last_seen"), "score": score,
                     "why": reason})
    hits.sort(key=lambda h: h["id"])
    hits.sort(key=lambda h: h.get("last_seen") or "", reverse=True)
    hits.sort(key=lambda h: h["score"], reverse=True)
    top = hits[:max(1, int(k))]
    full = _full(owner, [h["id"] for h in top])
    out["results"] = [{**full[h["id"]], "score": h["score"], "why": h["why"]}
                      for h in top if h["id"] in full]
    return out


def search_beliefs(owner: str | None, query: str, *, k: int = 10,
                   status: str | Iterable[str] | None = ("confirmed",),
                   subject_id: int | None = None, attribute: str | None = None,
                   kinds: Iterable[str] | None = None) -> list[dict]:
    """The owner's beliefs that match ``query``, best first: each is the belief dict
    (as ``evidence_intake.list_beliefs`` returns it) plus ``score`` (0..1) and ``why``
    ("subject match", "attribute", "semantic", "keyword", comma-joined; "; no
    embeddings" when no local lane was used). Only beliefs that score above 0.
    ``status``: one status, several, ``"all"`` or None (any); ``subject_id``,
    ``attribute`` and ``kinds`` (subject kinds) narrow the pool."""
    return search_details(owner, query, k=k, status=status, subject_id=subject_id,
                          attribute=attribute, kinds=kinds)["results"]


# ------------------------------------------------------------------------ prompts
def _line(b: dict) -> str:
    name = b.get("subject_display") or b.get("subject") or ""
    if b.get("subject_kind") == "owner":
        name = "You" if name in ("", ledger.OWNER_DISPLAY) else f"{name} (you)"
    text = re.sub(r"\s+", " ", b.get("shown_text") or b.get("text") or "").strip()
    link = (b.get("link") or "").strip()
    return f"- {name}: {text}" + (f" ({link})" if link else "") if name else \
        f"- {text}" + (f" ({link})" if link else "")


def context_for(owner: str | None, query: str, *, k: int = CONTEXT_K,
                max_chars: int = CONTEXT_CHARS) -> str:
    """The top confirmed beliefs for ``query`` as a compact prompt block (at most
    ``max_chars``): a first line telling the model to use its tools for anything else
    and to cite the links, then ``- <subject>: <text> (<link>)`` per belief. Empty when
    nothing matches."""
    try:
        hits = search_beliefs(owner, query, k=k)
    except Exception as e:  # noqa: BLE001 -- prompts never fail on this
        logger.debug("belief search: context unavailable: %s", e)
        return ""
    text = CONTEXT_HEADER
    added = 0
    for b in hits:
        line = _line(b)
        if len(text) + 1 + len(line) > max_chars:
            continue
        text += "\n" + line
        added += 1
    return text if added else ""


# --------------------------------------------------------------------------- chat
_NAME = "__S__"  # placeholder in the patterns below: a known subject or "I"/"me"/"my"
_ATTRIBUTE_QUESTIONS = (
    # lives_in
    r"\bwhere\s+(?:does|do|did)\s+__S__\s+live\b",
    r"\bwhere\s+(?:is|am|are)\s+__S__\s+(?:living|based|from)\b",
    r"\b__S__(?:['’]s)?\s+(?:home\s+)?address\b",
    # works_at, role
    r"\bwhere\s+(?:does|do)\s+__S__\s+work\b",
    r"\bwho\s+(?:does|do)\s+__S__\s+work\s+for\b",
    r"\bwhat\s+(?:does|do)\s+__S__\s+do\s+for\s+(?:a\s+)?(?:living|work)\b",
    r"\bwhat(?:['’]s|\s+is)\s+__S__(?:['’]s)?\s+(?:job|role|title|position|"
    r"profession|occupation)\b",
    # birthday
    r"\bwhen(?:['’]s|\s+is|\s+was)\s+__S__(?:['’]s)?\s+birthday\s*[?.!]*$",
    r"\bwhen\s+(?:was|were)\s+__S__\s+born\b",
    # allergy: yes/no questions and "allergies"; "What is <person> allergic to?" is left
    # to the module tools (an Athena bakeoff question)
    r"^\s*(?:am|is|are)\s+__S__\s+allergic\b",
    r"\b(?:does|do)\s+__S__\s+have\s+(?:any\s+)?allerg",
    r"\b__S__(?:['’]s)?\s+allergies\b",
    r"\bwhat\s+am\s+i\s+allergic\s+to\b",
    # diet, likes
    r"^\s*(?:am|is|are)\s+__S__\s+(?:a\s+)?(?:vegetarian|vegan|pescatarian)\b",
    r"\b__S__(?:['’]s)?\s+diet\b",
    r"\bwhat\s+(?:does|do)\s+__S__\s+(?:like|love|enjoy|dislike|hate)\b",
    r"\b__S__(?:['’]s)?\s+favou?rite\b",
    # pet, contact, relationship
    r"\b(?:does|do)\s+__S__\s+have\s+(?:a\s+|any\s+)?(?:pets?|dogs?|cats?)\b",
    r"\b__S__(?:['’]s)?\s+(?:pet|dog|cat)(?:['’]s)?\s+name\b",
    r"\b__S__(?:['’]s)?\s+(?:phone(?:\s+number)?|email(?:\s+address)?)\b",
    r"\bwho\s+is\s+__S__(?:['’]s)?\s+(?:wife|husband|partner|sister|brother|mother|"
    r"father|son|daughter|boss|manager)\b",
    r"\bhow\s+(?:is|are|am)\s+__S__\s+related\s+to\b",
    r"\bwho\s+is\s+__S__\s+to\s+(?:me|__S__)\b",
    r"\b__S__(?:['’]s)?\s+relationship\s+(?:to|with)\b",
    # projects
    r"\b(?:status|progress|deadline)\s+(?:of|on|for)\s+__S__\b",
    r"\bhow\s+is\s+__S__\s+going\b",
    r"\bwhen\s+is\s+__S__\s+due\b",
)
_KNOW_ABOUT = (
    r"\b(?:what|anything)\s+(?:do|did|does)\s+(?:you|we)\s+know\s+about\s+__S__\b",
    r"\bwhat\s+(?:can|do)\s+you\s+(?:tell\s+me|remember)\s+about\s+__S__\b",
    r"\btell\s+me\s+(?:everything\s+|more\s+|what\s+you\s+know\s+)?about\s+__S__\b",
    r"^\s*who(?:['’]s|\s+is)\s+__S__\s*[?.!]*$",
    r"\bremind\s+me\s+(?:who|about)\s+__S__\b",
)
_RECALL = re.compile(
    r"\b(?:say|said|says|saying|tell|told|mention(?:ed|s)?|ask(?:ed|s)?|promis(?:e|ed|es)|"
    r"recommend(?:ed|s)?|talk(?:ed|s)?|discuss(?:ed|es)?|happen(?:ed|s)?|latest|"
    r"conversations?|meetings?|standup|voice\s+notes?|record(?:ed|ing)|yesterday|"
    r"last\s+(?:week|month|time|night)|january|february|march|april|june|july|august|"
    r"september|october|november|december|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday)\b", re.IGNORECASE)


def _compile(patterns: tuple[str, ...], names: list[str]) -> list[re.Pattern]:
    alt = "|".join(re.escape(n) for n in sorted(set(names), key=len, reverse=True))
    return [re.compile(p.replace(_NAME, f"(?:{alt})"), re.IGNORECASE) for p in patterns]


def _is_slash(text: str) -> bool:
    if text.startswith("/"):
        return True
    try:
        from src.skill_invocation import INVOKE_PREFIX

        return text.replace("\r\n", "\n").startswith(INVOKE_PREFIX)
    except Exception:  # noqa: BLE001
        return False


def wants_beliefs(message: Any, owner: str | None) -> bool:
    """Whether a chat message asks what the hub knows about someone: "what do you know
    about <known subject>", "tell me about <known subject>", "who is <known subject>?",
    or a question about an attribute of a known subject or the owner ("where does Tom
    live", "am I allergic to anything", "when is Priya's birthday?"). Never for slash
    turns, nor for questions about what was said or happened (said, told, asked,
    mentioned, promised, a meeting, a month or weekday ...): those must reach the module
    tools. Known subjects: the owner's subjects with a confirmed belief."""
    raw = str(message or "").strip()
    text = re.sub(r"\s+", " ", raw)
    if not text or _is_slash(raw) or len(text) > 300:
        return False
    try:
        if not ledger.db_path().exists():
            return False
        subjects = _subjects(owner or "", confirmed_only=True)
    except Exception as e:  # noqa: BLE001 -- chat never fails on this
        logger.debug("belief search: subjects unavailable: %s", e)
        return False
    if not subjects:
        return False
    names = [n for s in subjects if s["kind"] != "owner" or s["display"] != ledger.OWNER_DISPLAY
             for n in s["names"]]
    people = names + ["I", "me", "my", "myself"]
    know = _compile(_KNOW_ABOUT, people)
    rest = text
    for pattern in know:
        rest = pattern.sub(" ", rest)
    if _RECALL.search(rest):
        return False
    if rest != text:
        return True
    return any(p.search(text) for p in _compile(_ATTRIBUTE_QUESTIONS, people))


def chat_block(owner: str | None, message: Any) -> str:
    """The "What I know (confirmed)" block for a chat message, or "" when the message
    doesn't ask for it (:func:`wants_beliefs`) or nothing matches."""
    try:
        if not wants_beliefs(message, owner):
            return ""
        return context_for(owner, str(message))
    except Exception as e:  # noqa: BLE001
        logger.debug("belief search: chat block unavailable: %s", e)
        return ""
