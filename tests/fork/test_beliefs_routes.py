"""Fork: the belief review page's routes (routes/beliefs_routes.py).

Fictional data only.
"""
import datetime
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.beliefs_routes as br
from core.middleware import require_admin
from src import evidence_intake as ledger
from src import module_facts as mf

T0 = datetime.datetime(2026, 10, 21, 9, 0, tzinfo=datetime.timezone.utc)


def ev(sid, text, subject="Me", about="owner", confirmed=False, source=mf.SOURCE, **kw):
    return ledger.Evidence(
        source=source, source_id=str(sid), subject=subject, about=about, text=text,
        link=f"http://h:8000/view/c{sid}?sig=abc#t={sid * 10}",
        seen_first=kw.get("seen_first", "2026-10-01"), seen_last=kw.get("seen_last", "2026-10-15"),
        times=kw.get("times", 1), days=kw.get("days", 1), source_confirmed=confirmed)


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    monkeypatch.setattr(mf, "_LOCKS", {})
    for var in ("MODULE_FACTS_SYNC", "BARVIS_FACTS_SYNC", "BELIEF_CONFIRM_SOURCES",
                "BELIEF_CONFIRM_TIMES", "BELIEF_CONFIRM_DAYS", "BELIEF_STALE_DAYS"):
        monkeypatch.delenv(var, raising=False)
    ledger.submit("alice", [
        ev(1, "Prefers oat milk in coffee", confirmed=True),
        ev(2, "Plays the cello"),
        ev(3, "Lives in Lisbon with her dog Pixel", subject="Kath", about="person"),
    ], now=T0)
    ledger.submit("bob", [ev(9, "Collects vintage maps")], now=T0)
    return tmp_path


def ids(owner="alice"):
    return {b["shown_text"]: b["id"] for b in ledger.list_beliefs(owner)}


def client_for(user="alice"):
    app = FastAPI()

    @app.middleware("http")
    async def session(request, call_next):
        request.state.current_user = user
        request.state.api_token = False
        return await call_next(request)

    app.dependency_overrides[require_admin] = lambda: None
    app.include_router(br.setup_beliefs_routes())
    return TestClient(app)


@pytest.fixture
def client(data):
    return client_for()


def use_module(monkeypatch, call):
    monkeypatch.setattr(br, "module_caller", lambda: call)


# ------------------------------------------------------------------ reads
def test_list_defaults_to_all_but_rejected_with_stats(client):
    ledger.reject("alice", ids()["Plays the cello"])
    body = client.get("/api/beliefs").json()
    assert {b["shown_text"] for b in body["beliefs"]} == {
        "Prefers oat milk in coffee", "Lives in Lisbon with her dog Pixel"}
    assert body["stats"] == {"tentative": 1, "confirmed": 1, "stale": 0, "rejected": 1,
                             "total": 3, "structured": 0}
    assert all(b["owner"] == "alice" for b in body["beliefs"])


def test_list_status_and_subject_filters(client):
    got = client.get("/api/beliefs?status=tentative").json()["beliefs"]
    assert {b["shown_text"] for b in got} == {"Plays the cello",
                                              "Lives in Lisbon with her dog Pixel"}
    got = client.get("/api/beliefs?status=confirmed").json()["beliefs"]
    assert [b["confirmed_by"] for b in got] == ["source"]
    got = client.get("/api/beliefs?status=tentative&subject=kath").json()["beliefs"]
    assert [b["subject"] for b in got] == ["Kath"]
    assert len(client.get("/api/beliefs?status=all").json()["beliefs"]) == 3
    assert client.get("/api/beliefs?status=nonsense").status_code == 400


def test_get_has_evidence_and_events(client):
    body = client.get(f"/api/beliefs/{ids()['Prefers oat milk in coffee']}").json()
    assert body["status"] == "confirmed"
    assert body["evidence"][0]["link"] == "http://h:8000/view/c1?sig=abc#t=10"
    assert body["evidence"][0]["source"] == "odysseus-sound"
    assert [e["kind"] for e in body["events"]][-1] == "added"
    assert client.get("/api/beliefs/99999").status_code == 404


