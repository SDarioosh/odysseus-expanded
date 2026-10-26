"""Fork: Athena's fact kind, quote and conversation on the belief review page
(src/module_facts.py, static/js/beliefs.js).

Athena (MCP 0.11.0) sends each fact's ``kind``, the transcript line it was said on
(``quote``) and its ``conversation_id``. The kind sets the memory category, so a request
to someone else ("wants Bo to write a report") is no longer a "preference" because it has
the word "wants"; the three travel in the evidence payload for the review page, and
erasing a belief clears them. An older module sends none of them and nothing changes.

Fictional data only.
"""
import pytest
from test_module_facts import T0, FakeModule, data, fact  # noqa: F401  (fixture)

from src import evidence_intake as ledger
from src import module_facts as mf

QUOTE = "Bo, could you write up how the Larkspur station joins the mesh?"


def with_kind(fid, text, kind, quote=QUOTE, conv="conv-7", **kw):
    return {**fact(fid, text, confirmed=False, times_said=1, days=1, **kw),
            "kind": kind, "quote": quote, "conversation_id": conv}


@pytest.mark.parametrize("about,text,kind,want", [
    ("owner", "Wants Bo Lindqvist to write a mesh report", "other", "fact"),
    ("owner", "Wants to visit Lake Bled next summer", "plan", "fact"),
    ("owner", "Drinks tea, never coffee", "preference", "preference"),
    ("owner", "Likes sailing", "", "preference"),  # no kind: the word decides, as before
    ("owner", "Likes sailing", "unknown-kind", "preference"),
    ("person", "Loves sailing", "preference", "contact"),
    ("other", "The allotment moves to Fernhill", "decision", "fact"),
])
def test_kind_sets_the_category(about, text, kind, want):
    assert mf.category({"about": about, "fact": text, "kind": kind}) == want


def test_payload_keeps_only_what_the_module_sends():
    row = with_kind(1, "Drinks tea, never coffee", "Preference", quote="  tea,\n please  ")
    assert mf.payload(row) == {"kind": "preference", "quote": "tea, please",
                               "conversation_id": "conv-7"}
    assert mf.payload(fact(2, "Likes sailing")) == {}
    long = with_kind(3, "Drinks tea", "preference", quote="word " * 200)
    assert len(mf.payload(long)["quote"]) == mf.QUOTE_CHARS
    assert mf.evidence(fact(2, "Likes sailing")).payload is None


@pytest.mark.asyncio
async def test_sync_files_kind_quote_and_conversation(data):  # noqa: F811
    module = FakeModule([
        with_kind(1, "Wants Bo Lindqvist to write a mesh report", "other"),
        with_kind(2, "Drinks tea, never coffee", "preference", quote="Tea for me, thanks."),
        fact(3, "Likes sailing", confirmed=False, times_said=1, days=1),
    ], flag=True)
    await mf.sync("alice", module, now=T0)
    beliefs = {b["text"]: b for b in ledger.list_beliefs("alice", "tentative")}
    request = beliefs["Wants Bo Lindqvist to write a mesh report"]
    assert request["category"] == "fact"
    (ev,) = request["evidence"]
    assert ev["payload"] == {"kind": "other", "quote": QUOTE, "conversation_id": "conv-7"}
    tea = beliefs["Drinks tea, never coffee"]
    assert tea["category"] == "preference"
    assert tea["evidence"][0]["payload"]["quote"] == "Tea for me, thanks."
    old = beliefs["Likes sailing"]
    assert old["category"] == "preference" and old["evidence"][0]["payload"] == {}


@pytest.mark.asyncio
async def test_a_module_update_adds_the_kind_to_an_existing_belief(data):  # noqa: F811
    module = FakeModule([fact(1, "Wants Bo Lindqvist to write a mesh report",
                              confirmed=False, times_said=1, days=1)], flag=True)
    await mf.sync("alice", module, now=T0)
    (b,) = ledger.list_beliefs("alice", "tentative")
    assert b["category"] == "preference"  # the old keyword guess
    module.facts = [with_kind(1, "Wants Bo Lindqvist to write a mesh report", "other")]
    await mf.sync("alice", module, now=T0)
    (b,) = ledger.list_beliefs("alice", "tentative")
    assert b["category"] == "fact"
    assert b["evidence"][0]["payload"]["conversation_id"] == "conv-7"


@pytest.mark.asyncio
async def test_erasing_a_belief_clears_its_quote(data):  # noqa: F811
    module = FakeModule([with_kind(1, "Drinks tea, never coffee", "preference")], flag=True)
    await mf.sync("alice", module, now=T0)
    (b,) = ledger.list_beliefs("alice", "tentative")
    ledger.reject("alice", b["id"], now=T0)
    ledger.erase("alice", b["id"], {"1": "erased"}, now=T0)
    got = ledger.get_belief("alice", b["id"])
    assert all("quote" not in e["payload"] for e in got["evidence"])
    assert QUOTE not in str(got)


# --- QC report fixes (fictional data) ------------------------------------------------
LINE = "My sister Ines moved to Porto last year, and I prefer tea to coffee."


