"""Fork: the brain's check for invented actions (fork/brain/claims.py, the end of
fork/brain/turn.py's turn): a change the answer claims and no successful write of the
turn made gets one note after the streamed answer, saved with it. Fake model and tools
(tests/fork/brain_fakes.py); fictional data."""
from __future__ import annotations

import random
import time

import pytest

from fork.brain import claims as brain_claims
from fork.brain import turn as brain_turn
from tests.fork.brain_fakes import Brain, answer_text, calls, of, text

UPDATE = "mcp__convo__update_action_item"
CALENDAR_ADD = ("manage_calendar", {"action": "create_event", "summary": "Lunch with Maria",
                                    "dtstart": "2026-11-06T12:00"})
NOTE_ADD = ("manage_notes", {"action": "create", "title": "Buy oat milk"})
NO_CALENDAR = "\n\nNote: nothing was changed in this turn: no calendar event was added or changed."


@pytest.fixture
def brain(monkeypatch, data_dir):
    return Brain(monkeypatch, data_dir)


def _note_events(events):
    return [e for e in events if isinstance(e.get("delta"), str) and "Note:" in e["delta"]]


async def _approve_next(brain, sess, events):
    approval_id = of(events, "ask_user")[0]["data"]["approval_id"]
    return await brain.resume(approval_id, sess)


# ------------------------------------------------------------------- the turn
async def test_a_claimed_calendar_add_without_a_write_is_corrected_streamed_and_saved(brain):
    sess = brain.session()
    claim = "I've added lunch with Maria to your calendar for Friday at noon."
    brain.llm.rounds([text("Sure. "), text(claim)])
    events = await brain.run(sess, "put lunch with Maria on Friday on my calendar")
    notes = _note_events(events)
    assert [e["delta"] for e in notes] == [NO_CALENDAR]
    # the note is the last text, after the answer and before metrics
    deltas = [i for i, e in enumerate(events) if "delta" in e]
    metrics = [i for i, e in enumerate(events) if e.get("type") == "metrics"]
    assert deltas[-1] == events.index(notes[0]) and deltas[-1] < metrics[0]
    assert answer_text(events) == "Sure. " + claim + NO_CALENDAR
    saved = sess.history[-1]
    assert saved.content == "Sure. " + claim + NO_CALENDAR  # follow-ups see it
    assert saved.metadata["round_texts"][-1].endswith(NO_CALENDAR)


async def test_a_claim_backed_by_an_approved_write_is_not_corrected(brain):
    sess = brain.session()
    brain.llm.rounds([calls(CALENDAR_ADD)])
    events = await brain.run(sess, "put lunch with Maria on Friday on my calendar")
    brain.llm.rounds([text("I've added lunch with Maria to your calendar. Done.")])
    resumed = await _approve_next(brain, sess, events)
    assert [r["tool"] for r in brain.ran] == ["manage_calendar"]
    assert _note_events(resumed) == []
    assert sess.history[-1].content == "I've added lunch with Maria to your calendar. Done."


async def test_a_failed_write_and_a_claim_is_corrected(brain):
    sess = brain.session()
    brain.results["manage_calendar"] = {"error": "Could not parse dtstart", "exit_code": 1}
    brain.llm.rounds([calls(CALENDAR_ADD)])
    events = await brain.run(sess, "put lunch with Maria on Friday on my calendar")
    brain.llm.rounds([text("It's been added to your calendar.")])
    resumed = await _approve_next(brain, sess, events)
    assert [e["delta"] for e in _note_events(resumed)] == [NO_CALENDAR]
    assert sess.history[-1].content == "It's been added to your calendar." + NO_CALENDAR


async def test_a_blocked_write_and_a_claim_is_corrected(brain):
    sess = brain.session()
    # refused before it asks: a delete that names no document (fork/brain/documents.py)
    brain.llm.rounds([calls(("manage_documents", {"action": "delete"}))],
                     [text("I've deleted the document.")])
    events = await brain.run(sess, "delete my document")
    assert brain.ran == [] and of(events, "tool_output")[0]["blocked"] is True
    note = "\n\nNote: nothing was changed in this turn: no document was created or changed."
    assert [e["delta"] for e in _note_events(events)] == [note]


