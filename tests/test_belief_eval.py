"""Fork: the belief-ledger memory eval (scripts/belief_eval.py) through pytest.

Same seeded history and checks as the script, in a temp DATA_DIR. Fictional data only.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "belief_eval.py"
_SPEC = importlib.util.spec_from_file_location("belief_eval", _PATH)
belief_eval = importlib.util.module_from_spec(_SPEC)
sys.modules["belief_eval"] = belief_eval
_SPEC.loader.exec_module(belief_eval)


@pytest.fixture(scope="module")
def checks(tmp_path_factory):
    return belief_eval.run_eval(tmp_path_factory.mktemp("belief-eval"))


@pytest.mark.parametrize("case", list(belief_eval.CASES))
def test_case(checks, case):
    mine = [c for c in checks if c.case == case]
    assert mine, f"no checks for {case}"
    failed = [f"{c.step}: expected {c.expected!r}, got {c.actual!r}" for c in mine if not c.ok]
    assert not failed, "\n".join(failed)


def test_every_check_belongs_to_a_case(checks):
    assert {c.case for c in checks} == set(belief_eval.CASES)


def test_main_scores_keeps_the_dir_and_restores_settings(tmp_path, monkeypatch, capsys):
    from src import constants

    monkeypatch.setenv("BELIEF_STALE_DAYS", "5")  # the eval runs on the defaults
    data_dir_before = constants.DATA_DIR
    assert belief_eval.main(["--data-dir", str(tmp_path), "--keep"]) == 0
    out = capsys.readouterr().out
    n = len(belief_eval.CASES)
    assert f"cases: {n}/{n}" in out
    score = out.split("score ")[1].split()[0]
    passed, total = score.split("/")
    assert passed == total and int(total) > 50
    assert (tmp_path / "beliefs.db").exists() and (tmp_path / "memory.json").exists()
    assert "ledger kept:" in out
    assert constants.DATA_DIR == data_dir_before
    assert os.environ["BELIEF_STALE_DAYS"] == "5"
    # Seeding the same owner twice would not be the same history: refused.
    assert belief_eval.main(["--data-dir", str(tmp_path)]) == 2
    # Another owner in the same dir is a fresh history.
    assert belief_eval.main(["--data-dir", str(tmp_path), "--owner", "eval-2"]) == 0


def test_a_mismatch_exits_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(belief_eval, "CONFIRMED", belief_eval.CONFIRMED + ("Not a belief",))
    assert belief_eval.main(["--data-dir", str(tmp_path)]) == 1
    assert "FAIL memory: the expected confirmed set is in memory" in capsys.readouterr().out


def test_default_temp_dir_is_removed(monkeypatch, tmp_path, capsys):
    made = []
    real = belief_eval.tempfile.mkdtemp

    def mkdtemp(**kw):
        made.append(real(dir=tmp_path, **kw))
        return made[-1]

    monkeypatch.setattr(belief_eval.tempfile, "mkdtemp", mkdtemp)
    assert belief_eval.main([]) == 0
    assert made and not Path(made[0]).exists()
    assert "ledger kept:" not in capsys.readouterr().out


def test_chat_needs_data_dir_and_bakeoff():
    with pytest.raises(SystemExit):
        belief_eval.main(["--chat"])


def test_score_answer():
    link = belief_eval.module_link(9004)
    answer = (f"Priya Nair is allergic to peanuts ({link}). Her birthday is on May 21st "
              f"(source: {belief_eval.module_link(9015)}).")
    got = {label: (fact, cited) for label, fact, cited in belief_eval.score_answer(answer)}
    assert got == {"peanut allergy": (True, True), "birthday (corrected)": (True, True),
                   "design review": (False, False)}


@pytest.mark.parametrize("text", [
    "Her birthday is on 21 May.", "her birthday is May 21", "On May 21st she turns 40",
    "the 21st of May", "born 2026-05-21", "21 MAY", "may 21.", "21st May", "May the 21st",
    "21 may, I think", "(May 21)",
])
def test_mentions_date_accepts_either_order(text):
    assert belief_eval.mentions_date(text, 5, 21)


@pytest.mark.parametrize("text", [
    "Her birthday is on 12 May.", "May 2", "May 210", "121 May", "21 Mayfair", "2026-05-12",
    "21 June", "maybe 21", "",
])
def test_mentions_date_rejects_other_dates(text):
    assert not belief_eval.mentions_date(text, 5, 21)


def test_mentions_date_old_birthday_and_september():
    assert belief_eval.mentions_date("It used to be May 12th", 5, 12)
    assert belief_eval.mentions_date("12 of May", 5, 12)
    assert belief_eval.mentions_date("Sept. 3", 9, 3)
    assert belief_eval.mentions_date("3 sep", 9, 3)


def test_link_matches_on_the_path_not_the_signed_url():
    link = belief_eval.module_link(9004)
    assert belief_eval.citation(link) == "/view/c9004"
    assert belief_eval.cites("see http://h:8090/view/c9004?sig=OTHER#t=1", link)
    assert belief_eval.cites("[source](/view/c9004)", link)
    assert not belief_eval.cites("see /view/c90041", link)
    assert not belief_eval.cites("from memory context", link)
    note = belief_eval.note_link("3")
    assert belief_eval.cites(f"(source: {note})", note)
    assert not belief_eval.cites("(source: /notes#belief-eval-note-30)", note)


# ------------------------------------------------------------------- cleanup
def test_cleanup_removes_exactly_what_the_eval_seeded(tmp_path, monkeypatch, capsys):
    from src import evidence_intake as ledger
    from src.memory import MemoryManager

    owner = belief_eval.OWNER
    t0 = belief_eval.at(10, 20)

    def other(source_id, text, who=owner, source="odysseus-sound"):
        ledger.submit(who, [ledger.Evidence(
            source=source, source_id=source_id, subject="Ana Silva", about="person",
            text=text, link=f"/view/c{source_id}", seen_first="2026-10-20",
            seen_last="2026-10-20", source_confirmed=True)], now=t0)

    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    other("15", "Ana Silva keeps bees")                       # before the eval, same owner
    other("16", "Ana Silva rows on Sundays", who="someone")   # another owner
    manager = MemoryManager(str(tmp_path))
    mine = manager.add_entry("My own note about the boat", owner=owner)
    manager.save([*manager.load_all_for_update(), mine])

    assert belief_eval.main(["--data-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "--cleanup" in out and str(tmp_path) in out
    manifest = belief_eval.manifest_path(tmp_path, owner)
    recorded = belief_eval.json.loads(manifest.read_text())
    assert recorded["owner"] == owner and len(recorded["belief_ids"]) == 17
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    other("17", "Ana Silva has a red kayak")                  # after the eval
    # A later source backs one eval belief: cleanup must leave that one alone.
    ledger.submit(owner, [ledger.Evidence(
        source="notes", source_id="real-note-1", subject="Tomas Berg", about="person",
        text=belief_eval.CELLO, link="/notes#real-note-1", seen_first="2026-10-20",
        seen_last="2026-10-20")], now=t0)

    assert belief_eval.main(["--cleanup", "--data-dir", str(tmp_path)]) == 0
    assert "cleanup: 16 beliefs, 1 kept, 0 missing" in capsys.readouterr().out
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    left = {b["text"] for b in ledger.list_beliefs(owner)}
    assert left == {"Ana Silva keeps bees", "Ana Silva has a red kayak", belief_eval.CELLO}
    assert {b["text"] for b in ledger.list_beliefs("someone")} == {"Ana Silva rows on Sundays"}
    texts = {(m.get("owner"), m["text"]) for m in MemoryManager(str(tmp_path)).load_all()}
    assert (owner, "My own note about the boat") in texts
    assert not any(t == belief_eval.OWN_MEMORY for _, t in texts)
    assert {t for o, t in texts if o == owner and "(source:" in t} == {
        "Ana Silva keeps bees (source: /view/c15)", "Ana Silva has a red kayak (source: /view/c17)",
        f"{belief_eval.CELLO} (source: {belief_eval.module_link(9003)})"}  # kept, confirmed by two sources; the transcript link outranks the note
    assert not manifest.exists()
    # Cleanup twice: nothing recorded any more.
    assert belief_eval.main(["--cleanup", "--data-dir", str(tmp_path)]) == 2


def test_cleanup_then_rerun_in_the_same_dir(tmp_path, capsys):
    assert belief_eval.main(["--data-dir", str(tmp_path), "--owner", "ann"]) == 0
    assert belief_eval.main(["--data-dir", str(tmp_path), "--owner", "ann"]) == 2
    assert belief_eval.main(["--cleanup", "--data-dir", str(tmp_path), "--owner", "ann"]) == 0
    assert belief_eval.main(["--data-dir", str(tmp_path), "--owner", "ann"]) == 0
    assert "score 80/80" in capsys.readouterr().out


def test_cleanup_needs_data_dir():
    with pytest.raises(SystemExit):
        belief_eval.main(["--cleanup"])