def test_another_owners_belief_is_404(client):
    bobs = ids("bob")["Collects vintage maps"]
    assert client.get(f"/api/beliefs/{bobs}").status_code == 404
    for action in ("confirm", "restore", "forget"):
        assert client.post(f"/api/beliefs/{bobs}/{action}").status_code == 404
    assert client.post(f"/api/beliefs/{bobs}/correct", json={"text": "x"}).status_code == 404
    assert ledger.get_belief("bob", bobs)["status"] == "tentative"


# ------------------------------------------------------------------ owner actions
def test_confirm(client):
    bid = ids()["Plays the cello"]
    body = client.post(f"/api/beliefs/{bid}/confirm").json()
    assert (body["status"], body["confirmed_by"]) == ("confirmed", "owner")
    assert body["memory_id"]


def test_correct_and_empty_text(client):
    bid = ids()["Plays the cello"]
    assert client.post(f"/api/beliefs/{bid}/correct", json={"text": "  "}).status_code == 400
    assert client.post(f"/api/beliefs/{bid}/correct", json={}).status_code == 400
    body = client.post(f"/api/beliefs/{bid}/correct",
                       json={"text": "Plays the  viola"}).json()
    assert (body["owner_text"], body["shown_text"], body["text"]) == \
        ("Plays the viola", "Plays the viola", "Plays the cello")
    assert (body["status"], body["confirmed_by"]) == ("confirmed", "owner")


def test_restore(client, monkeypatch):
    """A belief rejected before 4.2 (not erased) can still be restored."""
    bid = ids()["Plays the cello"]
    ledger.reject("alice", bid)
    body = client.post(f"/api/beliefs/{bid}/restore").json()
    assert body["status"] == "tentative"


def test_an_erased_belief_cant_come_back(client, monkeypatch):
    use_module(monkeypatch, None)
    bid = ids()["Plays the cello"]
    client.post(f"/api/beliefs/{bid}/forget")
    for action, body in (("restore", {}), ("confirm", {}), ("correct", {"text": "x"})):
        r = client.post(f"/api/beliefs/{bid}/{action}", json=body)
        assert r.status_code == 409, action


# ------------------------------------------------------------------ forget
class Module:
    def __init__(self, answer=None, error=None, erase=True):
        self.answer, self.error, self.erase, self.calls = answer, error, erase, []

    async def __call__(self, tool, args):
        self.calls.append((tool, args))
        if self.error:
            raise self.error
        if tool == "erase_fact" and not self.erase:
            raise RuntimeError("erase_fact: Unknown tool: erase_fact")
        return self.answer


REASON = "owner forgot it in Odysseus"


@pytest.mark.parametrize("answer", ["Fact #1 erased: 1 transcript line(s) redacted.",
                                    "No fact #1.", "Fact #1 was already erased."])
def test_forget_erases_the_module_fact(client, monkeypatch, answer):
    """A fact id the module no longer knows (re-summarized) is gone anyway: erased."""
    module = Module(answer)
    use_module(monkeypatch, module)
    bid = ids()["Prefers oat milk in coffee"]
    r = client.post(f"/api/beliefs/{bid}/forget", json={"reason": "wrong"})
    assert r.status_code == 200
    body = r.json()
    assert body["propagated"] == {"erased": 1, "retired": 0, "failed": 0, "unsupported": 0}
    assert body["erased"]["pending"] == 0 and body["erased"]["remaining"] == []
    belief = body["belief"]
    assert (belief["status"], belief["shown_text"], belief["note"]) == ("rejected", "", None)
    assert belief["erased_at"] and belief["evidence"][0]["text"] == ""
    assert belief["evidence"][0]["link"] == ""
    assert module.calls == [("erase_fact", {"fact_id": 1, "reason": REASON})]


def test_an_older_module_only_retires(client, monkeypatch):
    module = Module("Fact #1 retired.", erase=False)
    use_module(monkeypatch, module)
    bid = ids()["Prefers oat milk in coffee"]
    body = client.post(f"/api/beliefs/{bid}/forget").json()
    assert body["propagated"] == {"erased": 0, "retired": 1, "failed": 0, "unsupported": 0}
    assert body["erased"]["pending"] == 1
    assert [c[0] for c in module.calls] == ["erase_fact", "retire_fact"]
    assert body["belief"]["evidence"][0]["payload"] == {"erase": "retired"}