async def test_a_claim_of_another_store_than_the_write_is_corrected(brain):
    sess = brain.session()
    brain.llm.rounds([calls(CALENDAR_ADD)])
    events = await brain.run(sess, "lunch with Maria on Friday, and tell her")
    brain.llm.rounds([text("I've added lunch to your calendar and sent Maria an email.")])
    resumed = await _approve_next(brain, sess, events)
    note = "\n\nNote: no email was sent or drafted in this turn."
    assert [e["delta"] for e in _note_events(resumed)] == [note]


async def test_an_action_item_claim_needs_athenas_update(brain):
    sess = brain.session()
    brain.llm.rounds([calls((UPDATE, {"id": 7}))])
    events = await brain.run(sess, "mark action item 7 done")
    brain.llm.rounds([text("I've marked action item 7 as done.")])
    resumed = await _approve_next(brain, sess, events)
    assert _note_events(resumed) == []

    sess = brain.session(sid="chat-2")
    brain.llm.rounds([calls(CALENDAR_ADD)])
    events = await brain.run(sess, "lunch on Friday, and close item 7")
    brain.llm.rounds([text("I've marked action item 7 as done.")])
    resumed = await _approve_next(brain, sess, events)
    assert [e["delta"] for e in _note_events(resumed)] == [
        "\n\nNote: no action item was updated in this turn."]


async def test_writes_before_a_pause_still_back_the_answer_after_it(brain):
    """Two writes, two approvals: the first write's success survives the second pause."""
    sess = brain.session()
    brain.llm.rounds([calls(CALENDAR_ADD)])
    first = await brain.run(sess, "lunch with Maria on Friday, and a note to buy oat milk")
    brain.llm.rounds([calls(NOTE_ADD)])
    second = await _approve_next(brain, sess, first)
    assert of(second, "ask_user")  # paused again, on the note
    brain.llm.rounds([text("I've added lunch to your calendar and saved the note.")])
    resumed = await _approve_next(brain, sess, second)
    assert [r["tool"] for r in brain.ran] == ["manage_calendar", "manage_notes"]
    assert _note_events(resumed) == []
    assert sess.history[-1].content == "I've added lunch to your calendar and saved the note."


async def test_a_bare_done_without_any_write_is_corrected(brain):
    sess = brain.session()
    brain.llm.rounds([text("Done.")])
    events = await brain.run(sess, "add milk to my list")
    assert [e["delta"] for e in _note_events(events)] == [
        "\n\nNote: nothing was changed in this turn."]


async def test_the_fallback_done_after_a_failed_write_carries_the_note(brain):
    sess = brain.session()
    brain.results["manage_calendar"] = {"error": "Calendar unavailable", "exit_code": 1}
    brain.llm.rounds([calls(CALENDAR_ADD)])
    events = await brain.run(sess, "lunch with Maria on Friday")
    brain.llm.rounds([text("")])
    resumed = await _approve_next(brain, sess, events)
    finals = of(resumed, "final_response")
    assert [f["content"] for f in finals] == ["Done.\n\nNote: nothing was changed in this turn."]
    assert _note_events(resumed) == []  # one note, inside the final answer
    assert sess.history[-1].content == finals[0]["content"]


async def test_a_timed_out_approval_says_only_not_approved(brain):
    sess = brain.session()
    brain.llm.rounds([text("I've added lunch to your calendar."), calls(CALENDAR_ADD)])
    await brain.run(sess, "lunch with Maria on Friday")
    assert brain_turn.expire_pending(now=time.time() + brain_turn.PENDING_SECONDS + 1) == 1
    assert sess.history[-1].content == brain_turn.NOT_APPROVED
    assert "Note:" not in sess.history[-1].content


async def test_an_answer_without_claims_gets_no_note(brain):
    sess = brain.session()
    brain.llm.rounds([text("Shall I add lunch with Maria to your calendar? "
                           "I can create a reminder too.")])
    events = await brain.run(sess, "lunch with Maria on Friday?")
    assert _note_events(events) == []


async def test_text_still_streams_before_the_turn_ends_with_a_claim(brain):
    sess = brain.session()
    state = {"model_done": False, "seen_before_done": None}

    async def slow_model(url, model, messages, **kw):
        yield text("I've added lunch")
        yield text(" to your calendar.")
        state["model_done"] = True
        yield "data: [DONE]\n\n"

    brain.mp.setattr("src.llm_core.stream_llm", slow_model)
    t = brain.start(sess, "lunch on Friday")
    chunks = []
    async for chunk in brain_turn._run_new_turn(t):
        chunks.append(chunk)
        if '"delta"' in chunk and state["seen_before_done"] is None:
            state["seen_before_done"] = not state["model_done"]
    assert state["seen_before_done"] is True
    assert sum(c.startswith("data: {\"delta\"") and "Note:" in c for c in chunks) == 1


