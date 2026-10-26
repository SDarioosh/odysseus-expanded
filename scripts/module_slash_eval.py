#!/usr/bin/env python3
"""Fork: run a module's bakeoff with every question sent as a slash command.

Wraps Athena's ``server/eval/bakeoff.py --frontend odysseus`` without changing
it: each question is turned into the message ``/<skill> <question>`` would send
(``POST /api/skills/<skill>/invoke``), then sent through bakeoff's Odysseus client.
Scoring and the results file are bakeoff's own (``eval/results/odysseus.jsonl``).
Without ``--skill`` the questions go in plain, like bakeoff itself.

``--extra`` adds the person + relative date question used in the sandbox eval
(docs/fork/sandbox.md, "Slash skills"); it comes after bakeoff's own questions
(question 22 since bakeoff's voice-note question 21; it was 21 before). It assumes the
fictional seed data (seed_demo.py, 2026-10-16 is a Friday).

Per question it prints the Odysseus log's approvals and ``tools_sent=`` counts when
``--log`` points at the Odysseus log.

Usage, from Athena's ``server/`` directory:
    ODYSSEUS_URL=http://127.0.0.1:7010 ODYSSEUS_PASSWORD_FILE=<file> \
      .venv/bin/python <odysseus fork>/scripts/module_slash_eval.py --skill sound \
      [--log <odysseus log>] [--extra] -- [bakeoff args, e.g. --only 1 4 16]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

EXTRA = ("What did Maria ask me to do on Friday?", ["maria-oct"], ["Rosa", "train", "station"])


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    rest: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, rest = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--skill", help="slash skill to prefix, e.g. sound or context")
    ap.add_argument("--bakeoff", type=Path, default=Path("eval/bakeoff.py"),
                    help="path to bakeoff.py (default: eval/bakeoff.py)")
    ap.add_argument("--log", type=Path, help="Odysseus log, for approvals and tools_sent")
    ap.add_argument("--extra", action="store_true", help="add the person + date question (22)")
    args = ap.parse_args(argv)

    sys.path.insert(0, str(args.bakeoff.resolve().parent))
    import bakeoff  # Athena's eval script, unmodified

    if args.extra:
        bakeoff.QUESTIONS.append(EXTRA)
    plain = bakeoff.run_odysseus

    def run(question: str):
        start = args.log.stat().st_size if args.log else 0
        message = question
        if args.skill:
            r = bakeoff._odysseus_client().post(f"/api/skills/{args.skill}/invoke",
                                                json={"request": question})
            r.raise_for_status()
            message = r.json()["message"]
        answer, tools = plain(message)
        if args.log:
            with args.log.open(errors="replace") as f:
                f.seek(start)
                seg = f.read()
            sent = re.findall(r"tools_sent=(\d+)", seg)
            print(f"  approvals={seg.count('Exact approval required')} "
                  f"tools_sent={','.join(sent[:6])} pinned={'[skill-pin]' in seg}", flush=True)
        return answer, tools

    bakeoff.run_odysseus = run
    sys.argv = [str(args.bakeoff), "--frontend", "odysseus", *rest]
    return bakeoff.main()


if __name__ == "__main__":
    sys.exit(main())
