"""Fork: the brain's ``search_datasheet_marks`` (fork/brain/datasheets.py) over the
ranked marks search (fork/ink/pdf_marks.py ``rank``): the pure scoring, the tool through
the brain's ``execute`` on real fictional datasheet notes (tests/fork/pdf_fixtures.py),
owner isolation, who is offered it, the gate armed, and voice turns. Fictional data only."""
from __future__ import annotations

import json
import uuid

import pytest

from fork.brain import datasheets as brain_datasheets
from fork.brain import prompt as brain_prompt
from fork.brain import tools as brain_tools
from fork.brain import toolset
from fork.brain import turn as brain_turn
from fork.ink import pdf_marks as pm
from tests.fork.brain_fakes import Brain, calls, text

TOOL = brain_datasheets.SEARCH_DATASHEET_MARKS
PEN = {"color": "#1f5fbf", "width": 2.5, "highlighter": False}
YELLOW = {"color": "#ffd400", "width": 14.0, "highlighter": True}


# -------------------------------------------------------------- scoring (pure)
def test_query_words_drop_stop_words_and_keep_part_numbers():
    assert pm.query_words("What did I note about the ZX-100's enable pin?") == \
        ["zx-100", "enable", "pin"]
    assert pm.query_words("what did I mark in the datasheet") == []
    assert pm.query_words("EN en 5.5 V") == ["en", "5.5"]


def test_score_counts_each_word_once_plus_the_datasheets_name():
    mark = {"text": "EN to GND -0.3 V to VIN", "written": "check EN pull-down", "near": ""}
    words = pm.query_words("EN pull-down")
    assert pm.score(words, mark) == 2  # "en" twice in the mark: counted once
    assert pm.score(pm.query_words("pulldown"), mark) == 1  # without the hyphen
    assert pm.score(pm.query_words("voltage"), {"text": "Input voltage range"}) == 1
    assert pm.score(pm.query_words("volt"), {"text": "Input voltage range"}) == 1  # a prefix
    assert pm.score(pm.query_words("pins"), {"near": "EN pin"}) == 1  # a plural
    assert pm.score(pm.query_words("100"), {"text": "1000 uF"}) == 0  # numbers: whole only
    assert pm.score(pm.query_words("zebra"), mark) == 0
    # the datasheet named: every mark of it qualifies, one that also matches ranks higher
    named = pm.query_words("ZX-100 enable pin")
    assert pm.score(named, {"text": "Output current"}, "ZX-100 Buck", "zx100.pdf") == 1
    assert pm.score(named, {"near": "EN pin"}, "ZX-100 Buck", "zx100.pdf") == 2
    assert pm.score(named, {"near": "EN pin"}, "Bench", "ab-200.pdf") == 1
    assert pm.score(named, {"text": "x"}, "", "zx100-datasheet.pdf") == 1  # the PDF name


def test_part_numbers_count_like_the_title():
    words = pm.query_words("QX7720 enable pin")
    assert pm.score(words, {"text": "Output current"}, "Bench", "a.pdf") == 0
    assert pm.score(words, {"text": "Output current"}, "Bench", "a.pdf", ["QX7720"]) == 1
    assert pm.score(words, {"near": "EN pin"}, "Bench", "a.pdf", ["QX-7720"]) == 2
    assert pm.query_words("the circled values and arrows") == ["values"]


def test_a_block_shows_the_parts_circles_and_arrows():
    hit = {"note_id": "n1", "title": "Buck", "pdf_name": "b.pdf",
           "parts": ["ZX-100", "ZX-100A"],
           "mark": {"id": "m-a", "page_id": "pd1", "page_no": 3, "kind": "arrow",
                    "text": "EN to GND", "near": "", "written": "", "reading": "none"}}
    lines = brain_datasheets.block(hit).splitlines()
    assert lines[0] == "Buck [ZX-100, ZX-100A] (b.pdf), page 3, arrow"
    assert lines[1] == "  points to: EN to GND"
    hit["mark"] = {**hit["mark"], "kind": "circle", "text": "1.2 A", "near": "Output",
                   "points_to": True}
    lines = brain_datasheets.block({**hit, "parts": []}).splitlines()
    assert lines[:3] == ["Buck (b.pdf), page 3, circled", "  covers: 1.2 A",
                         "  points to: Output"]
    hit["mark"] = {**hit["mark"], "kind": "ink", "text": "", "near": "EN pin",
                   "written": "pull down", "reading": "read", "points_to": True}
    assert "  points to: EN pin" in brain_datasheets.block(hit)