# ------------------------------------------------------------------- the rules
MUST_NOT = [
    'You created the "Dentist" event last week.',
    "Maria sent the agenda on Monday.",
    "I sent you the link earlier.",
    "Yesterday I added the dentist appointment to your calendar.",
    "I've already added that to your calendar.",
    "Shall I add it to your calendar?",
    "Would you like me to save this as a note?",
    "I can create a note for that.",
    "I'll add it once you approve.",
    "If you want, I could schedule it for Monday.",
    "I didn't send the email.",
    "I haven't created any event.",
    "No event was created.",
    "Nothing was saved.",
    "I added nothing to your calendar.",
    "I found three events on Friday.",
    "I checked your notes; there is nothing about the trip.",
    'Your note says "I\'ve added the milk to the list."',
    "Your note says “I've booked the flights.”",
    "```\nI've saved it to your notes\n```",
    "The meeting was moved to Thursday, according to the transcript.",
    "Tom said he has already sent the invoice.",
    "Here's a draft I wrote for you:",
    "I've drafted a reply below.",
    "I've added a few details to the paragraph.",
    "I've created a summary of the meeting.",
    "I've edited your draft to sound friendlier.",
    "I've updated my answer.",
    "I put together a short summary.",
    "Updated forecast for Friday: rain.",
    "Sent by Maria on Tuesday.",
    "Let me know if I've missed anything.",
    "When I checked, the event was already there.",
    "Done reading your notes, nothing about the trip.",
    "In JavaScript, write <function name> like this: function f() {}. Done.",
]

MUST = [
    ("I've added lunch with Maria to your calendar for Friday at noon.", [{"calendar"}]),
    ("I just scheduled the dentist for Tuesday.", [{"calendar"}]),
    ("It's been added to your calendar.", [{"calendar"}]),
    ("It's now on your calendar.", [{"calendar"}]),
    ("The reminder has been saved.", [{"notes"}]),
    ("No problem, I've added it to your to-do list.", [{"notes"}]),
    ("- **Added** oat milk to your shopping list", [{"notes"}]),
    ("I've sent the email to Tom.", [{"email"}]),
    ("I've replied to Tom.", [{"email"}]),
    ("We've saved that to memory.", [{"memory"}]),
    ("I successfully updated the contact.", [{"contacts"}]),
    ("I've created the document.", [{"documents"}]),
    ("I've marked action item 7 as done.", [{"action_items"}]),
    ("I've gone ahead and created the note.", [{"notes"}]),
    ("I've created the event, and sent Maria an invite.", [{"calendar"}, {"calendar"}]),
    ("Saved.", [{""}]),
    ("Done.", [{""}]),
    ("All set!", [{""}]),
    ("You're all set.", [{""}]),
    ("I've deleted it.", [{""}]),
]


@pytest.mark.parametrize("statement", MUST_NOT)
def test_wordings_that_claim_no_change_of_this_turn(statement):
    # a closing "Done." is kept only as a maybe: a claim next to a write that failed
    assert all(c == {brain_claims.LATE_DONE} for c in brain_claims.claims(statement))
    assert brain_claims.correction(statement, []) == ""


@pytest.mark.parametrize("statement,found", MUST)
def test_wordings_that_claim_a_change(statement, found):
    assert brain_claims.claims(statement) == found


def test_the_note_names_what_wasnt_done():
    say = brain_claims.correction
    assert say("Done.", []) == "\n\nNote: nothing was changed in this turn."
    assert say("Done.", [("manage_notes", False)]) == "\n\nNote: nothing was changed in this turn."
    assert say("Done.", [("manage_notes", True)]) == ""
    # a "Done." that ends an explanation claims a change only next to a write
    assert say("It works like this: f() {}. Done.", []) == ""
    assert say("It works like this: f() {}. Done.", [("manage_notes", False)]) == (
        "\n\nNote: nothing was changed in this turn.")
    assert say("It works like this: f() {}. Done.", [("manage_notes", True)]) == ""
    assert say("I've saved it.", [("generate_image", True)]) == ""  # any write backs "it"
    assert say("I've added it to your calendar and sent Tom an email.", []) == (
        "\n\nNote: nothing was changed in this turn: no calendar event was added or "
        "changed, and no email was sent or drafted.")
    assert say("I've added it to your calendar.", [("manage_notes", True)]) == (
        "\n\nNote: no calendar event was added or changed in this turn.")
    assert say("I've added it to your calendar.", [("manage_calendar", False),
                                                    ("manage_calendar", True)]) == ""
    assert say("I've marked action item 3 done.", [("mcp__athena__update_action_item", True)]) == ""


