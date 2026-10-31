"""Fork: scripts/module_slash_eval.py without a live stack: its argument split, log line
and bakeoff arguments, and main() driving a fake ``bakeoff`` module (Athena's
``server/eval/bakeoff.py`` stands in as a few functions). Fictional data only.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "module_slash_eval.py"
_SPEC = importlib.util.spec_from_file_location("module_slash_eval", _PATH)
slash = importlib.util.module_from_spec(_SPEC)
sys.modules["module_slash_eval"] = slash
_SPEC.loader.exec_module(slash)

FAKE_BAKEOFF = '''
import json
import os
import sys

QUESTIONS = [("Who is Lena?", [], ["Lena"])]
CALLS = []


class _Response:
    def __init__(self, message):
        self.message = message

    def raise_for_status(self):
        pass

    def json(self):
        return {"message": self.message}


class _Client:
    def post(self, path, json):
        CALLS.append(["post", path, json])
        return _Response(f"<skill {path}> {json['request']}")


def _odysseus_client():
    return _Client()


def run_odysseus(message):
    CALLS.append(["ask", message])
    log = os.environ.get("FAKE_ODYSSEUS_LOG")
    if log:
        with open(log, "a") as f:
            f.write("tools_sent=4 Exact approval required [skill-pin] tools_sent=2\\n")
    return f"answer: {message}", ["search_transcripts"]


def main():
    CALLS.append(["argv", list(sys.argv)])
    for question, _ids, _words in QUESTIONS:
        answer, tools = run_odysseus(question)
        print(f"{question} -> {answer} ({','.join(tools)})")
    print(json.dumps(CALLS))
    return 0
'''


def test_split_args():
    assert slash.split_args(["--skill", "sound"]) == (["--skill", "sound"], [])
    assert slash.split_args(["--extra", "--", "--only", "1", "--", "x"]) == (
        ["--extra"], ["--only", "1", "--", "x"])
    argv = ["--", "--only"]
    slash.split_args(argv)
    assert argv == ["--", "--only"]  # the caller's list is left alone


def test_log_line():
    seg = ("tools_sent=3 Exact approval required\n[skill-pin] Exact approval required\n"
           + "".join(f"tools_sent={n}\n" for n in range(4, 10)))
    assert slash.log_line(seg) == "  approvals=2 tools_sent=3,4,5,6,7,8 pinned=True"
    assert slash.log_line("") == "  approvals=0 tools_sent= pinned=False"


def test_bakeoff_argv():
    assert slash.bakeoff_argv(Path("eval/bakeoff.py"), ["--only", "4"]) == [
        "eval/bakeoff.py", "--frontend", "odysseus", "--only", "4"]


@pytest.fixture
def fake_bakeoff(tmp_path, monkeypatch):
    path = tmp_path / "eval" / "bakeoff.py"
    path.parent.mkdir()
    path.write_text(FAKE_BAKEOFF, encoding="utf-8")
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    monkeypatch.delitem(sys.modules, "bakeoff", raising=False)
    yield path
    sys.modules.pop("bakeoff", None)


def test_main_sends_each_question_as_the_slash_message(fake_bakeoff, tmp_path, monkeypatch,
                                                         capsys):
    log = tmp_path / "odysseus.log"
    log.write_text("before the run tools_sent=99 Exact approval required\n")
    monkeypatch.setenv("FAKE_ODYSSEUS_LOG", str(log))
    assert slash.main(["--skill", "sound", "--bakeoff", str(fake_bakeoff), "--log", str(log),
                       "--extra", "--", "--only", "1"]) == 0
    out = capsys.readouterr().out.splitlines()
    sent = "<skill /api/skills/sound/invoke>"
    assert out[0] == "  approvals=1 tools_sent=4,2 pinned=True"  # this question's log only
    assert out[1] == f"Who is Lena? -> answer: {sent} Who is Lena? (search_transcripts)"
    assert out[3] == (f"{slash.EXTRA[0]} -> answer: {sent} {slash.EXTRA[0]} "
                      "(search_transcripts)")
    calls = sys.modules["bakeoff"].CALLS
    assert calls[0] == ["argv", [str(fake_bakeoff), "--frontend", "odysseus", "--only", "1"]]
    assert calls[1] == ["post", "/api/skills/sound/invoke", {"request": "Who is Lena?"}]
    assert calls[2] == ["ask", f"{sent} Who is Lena?"]
    assert sys.modules["bakeoff"].QUESTIONS[-1] == slash.EXTRA


def test_main_without_skill_asks_plainly(fake_bakeoff, capsys):
    assert slash.main(["--bakeoff", str(fake_bakeoff)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "Who is Lena? -> answer: Who is Lena? (search_transcripts)"
    assert sys.modules["bakeoff"].CALLS == [
        ["argv", [str(fake_bakeoff), "--frontend", "odysseus"]], ["ask", "Who is Lena?"]]