# ------------------------------------------------------------ fictional notes
@pytest.fixture
def brain(monkeypatch, data_dir):
    from fork.ink import mark_reading
    from fork.ink import notes as ink
    from src import tool_utils
    from src.upload_handler import UploadHandler
    from tests.fork.brain_fakes import notes_db

    b = Brain(monkeypatch, data_dir, admins=("alice", "bob"))
    notes_db(monkeypatch)  # after Brain(): the notes are this test's own
    uploads = data_dir / "uploads"
    uploads.mkdir()
    monkeypatch.setattr(tool_utils, "_upload_handler", UploadHandler(str(data_dir), str(uploads)))
    monkeypatch.setattr(ink, "_LOCKS", {})
    monkeypatch.setattr(pm, "_CACHE", type(pm._CACHE)())
    monkeypatch.setattr(mark_reading, "schedule", lambda *a, **k: None)  # no model here
    return b


def _span(words, *texts):
    seq = [w.text for w in words]
    for i in range(len(seq) - len(texts) + 1):
        if tuple(seq[i:i + len(texts)]) == texts:
            run = words[i:i + len(texts)]
            return (min(w.x0 for w in run), min(w.y0 for w in run),
                    max(w.x1 for w in run), max(w.y1 for w in run))
    raise AssertionError(f"{texts} not on the page: {seq}")


def _stroke(sid, pen, pts, at):
    return {"id": sid, "pen": pen, "at": at,
            "points": [[x, y, i * 16] for i, (x, y) in enumerate(pts)]}


def datasheet_note(owner="alice", name="zx100-datasheet.pdf", title=None):
    """A fictional ZX-100 datasheet: the input range highlighted on page 1, a note
    written beside the EN line on page 2 (corrected by the owner)."""
    from fork.ink import mark_reading
    from fork.ink import notes as ink
    from fork.ink import pdf as ink_pdf
    from tests.fork.pdf_fixtures import datasheet

    note_id = str(uuid.uuid4())
    note = ink.add_pdf(owner, note_id, datasheet(), name=name, title=title, edited_at=1000)
    path = ink.pdf_file(owner, note_id)
    w1, w2 = ink_pdf.page_words(path, 1), ink_pdf.page_words(path, 2)
    hx0, hy0, hx1, hy1 = _span(w1, "2.7", "V", "to", "5.5", "V")
    _, ly0, lx1, ly1 = _span(w2, "EN", "to", "GND", "-0.3", "V", "to", "VIN")
    mid = (hy0 + hy1) / 2
    strokes = {0: [_stroke("hl1", YELLOW, [(hx0 + 2, mid), (hx1 - 2, mid)], at=2000)],
               1: [_stroke("hw1", PEN, [(lx1 + 40, (ly0 + ly1) / 2), (lx1 + 60, ly1),
                                        (lx1 + 80, ly0)], at=5000),
                   _stroke("hw2", PEN, [(lx1 + 90, ly0), (lx1 + 110, ly1)], at=5100)]}
    body = {"paper": note["paper"], "edited_at": note["edited_at"] + 1,
            "pages": [{"id": p["id"], "width": p["width"], "height": p["height"],
                       "strokes": p["strokes"] + strokes.get(i, [])}
                      for i, p in enumerate(note["pages"])]}
    ink.save(owner, note_id, body)
    mark_reading.correct(owner, note_id, "m-hw1", "check EN pull-down")
    return note_id, [p["id"] for p in note["pages"]]


def _run(owner="alice", offered=(TOOL,), **kw):
    return brain_tools.build_run(request_text="what did I note?", owner=owner,
                                 session_id="c1", history=[], offered=offered,
                                 disabled=set(), **kw)