@pytest.mark.parametrize("error", [RuntimeError("erase_fact: database is locked"),
                                   ConnectionError("connection refused")])
def test_forget_stands_when_the_module_fails(client, monkeypatch, error):
    use_module(monkeypatch, Module(error=error))
    bid = ids()["Prefers oat milk in coffee"]
    r = client.post(f"/api/beliefs/{bid}/forget")
    assert r.status_code == 200
    assert r.json()["propagated"] == {"erased": 0, "retired": 0, "failed": 1, "unsupported": 0}
    belief = ledger.get_belief("alice", bid)
    assert belief["status"] == "rejected" and belief["erased_at"]
    assert ledger.erase_pending("alice") == [{"belief_id": bid, "fact_ids": [1]}]


def test_forget_without_the_module(client, monkeypatch):
    use_module(monkeypatch, None)
    bid = ids()["Prefers oat milk in coffee"]
    body = client.post(f"/api/beliefs/{bid}/forget").json()
    assert body["propagated"] == {"erased": 0, "retired": 0, "failed": 0, "unsupported": 1}
    assert body["belief"]["status"] == "rejected"


def test_forget_with_neither_tool(client, monkeypatch):
    use_module(monkeypatch, Module(error=RuntimeError("Unknown tool: erase_fact")))
    bid = ids()["Prefers oat milk in coffee"]
    body = client.post(f"/api/beliefs/{bid}/forget").json()
    assert body["propagated"] == {"erased": 0, "retired": 0, "failed": 0, "unsupported": 1}


def test_forget_without_module_evidence_calls_nothing(client, monkeypatch):
    module = Module("Fact #1 erased.")
    use_module(monkeypatch, module)
    ledger.submit("alice", [ev(7, "Keeps a sourdough starter", source="notes")], now=T0)
    bid = ids()["Keeps a sourdough starter"]
    body = client.post(f"/api/beliefs/{bid}/forget").json()
    assert body["propagated"] == {"erased": 0, "retired": 0, "failed": 0, "unsupported": 0}
    assert module.calls == []
    # the note still holds the words: the answer says where
    assert body["erased"]["remaining"] == [{"source": "notes",
                                            "link": "http://h:8000/view/c7?sig=abc#t=70"}]


def test_a_belief_forgotten_before_erasing_existed_can_be_erased(client, monkeypatch):
    module = Module("Fact #2 erased.")
    use_module(monkeypatch, module)
    bid = ids()["Plays the cello"]
    ledger.reject("alice", bid, "not true")  # the 1.4 forget
    body = client.post(f"/api/beliefs/{bid}/forget").json()
    assert body["belief"]["erased_at"] and body["propagated"]["erased"] == 1
    assert all(e["detail"] == "" for e in body["belief"]["events"])
    # erasing again calls nothing more
    module.calls.clear()
    client.post(f"/api/beliefs/{bid}/forget")
    assert module.calls == []


# ------------------------------------------------------------------ dismiss
def test_dismiss_retires_the_module_fact_and_erases_nothing(client, monkeypatch):
    module = Module("Fact #2 retired.")
    use_module(monkeypatch, module)
    bid = ids()["Plays the cello"]
    r = client.post(f"/api/beliefs/{bid}/dismiss", json={"reason": "only true that day"})
    assert r.status_code == 200
    body = r.json()
    assert body["propagated"] == {"retired": 1, "failed": 0, "unsupported": 0}
    belief = body["belief"]
    assert (belief["status"], belief["shown_text"], belief["note"]) == \
        ("rejected", "Plays the cello", "only true that day")
    assert not belief["erased_at"]
    assert belief["evidence"][0]["text"] == "Plays the cello"
    assert belief["evidence"][0]["link"]
    assert module.calls == [("retire_fact", {"fact_id": 2, "reason": REASON})]
    assert ledger.erase_pending("alice") == []
    # Restore undoes it
    assert client.post(f"/api/beliefs/{bid}/restore").json()["status"] == "tentative"


