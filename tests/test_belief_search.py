"""Fork: retrieval over the belief ledger (src/belief_search.py; plan stage 2.3).

Fictional data only. The embedding lane is a deterministic fake (word concepts hashed
into a small vector); no model is loaded.
"""
import datetime
import hashlib
import math
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.middleware import require_admin
from src import belief_search as bs
from src import evidence_intake as ei
from src import module_facts as mf

T0 = datetime.datetime(2026, 10, 21, 9, 0, tzinfo=datetime.timezone.utc)

# odysseus-sound's bakeoff questions (server/eval/bakeoff.py): they must keep reaching the
# module tools, so none may add the beliefs block
BAKEOFF = [
    "What did Alice say about the budget in September?",
    "What's the latest on the Q4 budget cut?",
    "What did Tom say on October 3rd?",
    "When is my next dentist appointment?",
    "What am I getting Mom for her birthday?",
    "Where and when is Mom's birthday dinner?",
    "What did Ben say in the standup on October 7th?",
    "When was the client demo moved to?",
    "What did Maria ask me to do last week?",
    "How much will my rent be from January?",
    "What happened in my conversations on September 30?",
    "Who is taking over Alice's vendor contracts?",
    "Did anyone mention a marathon?",
    "What did I promise Ben?",
    "What did Dr. Patel tell me to avoid?",
    "What is Jonas allergic to?",
    "Which book did Jonas recommend?",
    "When exactly is Alice moving to Berlin?",
    "What do I need to do before October 31?",
    "What did Alice say about Mars?",
    "What was my latest voice note about?",
]

CONCEPTS = {
    "outdoors": {"hike", "hiking", "mountains", "mountain", "trail", "outdoors", "weekends",
                 "weekend", "climbing"},
    "food": {"peanuts", "peanut", "allergic", "allergy", "eat", "food", "nuts", "jazz"},
    "pets": {"dog", "dogs", "pet", "pets", "puppy", "cat"},
    "home": {"lives", "live", "porto", "lisbon", "home", "city", "moving", "berlin"},
}


class FakeLane:
    """Deterministic: one dimension per concept plus 16 hashed word dimensions."""

    model = "fake:1"

    def __init__(self):
        self.encoded: list[str] = []

    def encode(self, texts):
        out = []
        for text in texts:
            self.encoded.append(text)
            vec = [0.0] * (len(CONCEPTS) + 16)
            for w in bs.tokens(text):
                for i, words in enumerate(CONCEPTS.values()):
                    if w in words:
                        vec[i] += 3.0
                vec[len(CONCEPTS) + int(hashlib.md5(w.encode()).hexdigest(), 16) % 16] += 0.2
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append([x / norm for x in vec])
        return out


def ev(sid, text, subject, about="person", *, confirmed=True, person_id=None, **kw):
    return ei.Evidence(
        source=mf.SOURCE, source_id=str(sid), subject=subject, about=about, text=text,
        link=f"http://h:8000/view/c{sid}?sig=abc#t={sid * 10}",
        seen_first=kw.get("seen_first", "2026-10-01"), seen_last=kw.get("seen_last", "2026-10-15"),
        times=3 if confirmed else 1, days=2 if confirmed else 1,
        source_confirmed=confirmed, person_id=person_id)