async def test_rank_finds_the_note_beside_the_en_line(brain):
    note_id, pages = datasheet_note()
    hits = pm.rank("alice", "EN pull-down")
    assert [h["mark"]["id"] for h in hits] == ["m-hw1"]
    hit = hits[0]
    assert hit["note_id"] == note_id and hit["pdf_name"] == "zx100-datasheet.pdf"
    assert hit["mark"]["written"] == "check EN pull-down" and hit["mark"]["reading"] == "reviewed"
    assert hit["mark"]["page_id"] == pages[1]
    # naming the datasheet lists all its marks, the better match first
    named = pm.rank("alice", "what did I note about the ZX-100's enable pin?")
    assert [h["mark"]["id"] for h in named] == ["m-hw1", "m-hl1"]
    named = pm.rank("alice", "ZX-100 5.5 V")
    assert [h["mark"]["id"] for h in named] == ["m-hl1", "m-hw1"]
    assert pm.rank("alice", "ZX-100", limit=1)[0]["mark"]["id"] == "m-hw1"  # newest
    assert pm.rank("alice", "the datasheet") == []  # no words left
    assert pm.rank("bob", "EN pull-down") == []  # another owner's marks


async def test_the_tool_answers_with_the_mark_and_its_link(brain):
    note_id, pages = datasheet_note(title="ZX-100 Buck Regulator")
    run = _run()
    _, desc, result = await brain_tools.execute(run, TOOL, json.dumps({"query": "ZX-100 enable pin"}))
    out = result["output"]
    assert desc == f"{TOOL}: 2 result(s)" and result["exit_code"] == 0
    reminder, first, second = out.split("\n\n")
    assert reminder == brain_datasheets.LINK_REMINDER  # first, so trimming never drops it
    # The part number guessed on import (fork/ink/parts.py) in brackets after the title.
    assert first.splitlines()[0] == \
        "ZX-100 Buck Regulator [ZX-100] (zx100-datasheet.pdf), page 2, handwritten note"
    assert "  you wrote: check EN pull-down (corrected by you)" in first
    assert "  beside: EN to GND -0.3 V to VIN" in first
    assert f"[open](/#open=write&note={note_id}&page={pages[1]}&mark=m-hw1)" in first
    assert second.startswith("ZX-100 Buck Regulator [ZX-100] (zx100-datasheet.pdf), page 1, "
                             "highlight")
    assert "  covers: 2.7 V to 5.5 V" in second and "you wrote" not in second
    assert "untrusted_content" not in result  # popped by execute: the run is armed
    assert run.tainted and run.security.external_untrusted_context_seen
    assert TOOL in run.security.external_sources


async def test_an_unread_note_says_so_and_nothing_found_says_so(brain, monkeypatch):
    datasheet_note()
    monkeypatch.setattr("fork.ink.mark_reading.readings_of", lambda stored: {})
    run = _run()
    _, _, result = await brain_tools.execute(run, TOOL, json.dumps({"query": "EN GND"}))
    assert "what you wrote isn't read yet" in result["output"]
    run = _run()
    _, desc, result = await brain_tools.execute(run, TOOL, json.dumps({"query": "zebra"}))
    assert result["output"] == brain_datasheets.NOTHING and desc.endswith("0 result(s)")
    assert run.tainted is False  # nothing came back: nothing to distrust
    _, _, result = await brain_tools.execute(run, TOOL, json.dumps({"query": " "}))
    assert result["exit_code"] == 1 and "query" in result["error"].casefold()


async def test_limit_is_clamped_and_the_search_runs_for_the_runs_owner(brain, monkeypatch):
    asked = []
    monkeypatch.setattr(pm, "rank", lambda owner, q, limit: asked.append((owner, q, limit)) or [])
    for limit, want in ((50, 20), (0, 10), ("x", 10), (3, 3)):
        await brain_tools.execute(_run(owner="bob"), TOOL,
                                  json.dumps({"query": "EN pin", "limit": limit}))
        assert asked[-1] == ("bob", "EN pin", want)


async def test_another_owners_marks_never_come_back_through_a_turn(brain):
    datasheet_note(owner="alice")
    sess = brain.session(sid="chat-b", owner="bob")
    brain.llm.rounds([calls((TOOL, {"query": "ZX-100 EN pull-down"}))], [text("Nothing.")])
    await brain.run(sess, "what did I note about the ZX-100?", owner="bob")
    assert TOOL in toolset.names(brain.llm.requests[0]["tools"])
    tool_msg = [m for m in brain.llm.requests[1]["messages"] if m.get("role") == "tool"][-1]
    assert "pull-down" not in tool_msg["content"]
    assert brain_datasheets.NOTHING in tool_msg["content"]
    assert brain.ran == []  # runs in fork code, not upstream's runner