def test_dismiss_a_confirmed_belief_leaves_memory(client, monkeypatch):
    use_module(monkeypatch, Module("Fact #1 retired."))
    bid = ids()["Prefers oat milk in coffee"]
    assert ledger.get_belief("alice", bid)["memory_id"]
    body = client.post(f"/api/beliefs/{bid}/dismiss").json()
    assert body["belief"]["status"] == "rejected"
    assert not body["belief"]["memory_id"]


@pytest.mark.parametrize("module,expected", [
    (None, {"retired": 0, "failed": 0, "unsupported": 1}),
    (Module(error=ConnectionError("connection refused")),
     {"retired": 0, "failed": 1, "unsupported": 0}),
    (Module(error=RuntimeError("Unknown tool: retire_fact")),
     {"retired": 0, "failed": 0, "unsupported": 1}),
])
def test_dismiss_stands_without_the_module(client, monkeypatch, module, expected):
    use_module(monkeypatch, module)
    bid = ids()["Plays the cello"]
    r = client.post(f"/api/beliefs/{bid}/dismiss")
    assert r.status_code == 200
    assert r.json()["propagated"] == expected
    belief = ledger.get_belief("alice", bid)
    assert belief["status"] == "rejected" and not belief["erased_at"]


def test_dismiss_errors(client, monkeypatch):
    use_module(monkeypatch, None)
    bobs = ids("bob")["Collects vintage maps"]
    assert client.post(f"/api/beliefs/{bobs}/dismiss").status_code == 404
    assert ledger.get_belief("bob", bobs)["status"] == "tentative"
    bid = ids()["Plays the cello"]
    client.post(f"/api/beliefs/{bid}/forget")
    assert client.post(f"/api/beliefs/{bid}/dismiss").status_code == 409
    # a dismissed belief can still be erased
    other = ids()["Lives in Lisbon with her dog Pixel"]
    client.post(f"/api/beliefs/{other}/dismiss")
    assert client.post(f"/api/beliefs/{other}/forget").json()["belief"]["erased_at"]


# ------------------------------------------------------------------ export
def test_export_downloads_the_owners_ledger_and_memories(client, monkeypatch):
    from src.constants import DATA_DIR  # noqa: F401 -- patched by the fixture
    from src.memory import MemoryManager
    import src.constants as constants

    manager = MemoryManager(constants.DATA_DIR)
    manager.save([{"id": "m1", "text": "Likes hiking", "owner": "alice"},
                  {"id": "m2", "text": "Collects vintage maps", "owner": "bob"}])
    r = client.get("/api/beliefs/export")
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith('attachment; filename="odysseus-memory-')
    assert r.headers["cache-control"] == "no-store"
    data = r.json()
    assert (data["format"], data["version"], data["owner"]) == ("odysseus-memory-export", 1,
                                                                 "alice")
    assert {b["shown_text"] for b in data["beliefs"]} == {
        "Prefers oat milk in coffee", "Plays the cello", "Lives in Lisbon with her dog Pixel"}
    assert all(b["evidence"] and b["events"] for b in data["beliefs"])
    assert all("norm" not in b for b in data["beliefs"])
    assert data["beliefs"][0]["evidence"][0]["link"].startswith("http://h:8000/view/")
    assert {s["display"] for s in data["subjects"]} >= {"Kath"}
    assert [m["id"] for m in data["memories"] if m["id"] == "m1"] == ["m1"]
    assert "Collects vintage maps" not in r.text  # bob's
    assert data["counts"]["beliefs"] == 3


def test_export_of_an_erased_belief_has_no_words(client, monkeypatch):
    use_module(monkeypatch, None)
    bid = ids()["Plays the cello"]
    client.post(f"/api/beliefs/{bid}/forget")
    r = client.get("/api/beliefs/export")
    assert "cello" not in r.text
    assert [b["erased_at"] is not None for b in r.json()["beliefs"] if b["id"] == bid] == [True]


# ------------------------------------------------------------------ run
class FactsModule:
    def __init__(self, facts):
        self.facts, self.calls = facts, []

    async def __call__(self, tool, args):
        self.calls.append((tool, args))
        if tool == "list_people":
            return "No people yet."  # an older module: text, tolerated
        assert tool == "list_facts"
        return json.dumps({"facts": self.facts})