def test_tool_domains():
    assert brain_claims.tool_domain("manage_calendar") == "calendar"
    assert brain_claims.tool_domain("send_email") == "email"
    assert brain_claims.tool_domain("create_document") == "documents"
    assert brain_claims.tool_domain("mcp__convo__update_action_item") == "action_items"
    assert brain_claims.tool_domain("mcp__convo__erase_fact") is None


def test_writes_record_success_only_for_calls_that_ran_cleanly():
    t = brain_turn._Turn(session_id="s", owner="alice", message="m", sess=None,
                         deps=brain_turn._Deps(), endpoint_url="", model="", headers={},
                         temperature=None, max_tokens=None, character_name=None)
    note = brain_turn._note_write
    note(t, "web_search", "{}", {"exit_code": 0}, attempted=True)  # a read: not recorded
    note(t, "manage_calendar", '{"action": "list_events"}', {"exit_code": 0}, attempted=True)
    note(t, "manage_calendar", '{"action": "create_event"}', {"exit_code": 0}, attempted=True)
    note(t, "manage_calendar", '{"action": "create_event"}', {"exit_code": 1}, attempted=True)
    note(t, "manage_notes", '{"action": "create"}', {"error": "x"}, attempted=True)
    note(t, "manage_notes", '{"action": "create"}', {"blocked": True}, attempted=False)
    note(t, "manage_notes", '{"action": "create"}', {"exit_code": True}, attempted=True)
    assert t.writes == [("manage_calendar", True), ("manage_calendar", False),
                        ("manage_notes", False), ("manage_notes", False),
                        ("manage_notes", False)]


# ------------------------------------------------------------------- the cost
_PARA = ("On Friday you have lunch with Maria at noon, then the dentist at 3 pm. I found "
         "two notes about the trip; the first lists the hotel and the second the train "
         "times, and \"I've booked the hotel\" is what Tom wrote. ")


def _answers() -> dict[str, str]:
    rng = random.Random(7)
    paragraphs = "\n\n".join([_PARA * 3] * 80)[:19900] + \
        "\n\nI've added lunch with Maria to your calendar. Done."
    one_line = (_PARA * 120)[:19950] + " I've added it to your calendar."
    dense, n = [], 0
    while n < 20000:
        k, who = rng.randint(1, 999), rng.choice(["Maria", "Tom", "Wren", "Jose"])
        part = (f"I've added lunch {k} with {who} to your calendar, and sent {who} invite "
                f"{k}. Done. It's been saved to your notes as item {k}. ")
        dense.append(part)
        n += len(part)
    return {"paragraphs": paragraphs, "one line": one_line, "dense": "".join(dense)[:20000]}


@pytest.mark.parametrize("kind", ["paragraphs", "one line", "dense"])
def test_the_check_takes_well_under_5_ms_on_a_20k_answer(kind):
    answer = _answers()[kind]
    assert len(answer) >= 19_000
    writes = [("manage_notes", True)]
    assert brain_claims.correction(answer, writes)  # the claims are found
    best = float("inf")
    for _ in range(15):  # the best of a few runs: other work shares the CPU
        start = time.perf_counter()
        brain_claims.correction(answer, writes)
        best = min(best, time.perf_counter() - start)
    assert best < 0.005, f"{kind}: {best * 1000:.2f} ms"


async def test_the_brains_own_done_after_reads_only_gets_no_note(brain):
    """An empty answer after read tools becomes the brain's own "Done.": it claims no
    change, so no "nothing was changed" note follows it."""
    sess = brain.session()
    brain.llm.rounds([calls(("web_search", {"query": "lisbon weather"}))], [text("")])
    events = await brain.run(sess, "look up the lisbon weather")
    final = [e for e in events if e.get("type") == "final_response"]
    assert final and final[-1]["content"] == "Done."
    assert sess.history[-1].content == "Done."