FACTS = [
    (1, "I am allergic to penicillin", "Me", "owner", "allergy", "penicillin"),
    (2, "Priya Nair lives in Porto", "Priya Nair", "person", "lives_in", "Porto"),
    (3, "Priya Nair is allergic to peanuts", "Priya Nair", "person", "allergy", "peanuts"),
    (4, "Priya ran the design review", "Priya Nair", "person", None, None),
    (5, "Tom lives in Lisbon", "Tom", "person", "lives_in", "Lisbon"),
    (6, "Tom goes hiking in the mountains on weekends", "Tom", "person", "likes", "hiking"),
    (7, "Jonas is allergic to peanuts", "Jonas", "person", "allergy", "peanuts"),
    (8, "Alice is moving to Berlin", "Alice", "person", None, None),
    (9, "Mom's birthday is 12 November", "Mom", "person", "birthday", "12 November"),
    (10, "Ben owns a red bike", "Ben", "person", "owns", "a red bike"),
    (11, "Maria likes jazz", "Maria", "person", "likes", "jazz"),
    (12, "Dr. Patel is the owner's dentist", "Dr. Patel", "person", "relationship", "dentist"),
    (13, "Project Heron launches in December", "Project Heron", "other", "deadline", "December"),
    (14, "Plays the cello on Sundays", "Me", "owner", None, None),
]


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    monkeypatch.setattr(mf, "_LOCKS", {})
    for var in ("BELIEF_CONFIRM_SOURCES", "BELIEF_CONFIRM_TIMES", "BELIEF_CONFIRM_DAYS",
                "BELIEF_STALE_DAYS", "MODULE_FACTS_SYNC"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(bs, "embedder", lambda: None)
    ei.sync_people("alice", mf.SOURCE, [{"id": "me", "name": "Sam Rivera", "is_owner": True}],
                   now=T0)
    ei.submit("alice", [ev(sid, text, subj, about) for sid, text, subj, about, _, _ in FACTS]
              + [ev(50, "Tom has a dog called Pixel", "Tom", confirmed=False)], now=T0)
    ei.submit("bob", [ev(90, "Priya Nair lives in Oslo", "Priya Nair")], now=T0)
    for b in ei.list_beliefs("alice", status="confirmed"):
        row = next(f for f in FACTS if f[1] == b["text"])
        if row[4]:
            assert ei.apply_structure("alice", b["id"], row[4], row[5])
    return tmp_path


@pytest.fixture
def lane(monkeypatch):
    fake = FakeLane()
    monkeypatch.setattr(bs, "embedder", lambda: fake)
    return fake


def texts(hits):
    return [h["shown_text"] for h in hits]


# ------------------------------------------------------------------ ranking
def test_subject_match_comes_first_and_beats_semantic(data, lane):
    found = bs.search_details("alice", "Priya and the mountains on weekends", k=10)
    hits = found["results"]
    # a named subject narrows the pool to its beliefs: Tom's hiking (semantically
    # closest) isn't even scored
    assert set(texts(hits)) == {"Priya Nair lives in Porto",
                                "Priya Nair is allergic to peanuts",
                                "Priya ran the design review"}
    assert found["candidates"] == 3
    assert all("subject match" in h["why"] for h in hits)
    assert all(0 < h["score"] <= 1 for h in hits)
    # without a name, the same words rank by similarity over everything
    hits = bs.search_beliefs("alice", "the mountains on weekends", k=10)
    assert texts(hits)[0] == "Tom goes hiking in the mountains on weekends"
    assert hits[0]["why"].startswith("semantic")


def test_attribute_synonyms(data):
    assert bs.query_attributes("Where does Tom live?") == {"lives_in"}
    assert bs.query_attributes("am I allergic to anything") == {"allergy"}
    assert "works_at" in bs.query_attributes("who is his employer")
    assert "birthday" in bs.query_attributes("when was she born")
    assert "diet" in bs.query_attributes("is she vegan")
    assert "likes" in bs.query_attributes("her favourite band")
    hits = bs.search_beliefs("alice", "where does Tom live", k=3)
    assert texts(hits)[0] == "Tom lives in Lisbon"
    assert hits[0]["why"].startswith("subject match, attribute")
    hits = bs.search_beliefs("alice", "allergies", k=5)
    assert set(texts(hits)) == {"I am allergic to penicillin", "Priya Nair is allergic to peanuts",
                                "Jonas is allergic to peanuts"}


def test_query_families_from_the_scale_eval(data):
    # "related to me": the named person's relationship, not the owner's beliefs
    assert bs.query_attributes("How is Dr. Patel related to me?") == {"relationship"}
    hits = bs.search_beliefs("alice", "How is Dr. Patel related to me?", k=3)
    assert texts(hits)[0] == "Dr. Patel is the owner's dentist"
    assert hits[0]["why"].startswith("subject match, attribute")
    assert bs.query_attributes("who is Ben to me") >= {"relationship"}
    # "for a living" is a job, not a home
    assert bs.query_attributes("What does Priya do for a living?") == {"role", "works_at"}
    assert bs.query_attributes("what is her profession") >= {"role"}
    assert bs.query_attributes("her occupation") >= {"role"}
    assert bs.query_attributes("job title") >= {"role"}
    ei.submit("alice", [ev(70, "Priya Nair works as a designer", "Priya Nair")], now=T0)
    (b,) = [b for b in ei.list_beliefs("alice") if b["text"] == "Priya Nair works as a designer"]
    ei.apply_structure("alice", b["id"], "role", "designer")
    hits = bs.search_beliefs("alice", "What does Priya Nair do for a living?", k=3)
    assert texts(hits)[0] == "Priya Nair works as a designer"
    # decisions and plans
    assert bs.query_attributes("What did I decide?") == {"decision"}
    assert bs.query_attributes("what am I going to do") == {"plans"}
    assert bs.query_attributes("what are my plans") == {"plans"}
    ei.submit("alice", [ev(71, "I decided to sell the old car", "Me", "owner"),
                        ev(72, "I plan to visit Kyoto in spring", "Me", "owner")], now=T0)
    for text, attr in (("I decided to sell the old car", "decision"),
                       ("I plan to visit Kyoto in spring", "plans")):
        (b,) = [b for b in ei.list_beliefs("alice") if b["text"] == text]
        ei.apply_structure("alice", b["id"], attr, text)
    assert texts(bs.search_beliefs("alice", "What did I decide?", k=1)) == \
        ["I decided to sell the old car"]
    assert texts(bs.search_beliefs("alice", "What am I going to do?", k=1)) == \
        ["I plan to visit Kyoto in spring"]


def test_candidate_pool_is_narrowed_at_scale(data):
    people = [f"Person{i:03d} Example" for i in range(300)]
    words = ["lives in Porto", "likes chess", "works at Acme", "has a cat", "plays tennis",
             "is allergic to dust", "owns a kayak", "dislikes rain", "is a nurse",
             "follows a vegan diet"]
    items = [ev(1000 + n, f"{people[n % 300]} {words[n // 300]}", people[n % 300],
                seen_last=f"2026-10-{1 + n % 20:02d}")
             for n in range(3000)]
    ei.submit("alice", items, now=T0)
    for b in ei.list_beliefs("alice", status="confirmed", limit=10_000):
        if b["text"].endswith("lives in Porto"):
            ei.apply_structure("alice", b["id"], "lives_in", "Porto")
    assert len(ei.list_beliefs("alice", status="confirmed", limit=10_000)) > 3000
    found = bs.search_details("alice", f"What do you know about {people[7]}?")
    assert found["candidates"] < 200 and len(found["results"]) == 10
    assert all(h["subject_display"] == people[7] for h in found["results"])
    found = bs.search_details("alice", f"Where does {people[42]} live?", k=1)
    assert found["candidates"] < 200
    assert texts(found["results"]) == [f"{people[42]} lives in Porto"]
    # attribute only: that attribute and the unstructured beliefs
    found = bs.search_details("alice", "who lives where")
    assert found["results"][0]["attribute"] == "lives_in"
    # no subject, no attribute: the newest SCAN_LIMIT at most
    found = bs.search_details("alice", "chess")
    assert found["candidates"] == bs.SCAN_LIMIT
    assert found == bs.search_details("alice", "chess")  # deterministic


def test_owner_pronouns_mean_the_owner_subject(data):
    hits = bs.search_beliefs("alice", "am I allergic to anything?", k=3)
    assert texts(hits)[0] == "I am allergic to penicillin"
    assert hits[0]["why"].startswith("subject match, attribute")
    # someone else named: "I" doesn't pull the owner's beliefs up
    hits = bs.search_beliefs("alice", "what do I know about Jonas", k=2)
    assert texts(hits)[0] == "Jonas is allergic to peanuts"


def test_keyword_fallback_without_a_lane(data):
    hits = bs.search_beliefs("alice", "cello", k=5)
    assert texts(hits) == ["Plays the cello on Sundays"]
    assert hits[0]["why"] == "keyword; no embeddings"
    assert bs.search_details("alice", "cello")["semantic"] is False
    assert bs.search_beliefs("alice", "zeppelins", k=5) == []
    assert bs.search_beliefs("alice", "   ") == []


def test_semantic_with_a_lane(data, lane):
    hits = bs.search_beliefs("alice", "outdoors trail", k=3)
    assert texts(hits)[0] == "Tom goes hiking in the mountains on weekends"
    assert hits[0]["why"] == "semantic"


def test_status_subject_attribute_and_kind_filters(data):
    assert bs.search_beliefs("alice", "Tom dog") and \
        "Tom has a dog called Pixel" not in texts(bs.search_beliefs("alice", "Tom dog"))
    assert "Tom has a dog called Pixel" in texts(bs.search_beliefs("alice", "Tom dog",
                                                                   status="all"))
    assert texts(bs.search_beliefs("alice", "dog", status=("tentative",))) == \
        ["Tom has a dog called Pixel"]
    tom = ei.find_subject("alice", "Tom")["id"]
    assert set(texts(bs.search_beliefs("alice", "lives", subject_id=tom))) == \
        {"Tom lives in Lisbon"}
    assert texts(bs.search_beliefs("alice", "Priya Nair", attribute="allergy")) == \
        ["Priya Nair is allergic to peanuts"]
    assert texts(bs.search_beliefs("alice", "Project Heron december", kinds=["project"])) == \
        ["Project Heron launches in December"]
    assert texts(bs.search_beliefs("bob", "where does Priya Nair live")) == \
        ["Priya Nair lives in Oslo"]  # owner-scoped


def test_ties_by_last_seen_then_id(data):
    ei.submit("alice", [ev(60, "Kai likes tea", "Kai", seen_last="2026-10-10"),
                        ev(61, "Kai likes coffee", "Kai", seen_last="2026-10-20"),
                        ev(62, "Kai likes cocoa", "Kai", seen_last="2026-10-20")], now=T0)
    hits = bs.search_beliefs("alice", "Kai", k=5)
    assert texts(hits) == ["Kai likes coffee", "Kai likes cocoa", "Kai likes tea"]
    assert bs.search_beliefs("alice", "Kai", k=5) == hits  # deterministic


# ------------------------------------------------------------------ embedding cache
def test_cache_reuse_text_change_and_budget(data, lane, monkeypatch):
    bs.search_beliefs("alice", "hiking")
    confirmed = len(ei.list_beliefs("alice", status="confirmed"))
    assert len(lane.encoded) == confirmed + 1  # every belief, then the query
    lane.encoded.clear()
    bs.search_beliefs("alice", "hiking")
    assert lane.encoded == ["hiking"]  # second call embeds only the query
    tom = next(b for b in ei.list_beliefs("alice") if b["text"] == "Tom lives in Lisbon")
    ei.correct("alice", tom["id"], "Tom lives in Coimbra", now=T0)
    lane.encoded.clear()
    bs.search_beliefs("alice", "hiking")
    assert lane.encoded == ["hiking", "Tom: Tom lives in Coimbra"]
    # at most EMBED_PER_CALL per call; the rest on the next call
    monkeypatch.setattr(bs, "EMBED_PER_CALL", 2)
    lane.model = "fake:2"  # another model: every vector is redone
    found = bs.search_details("alice", "hiking")
    assert (found["embedded"], found["pending"]) == (2, confirmed - 2)
    found = bs.search_details("alice", "hiking")
    assert (found["embedded"], found["pending"]) == (2, confirmed - 4)


def test_reindex(data, lane):
    bs.search_beliefs("alice", "hiking")
    out = bs.reindex("alice")
    assert out["model"] == "fake:1"
    assert out["embedded"] == len(ei.list_beliefs("alice", status=["tentative", "confirmed",
                                                                   "stale"]))
    assert out["removed"] == len(ei.list_beliefs("alice", status="confirmed"))


def test_reindex_without_a_lane(data):
    assert bs.reindex("alice") == {"embedded": 0, "removed": 0, "model": None}


def test_failing_lane_falls_back_to_keywords(data, monkeypatch):
    class Broken:
        model = "broken"

        def encode(self, texts):
            raise RuntimeError("down")

    monkeypatch.setattr(bs, "embedder", lambda: Broken())
    hits = bs.search_beliefs("alice", "cello")
    assert texts(hits) == ["Plays the cello on Sundays"] and "no embeddings" in hits[0]["why"]


def test_only_local_lanes():
    def lane(name, url):
        return SimpleNamespace(name=name, url=url)

    assert bs._local_lane(lane("fastembed", "local://fastembed"))
    assert bs._local_lane(lane("custom", "http://127.0.0.1:8081/v1"))
    assert bs._local_lane(lane("custom", "http://100.64.1.2:8081/v1"))  # tailscale
    assert bs._local_lane(lane("custom", "http://embeddings:8080/v1"))  # a container
    assert not bs._local_lane(lane("custom", "https://api.example.com/v1"))


def test_hub_lane_comes_from_the_loaded_memory_index(monkeypatch):
    import src.ai_interaction as ai

    remote = SimpleNamespace(name="custom", url="https://api.example.com/v1", healthy=True,
                             fingerprint="r", model="m", encode=lambda t: [[1.0]] * len(t))
    local = SimpleNamespace(name="fastembed", url="local://fastembed", healthy=True,
                            fingerprint="f1", model="m", encode=lambda t: [[1.0]] * len(t))
    monkeypatch.setattr(ai, "_memory_vector", SimpleNamespace(healthy=True, _lanes=[remote, local]))
    assert bs.embedder().model == "fastembed:f1"
    monkeypatch.setattr(ai, "_memory_vector", SimpleNamespace(healthy=False, _lanes=[local]))
    assert bs.embedder() is None
    monkeypatch.setattr(ai, "_memory_vector", None)
    assert bs.embedder() is None


# ------------------------------------------------------------------ prompt block
def test_context_for_format_and_cap(data):
    block = bs.context_for("alice", "what do you know about Priya Nair")
    lines = block.splitlines()
    assert lines[0] == bs.CONTEXT_HEADER
    assert "Use your tools for anything else" in lines[0] and "cite the link" in lines[0]
    assert "- Priya Nair: Priya Nair lives in Porto (http://h:8000/view/c2?sig=abc#t=20)" in lines
    assert len(lines) - 1 <= bs.CONTEXT_K
    small = bs.context_for("alice", "what do you know about Priya Nair", max_chars=220)
    assert 0 < len(small) <= 220 and len(small.splitlines()) == 2
    assert bs.context_for("alice", "what do you know about Priya Nair", max_chars=50) == ""
    assert bs.context_for("alice", "zeppelins") == ""
    owner = bs.context_for("alice", "am I allergic to anything")
    assert "- Sam Rivera (you): I am allergic to penicillin (" in owner


# ------------------------------------------------------------------ chat gate
@pytest.mark.parametrize("message", [
    "What do you know about Priya Nair?", "what do you know about Priya", "Tell me about Tom",
    "Who is Dr. Patel?", "where does Tom live", "Where does Priya live?",
    "am I allergic to anything", "Am I allergic to anything?", "Is Priya allergic to nuts?",
    "Does Jonas have any allergies?", "When is Mom's birthday?", "what do you know about me",
    "What does Maria like?", "What's the status of Project Heron?",
    "How is Dr. Patel related to me?", "Who is Ben to me?", "What does Priya do for a living?",
])
def test_wants_beliefs(data, message):
    assert bs.wants_beliefs(message, "alice")


@pytest.mark.parametrize("message", [
    *BAKEOFF,
    "/sound what do you know about Priya Nair", "/context where does Tom live",
    "What do you know about Bob Unknown?", "where does Zed live",
    "What did Tom say about where he lives?", "Tell me about the weather",
    "What should I do first today?", "", "   ",
])
def test_no_beliefs_block(data, message):
    assert not bs.wants_beliefs(message, "alice")


def test_bakeoff_questions_never_get_the_block(data):
    assert len(BAKEOFF) == 21
    assert [q for q in BAKEOFF if bs.chat_block("alice", q)] == []


def test_skill_invocation_is_a_slash_turn(data):
    from src.skill_invocation import INVOKE_PREFIX

    assert not bs.wants_beliefs(INVOKE_PREFIX + "---\nname: sound\n---\nwhere does Tom live",
                                "alice")


def test_no_ledger_no_block(tmp_path, monkeypatch):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    assert not bs.wants_beliefs("What do you know about Priya Nair?", "alice")
    assert not (tmp_path / "beliefs.db").exists()  # the chat gate never creates the ledger
    assert bs.search_beliefs("alice", "Priya") == []


def test_another_owners_subjects_dont_count(data):
    assert not bs.wants_beliefs("What do you know about Priya Nair?", "carol")


def test_chat_preface_adds_the_block(data, monkeypatch):
    from src.chat_processor import ChatProcessor

    class _Memory:
        def load(self, owner=None):
            return []

    class _Docs:
        rag_manager = None

    def preface(message):
        out, _, _ = ChatProcessor(memory_manager=_Memory(), personal_docs_manager=_Docs()) \
            .build_context_preface(message=message, session=SimpleNamespace(), use_rag=False,
                                   use_memory=True, owner="alice")
        return "\n".join(m.get("content", "") for m in out)

    text = preface("What do you know about Priya Nair?")
    assert "What I know (confirmed)" in text and "Priya Nair lives in Porto" in text
    assert "What I know (confirmed)" not in preface("What is Jonas allergic to?")
    assert "What I know (confirmed)" not in preface("/sound what do you know about Priya Nair?")


# ------------------------------------------------------------------ route
def client_for(user="alice", token=False):
    from routes.belief_search_routes import setup_belief_search_routes

    app = FastAPI()

    @app.middleware("http")
    async def session(request, call_next):
        request.state.current_user = user
        request.state.api_token = token
        return await call_next(request)

    if not token:
        app.dependency_overrides[require_admin] = lambda: None
    app.include_router(setup_belief_search_routes())
    return TestClient(app)


def test_route(data):
    c = client_for()
    r = c.get("/api/beliefs/search", params={"q": "where does Tom live", "k": 2})
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "where does Tom live" and body["semantic"] is False
    assert [h["shown_text"] for h in body["results"]][0] == "Tom lives in Lisbon"
    assert len(body["results"]) == 2 and {"score", "why", "evidence"} <= set(body["results"][0])
    r = c.get("/api/beliefs/search", params={"q": "dog", "status": "all"})
    assert [h["shown_text"] for h in r.json()["results"]] == ["Tom has a dog called Pixel"]
    r = c.get("/api/beliefs/search", params={"q": "dog", "status": "tentative,confirmed"})
    assert r.status_code == 200 and r.json()["results"]
    assert c.get("/api/beliefs/search", params={"q": "x", "status": "maybe"}).status_code == 400
    assert c.get("/api/beliefs/search").json()["results"] == []
    assert client_for("bob").get("/api/beliefs/search", params={"q": "Porto"}).json()[
        "results"] == []


def test_route_needs_an_admin_session(data):
    c = client_for(token=True)
    assert c.get("/api/beliefs/search", params={"q": "Tom"}).status_code in (401, 403)


def test_route_is_mounted_before_the_review_pages_belief_id_route():
    src = open("app.py", encoding="utf-8").read()
    assert src.index("setup_belief_search_routes())") < src.index("setup_beliefs_routes())")