def test_run_syncs_facts_now(client, monkeypatch):
    module = FactsModule([
        {"id": 1, "subject": "Me", "about": "owner", "fact": "Prefers oat milk in coffee",
         "times_said": 3, "days": 2, "first_said": "2026-10-01", "last_said": "2026-10-15",
         "link": "http://h:8000/view/c1?sig=abc#t=10", "confirmed": True},
        {"id": 5, "subject": "Me", "about": "owner", "fact": "Runs on Saturday mornings",
         "times_said": 1, "days": 1, "first_said": "2026-10-20", "last_said": "2026-10-20",
         "link": "http://h:8000/view/c5?sig=abc#t=50", "confirmed": False},
    ])
    use_module(monkeypatch, module)
    r = client.post("/api/beliefs/run")
    assert r.status_code == 200
    body = r.json()
    assert body["summary"].startswith("facts: 2 seen, 1 new, ")
    assert body["stats"]["facts"] == 2
    assert module.calls == [("list_facts", {"format": "json", "limit": 1000}),
                            ("list_people", {"format": "json"})]
    assert "Runs on Saturday mornings" in ids()


def test_run_without_the_module_or_switched_off(client, monkeypatch):
    use_module(monkeypatch, None)
    assert client.post("/api/beliefs/run").status_code == 503
    monkeypatch.setenv("MODULE_FACTS_SYNC", "off")
    assert client.post("/api/beliefs/run").status_code == 409


def test_run_reports_a_failed_call(client, monkeypatch):
    use_module(monkeypatch, Module(error=RuntimeError("list_facts: boom")))
    r = client.post("/api/beliefs/run")
    assert r.status_code == 502 and "boom" in r.json()["error"]


# ------------------------------------------------------------------ subjects (2.1)
def subject_ids(owner="alice"):
    return {s["display"]: s["id"] for s in ledger.list_subjects(owner)}


@pytest.fixture
def contacts(data, monkeypatch):
    """A local contact book (no CardDAV): two of alice's, one of bob's."""
    import routes.contacts.contacts_routes as cr

    book = data / "contacts.json"
    book.write_text(json.dumps({"contacts": [
        {"uid": "uid-kath", "name": "Kath Okafor", "owner": "alice"},
        {"uid": "uid-arlo", "name": "Arlo Vance", "owner": "alice"},
        {"uid": "uid-wren", "name": "Wren Hale", "owner": "bob"},
    ]}))
    monkeypatch.setattr(cr, "LOCAL_CONTACTS_FILE", book)
    monkeypatch.setattr(cr, "_get_carddav_config", lambda: {"url": ""})
    monkeypatch.setattr(cr, "_contact_cache", {"contacts": [], "fetched_at": None,
                                               "failed_at": None, "by_owner": {}})
    return book


def test_subjects_list_with_counts_kind_filter_and_stats(client):
    body = client.get("/api/beliefs/subjects").json()
    got = {s["display"]: s for s in body["subjects"]}
    assert [s["kind"] for s in body["subjects"]][0] == "owner"  # the owner first
    assert {k: v["kind"] for k, v in got.items()} == {"Owner": "owner", "Kath": "person"}
    assert got["Owner"]["counts"] == {"tentative": 1, "confirmed": 1, "stale": 0,
                                      "rejected": 0, "total": 2}
    assert body["stats"]["total"] == 3
    people = client.get("/api/beliefs/subjects?kind=person").json()["subjects"]
    assert [s["display"] for s in people] == ["Kath"]
    assert client.get("/api/beliefs/subjects?kind=robot").status_code == 400
    assert all(s["owner"] == "alice" for s in body["subjects"])


def test_subject_detail_has_beliefs_with_structure_and_summary(client):
    kath = subject_ids()["Kath"]
    bid = ids()["Lives in Lisbon with her dog Pixel"]
    ledger.confirm("alice", bid)
    assert ledger.apply_structure("alice", bid, "lives_in", "Lisbon", "2024-05-01")
    ledger.submit("alice", [ev(4, "Kath dislikes cilantro", subject="Kath", about="person")],
                  now=T0)
    ledger.reject("alice", ids()["Kath dislikes cilantro"])
    body = client.get(f"/api/beliefs/subjects/{kath}").json()
    assert (body["subject"]["display"], body["subject"]["kind"]) == ("Kath", "person")
    assert [(b["attribute"], b["value"]) for b in body["beliefs"]] == [("lives_in", "Lisbon")]
    assert body["summary"] == "Kath (person). lives in: Lisbon (since 2024-05-01)."
    assert body["stats"]["rejected"] == 1
    owner = subject_ids()["Owner"]
    body = client.get(f"/api/beliefs/subjects/{owner}").json()
    assert {b["shown_text"] for b in body["beliefs"]} == {"Prefers oat milk in coffee",
                                                          "Plays the cello"}
    assert body["summary"].startswith("Owner (owner). Also: Prefers oat milk in coffee.")