# ---------------------------------------------------------------- who gets it
def test_read_only_and_in_its_own_default_group():
    from fork.brain.settings import DEFAULT_GROUPS
    from fork.common import settings

    assert toolset.is_read(TOOL, json.dumps({"query": "x"})) and toolset.can_read(TOOL)
    assert TOOL in toolset.FORK_TOOLS
    assert toolset.GROUPS["datasheets"] == (TOOL,)
    assert "datasheets" in DEFAULT_GROUPS
    assert "datasheets" in settings.BRAIN_TOOL_GROUPS.default.split(",")
    assert len(json.dumps(brain_datasheets.SCHEMA)) // 4 < 200


@pytest.mark.parametrize("case", ["token", "non_admin", "global", "global_notes"])
def test_not_for_tokens_non_admins_or_when_disabled(monkeypatch, case):
    monkeypatch.setattr("src.tool_security.owner_is_admin_or_single_user",
                        lambda o: case != "non_admin")
    off_list = {"global": [TOOL], "global_notes": ["manage_notes"]}.get(case)
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: (
        off_list if off_list and key == "disabled_tools" else default))
    off = brain_tools.turn_disabled_tools(None, "alice", delegated=case == "token")
    assert TOOL in off


def test_stays_for_an_admin(monkeypatch):
    monkeypatch.setattr("src.tool_security.owner_is_admin_or_single_user", lambda o: True)
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)
    # the memory privilege is about memory, not the owner's datasheets
    off = brain_tools.turn_disabled_tools(None, "alice", privileges={"can_manage_memory": False})
    assert TOOL not in off and "search_beliefs" in off


async def test_offered_to_the_owner_not_to_a_token_turn(brain):
    sess = brain.session()
    brain.llm.rounds([text("ok")], [text("ok")])
    await brain.run(sess, "hello")
    assert TOOL in toolset.names(brain.llm.requests[0]["tools"])
    brain.mp.setattr("src.auth_helpers.is_delegated_credential", lambda request: True)
    toolset.forget_chat(sess.id)
    await brain.run(sess, "hello")
    assert TOOL not in toolset.names(brain.llm.requests[1]["tools"])


def test_the_prompt_says_when_to_use_it_and_to_link_the_mark():
    p = brain_prompt.SYSTEM_PROMPT
    assert TOOL in p and "[open] link" in p


# ---------------------------------------------------------------------- voice
async def _voice_call(owner, personal):
    from fork.voice.policy import PRIVATE_REPLY, VoiceGate

    gate = VoiceGate(owner=owner, session_id="v-1", turn_id="t-1", personal=personal)
    run = _run(owner=owner, read_only=True, gate=gate)
    _, desc, result = await brain_tools.execute(run, TOOL, json.dumps({"query": "EN pull-down"}))
    return desc, result, PRIVATE_REPLY


async def test_a_voice_turn_reads_marks_for_the_owner_only(brain):
    from fork.voice.policy import is_personal

    datasheet_note()
    _, result, _ = await _voice_call("alice", personal=True)
    assert "check EN pull-down" in result["output"]
    _, result, private = await _voice_call("alice", personal=False)
    assert result.get("blocked") and result["error"] == private
    assert "output" not in result
    assert is_personal(TOOL)


async def test_a_turn_finds_the_mark_end_to_end(brain):
    """The model asks, the tool answers from the owner's marks, the gate is armed."""
    datasheet_note(title="ZX-100 Buck Regulator")
    sess = brain.session()
    brain.llm.rounds([calls((TOOL, {"query": "ZX-100 enable pin"}))],
                     [text("You noted: check EN pull-down.")])
    t = brain.start(sess, "what did I note about the ZX-100's enable pin?")
    [_ async for _ in brain_turn._run_new_turn(t)]
    tool_msg = [m for m in brain.llm.requests[1]["messages"] if m.get("role") == "tool"][-1]
    assert "check EN pull-down (corrected by you)" in tool_msg["content"]
    assert "/#open=write&note=" in tool_msg["content"]
    assert t.run.tainted is True


async def test_rank_finds_a_datasheet_by_a_part_number_tag(brain):
    from fork.ink import parts as ink_parts

    note_id, _ = datasheet_note(name="bench.pdf", title="Bench notes")
    assert pm.rank("alice", "QX7720") == []
    ink_parts.set_parts("alice", note_id, ["QX7720"])
    hits = pm.rank("alice", "QX7720 pull-down")
    assert [h["mark"]["id"] for h in hits] == ["m-hw1", "m-hl1"]
    assert hits[0]["parts"] == ["QX7720"] and hits[0]["score"] == 2  # tag + the note
