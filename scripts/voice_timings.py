#!/usr/bin/env python3
"""Fork: how long each step of a voice turn took (docs/fork/voice-assistant.md#timings).

Reads ``<DATA_DIR>/fork_voice_timings.db`` (``fork/voice/timings.py``) and prints, for the
last ``--days`` days, every step's median, 90th percentile and slowest time, then each
tool's. ``--last N`` also lists the newest N turns one by one; ``--csv FILE`` writes every
turn's steps to a spreadsheet. Read-only: it never changes the file.

    python scripts/voice_timings.py                  # last 7 days
    python scripts/voice_timings.py --days 1 --last 10
    python scripts/voice_timings.py --db /path/to/fork_voice_timings.db
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fork.voice import timings


def _s(value: float | None) -> str:
    return "-" if value is None else f"{value:6.2f}s"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--last", type=int, default=0, help="also list the newest N turns")
    ap.add_argument("--csv", help="write every turn's steps to this CSV file")
    ap.add_argument("--db", help="the timings file (default: the one in DATA_DIR)")
    args = ap.parse_args(argv)
    path = Path(args.db) if args.db else timings.db_path()
    if not path.exists():
        print(f"No timings yet ({path}). Ask the Echo a question first.")
        return 0
    found = timings.rows(since=time.time() - args.days * 86400, path=path)
    with_device = sum(1 for r in found if r["device"])
    with_hub = sum(1 for r in found if r["hub"])
    print(f"{len(found)} turns in the last {args.days:g} days "
          f"({with_hub} reached Odysseus, {with_device} with the device's times)\n")
    steps = timings.summary(found)
    if steps:
        width = max(len(s["step"]) for s in steps)
        print(f"{'Step':<{width}}  turns   median      p90  slowest")
        for s in steps:
            print(f"{s['step']:<{width}}  {s['turns']:>5}  {_s(s['median'])}  {_s(s['p90'])}"
                  f"  {_s(s['max'])}")
    tools = timings.tool_summary(found)
    if tools:
        width = max(len(t["tool"]) for t in tools)
        print(f"\n{'Tool':<{width}}  calls   median  slowest")
        for t in tools:
            print(f"{t['tool']:<{width}}  {t['calls']:>5}  {_s(t['median'])}  {_s(t['max'])}")
    labels = [s[0] for s in timings.STEPS] + ["Odysseus: tool calls (all)"]
    if args.last:
        print(f"\nNewest {args.last} turns:")
        for row in found[-args.last:]:
            when = datetime.fromtimestamp(row["at"]).strftime("%Y-%m-%d %H:%M:%S")
            hub = row["hub"] or {}
            st = timings.steps_of(row)
            parts = [f"{k}: {v:.2f}s" for k, v in st.items()]
            tools_used = ", ".join(t["name"] for t in hub.get("tools") or []) or "none"
            print(f"- {when} {row['device_id'] or '?'} ({hub.get('outcome', 'not in Odysseus')};"
                  f" tools: {tools_used})")
            for p in parts:
                print(f"    {p}")
    if args.csv:
        with open(args.csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["time", "device", "outcome", "tools", *labels])
            for row in found:
                hub = row["hub"] or {}
                st = timings.steps_of(row)
                w.writerow([datetime.fromtimestamp(row["at"]).isoformat(timespec="seconds"),
                            row["device_id"] or "", hub.get("outcome", ""),
                            " ".join(t["name"] for t in hub.get("tools") or []),
                            *[f"{st[k]:.3f}" if k in st else "" for k in labels]])
        print(f"\nWrote {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