def test_subject_404s(client):
    bobs = subject_ids("bob")["Owner"]
    assert client.get(f"/api/beliefs/subjects/{bobs}").status_code == 404
    assert client.get("/api/beliefs/subjects/99999").status_code == 404
    assert client.get("/api/beliefs/subjects/nope").status_code == 404
    for action, body in (("kind", {"kind": "thing"}), ("rename", {"display": "X"}),
                         ("link", {"contact_id": "uid-kath"}), ("unlink", None)):
        assert client.post(f"/api/beliefs/subjects/{bobs}/{action}",
                           json=body).status_code == 404
    kath = subject_ids()["Kath"]
    r = client.post("/api/beliefs/subjects/merge", json={"keep_id": kath, "drop_id": bobs})
    assert r.status_code == 404
    r = client.post("/api/beliefs/subjects/merge", json={"keep_id": bobs, "drop_id": kath})
    assert r.status_code == 404
    assert subject_ids("bob") == {"Owner": bobs}


def test_set_kind(client):
    got = subject_ids()
    body = client.post(f"/api/beliefs/subjects/{got['Kath']}/kind",
                       json={"kind": "project"}).json()
    assert (body["kind"], body["kind_by"]) == ("project", "owner")
    r = client.post(f"/api/beliefs/subjects/{got['Kath']}/kind", json={"kind": "owner"})
    assert r.status_code == 400 and "person, project or thing" in r.json()["detail"]
    r = client.post(f"/api/beliefs/subjects/{got['Owner']}/kind", json={"kind": "person"})
    assert r.status_code == 400 and r.json()["detail"] == "the owner subject keeps its kind"


def test_rename_and_duplicate_name(client):
    ledger.submit("alice", [ev(5, "Arlo teaches pottery", subject="Arlo", about="person")],
                  now=T0)
    got = subject_ids()
    body = client.post(f"/api/beliefs/subjects/{got['Kath']}/rename",
                       json={"display": "Kath Okafor"}).json()
    assert (body["display"], body["aliases"]) == ("Kath Okafor", ["Kath"])
    r = client.post(f"/api/beliefs/subjects/{got['Arlo']}/rename",
                    json={"display": "kath okafor"})
    assert r.status_code == 400 and "already called" in r.json()["detail"]
    r = client.post(f"/api/beliefs/subjects/{got['Arlo']}/rename", json={"display": " "})
    assert r.status_code == 400


def test_link_unlink_and_contact_beliefs(client, contacts):
    ledger.submit("alice", [ev(5, "Arlo teaches pottery", subject="Arlo", about="person")],
                  now=T0)
    got = subject_ids()
    body = client.post(f"/api/beliefs/subjects/{got['Kath']}/link",
                       json={"contact_id": "uid-kath"}).json()
    assert body["contact_id"] == "uid-kath"
    beliefs = client.get("/api/beliefs/contact/uid-kath").json()
    assert beliefs["contact_id"] == "uid-kath"
    assert [b["shown_text"] for b in beliefs["beliefs"]] == ["Lives in Lisbon with her dog Pixel"]
    assert beliefs["beliefs"][0]["contact_id"] == "uid-kath"
    assert client.get("/api/beliefs/contact/uid-kath?status=confirmed").json()["beliefs"] == []
    assert client.get("/api/beliefs/contact/uid-arlo").json()["beliefs"] == []
    assert client.get("/api/beliefs/contact/uid-kath?status=x").status_code == 400
    # a contact links to one subject: a second link is a 400 with the ledger's message
    r = client.post(f"/api/beliefs/subjects/{got['Arlo']}/link", json={"contact_id": "uid-kath"})
    assert r.status_code == 400 and "is linked to subject" in r.json()["detail"]
    # another owner's contact, an unknown one, or none: 400, nothing linked
    for cid in ("uid-wren", "uid-nobody", " "):
        r = client.post(f"/api/beliefs/subjects/{got['Arlo']}/link", json={"contact_id": cid})
        assert r.status_code == 400
    assert ledger.get_subject("alice", got["Arlo"])["contact_id"] is None
    body = client.post(f"/api/beliefs/subjects/{got['Kath']}/unlink").json()
    assert body["contact_id"] is None
    assert client.get("/api/beliefs/contact/uid-kath").json()["beliefs"] == []