class EraseModule(FakeModule):
    """FakeModule with erase_fact (retires the fact, as the real one also does)."""

    async def __call__(self, tool, args):
        if tool == "erase_fact":  # the line reads [forgotten]: no fact quotes it any more
            gone = next(f for f in self.facts if f["id"] == args["fact_id"])
            self.facts = [{**f, "quote": None} if f.get("quote") == gone.get("quote") else f
                          for f in self.facts if f["id"] != args["fact_id"]]
            return f"Fact {args['fact_id']} erased."
        return await super().__call__(tool, args)


def by_text(owner, status=None):
    return {b["text"]: b for b in ledger.list_beliefs(owner, status)}


@pytest.mark.asyncio
@pytest.mark.parametrize("dismiss_first", [False, True])
async def test_forget_scrubs_the_line_from_beliefs_said_on_it(data, dismiss_first):  # noqa: F811
    from src import belief_forget
    module = EraseModule([with_kind(1, "Me's sister Ines lives in Porto", "relationship",
                                    quote=LINE, conv="c1"),
                          with_kind(2, "Prefers tea to coffee", "preference", quote=LINE,
                                    conv="c1"),
                          with_kind(3, "Plays the cello", "personal", quote="Cello, Fridays.",
                                    conv="c1")], flag=True)
    await mf.sync("alice", module, now=T0)
    got = by_text("alice")
    if dismiss_first:  # Athena no longer returns it: its evidence goes "gone", kept as is
        await belief_forget.dismiss("alice", got["Prefers tea to coffee"]["id"], call=module)
    await belief_forget.forget("alice", got["Me's sister Ines lives in Porto"]["id"],
                               call=module)
    for _ in range(2):  # right after the forget (the page reloads), and after a sync
        tea = ledger.get_belief("alice", got["Prefers tea to coffee"]["id"])
        assert all("quote" not in e["payload"] for e in tea["evidence"])
        await mf.sync("alice", module, now=T0)
    assert LINE not in str(ledger.export_rows("alice"))
    cello = ledger.get_belief("alice", got["Plays the cello"]["id"])
    assert cello["evidence"][0]["payload"]["quote"] == "Cello, Fridays."


@pytest.mark.asyncio
async def test_move_keeps_the_kind_category(data):  # noqa: F811
    module = FakeModule([
        with_kind(10, "Wants to visit Lake Bled next summer", "plan", about="person",
                  subject="Bo"),
        with_kind(11, "Drinks Assam tea every morning", "preference", about="person",
                  subject="Bo")], flag=True)
    await mf.sync("alice", module, now=T0)
    for b in ledger.list_beliefs("alice", "tentative"):
        ledger.move("alice", b["id"], name="me")
    got = by_text("alice", "confirmed")
    assert got["Wants to visit Lake Bled next summer"]["category"] == "fact"
    assert got["Drinks Assam tea every morning"]["category"] == "preference"
    # without a kind, the words still decide
    assert ledger.category_for("owner", "Likes sailing") == "preference"


@pytest.mark.asyncio
async def test_a_decided_belief_takes_the_category_of_its_new_kind(data):  # noqa: F811
    row = fact(20, "Wants Bo Lindqvist to write a mesh report", confirmed=False,
               times_said=1, days=1)
    module = FakeModule([row], flag=True)
    await mf.sync("alice", module, now=T0)
    (b,) = ledger.list_beliefs("alice", "tentative")
    ledger.confirm("alice", b["id"], now=T0)
    assert ledger.get_belief("alice", b["id"])["category"] == "preference"
    module.facts = [with_kind(20, "Wants Bo Lindqvist to write a mesh report", "other")]
    await mf.sync("alice", module, now=T0)
    got = ledger.get_belief("alice", b["id"])
    assert (got["category"], got["status"], got["confirmed_by"]) == \
        ("fact", "confirmed", "owner")


@pytest.mark.asyncio
async def test_a_moved_belief_keeps_its_category_when_its_source_syncs(data):  # noqa: F811
    module = FakeModule([with_kind(30, "Drinks Assam tea every morning", "preference",
                                   about="person", subject="Bo")], flag=True)
    await mf.sync("alice", module, now=T0)
    (b,) = ledger.list_beliefs("alice", "tentative")
    ledger.move("alice", b["id"], name="me")
    module.facts = [with_kind(30, "Drinks Assam tea every morning", "preference",
                              about="person", subject="Bo", quote="Assam, please.")]
    await mf.sync("alice", module, now=T0)
    got = ledger.get_belief("alice", b["id"])
    assert (got["about"], got["category"]) == ("owner", "preference")


@pytest.mark.asyncio
async def test_near_duplicates_merge_across_categories(data):  # noqa: F811
    old = fact(40, "Wants to visit Lake Bled next summer", confirmed=False, times_said=1,
               days=1)
    new = with_kind(41, "Really wants to visit Lake Bled next summer", "plan", conv="c6")
    await mf.sync("alice", FakeModule([old, new], flag=True), now=T0)
    (b,) = ledger.list_beliefs("alice")
    assert {e["source_id"] for e in b["evidence"]} == {"40", "41"}
