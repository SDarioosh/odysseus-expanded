"""Fork: how the fork's background jobs call the local model, pinned.

For belief structure, notes evidence and today insight: what ``default_llm`` sends
(candidates asked for, the wait-for-quiet label, every keyword of the model call), how
the one-retry loop talks to the model for a table of answer sequences (messages sent,
result, log lines), and what each JSON parser returns for a table of raw answers.
Compared with ``fixtures/background_model_calls.json``, recorded before the copies
were replaced by ``fork/common/llm.py``.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

FIXTURE = Path(__file__).with_name("fixtures") / "background_model_calls.json"

ITEMS = '{"items": [{"n": 1, "attribute": "likes", "value": "tea"}]}'
FACTS = '{"facts": [{"subject": "Ava", "about": "person", "fact": "Ava likes tea."}]}'
HEADLINE = '{"headline": "Plan the day", "focus": [{"ref": "T1", "why": "due"}]}'

RAW = [
    None, "", "   ", "{", "}", "{}", "[]", "[{}]", "null", "plain words", "{bad json}",
    ITEMS, FACTS, HEADLINE, "```json\n" + ITEMS + "\n```", "```\n" + FACTS + "\n```\n",
    "```json" + HEADLINE + "```", "Sure! " + ITEMS + " Hope that helps.",
    "<think>thinking {not json}</think>" + FACTS, "<think>x</think>\n```json\n" + HEADLINE
    + "\n```", "<think>unclosed " + ITEMS, ITEMS + "\n" + FACTS, "{} " + HEADLINE,
    '{"items": {}}', '{"facts": "x"}', '{"headline": "  "}', '{"headline": 5}',
    '```json\n{"items": [\n```\n{"n": 1}]}\n```', "text\n```json\n" + FACTS + "\n```\nmore",
    '{"facts": [{"subject": "S", "about": "owner", "fact": "' + "x" * 400 + '"}]}',
    '{"items": [{"n": "1", "attribute": "likes", "value": "tea"}, {"n": 2}, "x"]}',
    '﻿' + HEADLINE, "```JSON\n" + FACTS + "\n```",
]

BELIEF = {"id": "b1", "subject": "Ava", "kind": "preference", "text": "Ava likes tea.",
          "last_seen": "2026-10-20"}
NOTE = None  # notes_evidence.build_messages is replaced; the note isn't read

SEQUENCES = {
    "good": ["{good}"], "bad_then_good": ["no json", "{good}"], "bad_twice": ["{", "[]"],
    "none_then_good": [None, "{good}"], "long_bad": ["y" * 3000, "{good}"],
}


class _Logs(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        if not record.name.startswith("fork"):
            return
        self.lines.append(f"{record.levelname} {record.name}: {record.getMessage()}")


def _parsers():
    from fork.beliefs import notes_evidence, structure
    from fork.insight import today

    return {"structure.parse_items": lambda raw: structure.parse_items(raw, [BELIEF]),
            "notes_evidence.parse_facts": notes_evidence.parse_facts,
            "today.parse_answer": today.parse_answer}


def _default_llms(model):
    from fork.beliefs import notes_evidence, structure
    from fork.insight import today

    return {
        "structure": lambda: structure.default_llm("alice"),
        "structure_no_owner": lambda: structure.default_llm(""),
        "notes_evidence": lambda: notes_evidence.default_llm("alice"),
        "today": lambda: today.default_llm("alice"),
        "today_refresh": lambda: today.default_llm(None, wait_quiet=False),
    }


async def _run_default(make, model, candidates):
    model.calls.clear()
    model.waited.clear()
    model.resolved.clear()
    model.candidates = candidates
    try:
        llm = await make()
    except Exception as exc:  # noqa: BLE001
        return {"raises": f"{type(exc).__name__}: {exc}", "resolved": list(model.resolved)}
    first = await llm([{"role": "user", "content": "one"}])
    second = await llm([{"role": "user", "content": "two"}])
    return {"answers": [first, second], "resolved": list(model.resolved),
            "waited": list(model.waited), "calls": list(model.calls)}


def observe(monkeypatch) -> dict:
    from types import SimpleNamespace

    import src.interactive_gate as gate
    import src.llm_core as llm_core  # noqa: PLR0402
    import src.task_endpoint as te
    from fork.beliefs import notes_evidence, structure
    from fork.insight import today

    model = SimpleNamespace(calls=[], waited=[], resolved=[], candidates=[])

    async def fake_wait(label):
        model.waited.append(label)

    async def fake_call(cands, messages, **kw):
        model.calls.append({"candidates": cands, "messages": messages, **kw})
        return "answer"

    def fake_resolve(**kw):
        model.resolved.append(kw)
        return model.candidates

    monkeypatch.setattr(gate, "wait_for_interactive_quiet", fake_wait)
    monkeypatch.setattr(llm_core, "llm_call_async_with_fallback", fake_call)
    monkeypatch.setattr(te, "resolve_task_candidates", fake_resolve)

    out: dict = {"default_llm": {}, "ask": {}, "parse": {}}
    cands = [["http://llm", "m1", {}], ["http://llm2", "m2", {"X": "1"}]]
    for batch in (None, "3", "10"):
        if batch is None:
            monkeypatch.delenv("BELIEF_STRUCTURE_BATCH", raising=False)
        else:
            monkeypatch.setenv("BELIEF_STRUCTURE_BATCH", batch)
        for label, make in _default_llms(model).items():
            if batch is not None and not label.startswith("structure"):
                continue
            key = label if batch is None else f"{label} batch={batch}"
            out["default_llm"][key] = asyncio.run(_run_default(make, model, cands))
            out["default_llm"][key + " no candidates"] = asyncio.run(
                _run_default(make, model, []))
    monkeypatch.delenv("BELIEF_STRUCTURE_BATCH", raising=False)

    # The one-retry loops, with their prompts replaced by a fixed one.
    q = [{"role": "user", "content": "Q"}]
    monkeypatch.setattr(structure, "build_messages", lambda batch: list(q))
    monkeypatch.setattr(notes_evidence, "build_messages", lambda note: list(q))
    monkeypatch.setattr(today, "build_messages", lambda inputs: list(q))
    monkeypatch.setattr(today, "validate", lambda answer, cands, extra=None: {
        s: list(answer.get(s) or []) for s in today.SECTIONS})
    goods = {"structure": ITEMS, "notes_evidence": FACTS, "today": HEADLINE,
             "today_no_candidates": HEADLINE, "today_empty_sections": '{"headline": "h"}',
             "today_empty_no_candidates": '{"headline": "h"}'}
    asks = {
        "structure": lambda llm: structure.ask(llm, [BELIEF]),
        "notes_evidence": lambda llm: notes_evidence.extract(llm, NOTE),
        "today": lambda llm: today.ask_model(llm, {"candidates": [{"ref": "T1"}]}),
        "today_no_candidates": lambda llm: today.ask_model(llm, {"candidates": []}),
        "today_empty_sections": lambda llm: today.ask_model(llm, {"candidates": [{"r": 1}]}),
        "today_empty_no_candidates": lambda llm: today.ask_model(llm, {"candidates": []}),
    }
    logs = _Logs()
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(logs)
    root.setLevel(logging.DEBUG)
    try:
        for label, ask in asks.items():
            for name, seq in SEQUENCES.items():
                answers = [goods[label] if a == "{good}" else a for a in seq]
                sent: list = []

                async def llm(messages, answers=answers, sent=sent):
                    sent.append(json.loads(json.dumps(messages)))
                    return answers[len(sent) - 1] if len(sent) <= len(answers) else "{"

                logs.lines.clear()
                result = asyncio.run(ask(llm))
                out["ask"][f"{label} {name}"] = {"result": result, "sent": sent,
                                                 "logs": list(logs.lines)}
    finally:
        root.removeHandler(logs)
        root.setLevel(old_level)

    for label, parse in _parsers().items():
        out["parse"][label] = {repr(raw): parse(raw) for raw in RAW}
    return json.loads(json.dumps(out))


def test_background_model_calls_as_recorded(monkeypatch):
    seen = observe(monkeypatch)
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for section in ("default_llm", "ask", "parse"):
        assert seen[section].keys() == expected[section].keys(), section
        for key, value in expected[section].items():
            assert seen[section][key] == value, (section, key)


if __name__ == "__main__":  # pragma: no cover -- records the fixture (old code only)
    mp = pytest.MonkeyPatch()
    try:
        FIXTURE.write_text(json.dumps(observe(mp), indent=1, sort_keys=True,
                                      ensure_ascii=True) + "\n", encoding="utf-8")
    finally:
        mp.undo()