def test_contacts_listing_is_owner_scoped_and_marks_links(client, contacts):
    kath = subject_ids()["Kath"]
    ledger.link_contact("alice", kath, "uid-kath")
    body = client.get("/api/beliefs/contacts").json()
    assert body["contacts"] == [
        {"id": "uid-arlo", "name": "Arlo Vance", "subject_id": None},
        {"id": "uid-kath", "name": "Kath Okafor", "subject_id": kath},
    ]
    body = client_for("bob").get("/api/beliefs/contacts").json()
    assert body["contacts"] == [{"id": "uid-wren", "name": "Wren Hale", "subject_id": None}]
    # bob's view of alice's contact: nothing
    assert client_for("bob").get("/api/beliefs/contact/uid-kath").json()["beliefs"] == []


def test_merge_and_its_400s(client, contacts):
    ledger.submit("alice", [ev(5, "Kat O. runs a bakery", subject="Kat O.", about="person")],
                  now=T0)
    got = subject_ids()
    ledger.link_contact("alice", got["Kat O."], "uid-kath")
    body = client.post("/api/beliefs/subjects/merge",
                       json={"keep_id": got["Kath"], "drop_id": got["Kat O."]}).json()
    assert body["id"] == got["Kath"] and body["aliases"] == ["Kat O."]
    assert body["contact_id"] == "uid-kath" and body["counts"]["total"] == 2
    assert "Kat O." not in subject_ids()
    r = client.post("/api/beliefs/subjects/merge",
                    json={"keep_id": got["Kath"], "drop_id": got["Kath"]})
    assert r.status_code == 400 and "into itself" in r.json()["detail"]
    r = client.post("/api/beliefs/subjects/merge",
                    json={"keep_id": got["Kath"], "drop_id": got["Owner"]})
    assert r.status_code == 400 and r.json()["detail"] == "the owner subject can only be kept"
    assert client.post("/api/beliefs/subjects/merge", json={}).status_code == 422


def test_attribute_filter(client):
    bid = ids()["Prefers oat milk in coffee"]
    ledger.apply_structure("alice", bid, "likes", "oat milk")
    got = client.get("/api/beliefs?attribute=likes").json()["beliefs"]
    assert [(b["id"], b["value"]) for b in got] == [(bid, "oat milk")]
    assert client.get("/api/beliefs?attribute=diet").json()["beliefs"] == []
    assert client.get("/api/beliefs?attribute=shoe_size").status_code == 400
    assert client.get("/api/beliefs?attribute=likes&status=tentative").json()["beliefs"] == []


def test_belief_routes_leave_other_literal_paths_alone(client):
    """``/api/beliefs/search`` belongs to another router: not swallowed as a belief id."""
    assert client.get("/api/beliefs/search?q=milk").status_code == 404


# ------------------------------------------------------------------ auth
def test_routes_refuse_api_tokens(data):
    app = FastAPI()

    @app.middleware("http")
    async def token(request, call_next):
        request.state.api_token = True
        request.state.current_user = "api"
        request.state.api_token_owner = "alice"
        return await call_next(request)

    app.include_router(br.setup_beliefs_routes())
    c = TestClient(app)
    assert c.get("/api/beliefs").status_code in (401, 403)
    assert c.post("/api/beliefs/1/confirm").status_code in (401, 403)
    assert c.post("/api/beliefs/run").status_code in (401, 403)


def test_page_is_served(client):
    r = client.get("/beliefs")
    assert r.status_code == 200
    assert "What I know about you" in r.text and "/static/js/beliefs.js" in r.text
