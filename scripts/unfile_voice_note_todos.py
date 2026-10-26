#!/usr/bin/env python3
"""Fork: remove the to-dos filed one per voice-note action item (a one-off clean-up).

Before ``MODULE_FILE_ITEMS=explicit`` and the "Voice notes" list, the Module Action Items
task filed every voice-note item as a to-do of its own, titled with the whole sentence,
also items that were no task ("Stop prioritizing side projects ..."). This lists those
to-dos: label ``odysseus-sound``, one checklist line, no due date and no reminder, filed
from a voice note whose item isn't explicit now, and still in Odysseus, each with why it
qualifies. To-dos filed in ``explicit`` mode or from the page are left out unless
``--all``; dated to-dos and reminders never qualify. With ``--apply`` it deletes them
and dismisses their items in Athena (``update_action_item``; items already done
stay done). They are never filed again.

It runs in the hub, which holds the module's MCP connection: the script calls the admin
route ``POST /api/modules/sound/unfile-todos`` (src/module_action_items.py,
``unfile_auto_todos``). To remove only some, run it once without ``--apply``, then pass
their item ids with ``--only``. A single one can also be undone on ``/modules/sound``
(Undo on the item).

Usage:
    python scripts/unfile_voice_note_todos.py --url http://127.0.0.1:7010 \\
        --user admin --password-file ~/.config/odysseus-fork/admin.txt [--dry-run]
    ... --apply [--only 12,14] [--all]

Without ``--password-file`` no login is made (an instance with auth off).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

ROUTE = "/api/modules/sound/unfile-todos"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://127.0.0.1:7000")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password-file", type=Path, default=None)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="only list them (the default)")
    mode.add_argument("--apply", action="store_true",
                      help="delete the to-dos and dismiss their items")
    ap.add_argument("--only", default="", help="comma-separated item ids to limit it to")
    ap.add_argument("--all", action="store_true",
                    help="also to-dos filed in explicit mode or from the page")
    args = ap.parse_args(argv)
    try:
        args.only = [int(i) for i in args.only.split(",") if i.strip()] or None
    except ValueError:
        ap.error("--only expects item ids, e.g. 12,14")
    return args


def report(result: dict) -> str:
    todos = result.get("todos") or []
    if not todos:
        return "No to-dos filed one per voice-note item are left."
    lines = [f"- #{t['id']} [{t['status']}] {t['title']}"
             + (f" ({'; '.join(t['why'])})" if t.get("why") else "") for t in todos]
    if result.get("apply"):
        head = f"Removed {result.get('removed', 0)} of {len(todos)} to-dos:"
    else:
        head = f"{len(todos)} to-dos would be removed (run again with --apply):"
    return "\n".join([head, *lines])


def run(c: httpx.Client, args: argparse.Namespace) -> int:
    if args.password_file is not None:
        pw = args.password_file.expanduser().read_text().strip()
        c.post("/api/auth/login", json={"username": args.user, "password": pw,
                                        "remember": False}).raise_for_status()
    body: dict = {"apply": bool(args.apply)}
    if args.only:
        body["ids"] = args.only
    if args.all:
        body["all"] = True
    r = c.post(ROUTE, json=body)
    if r.status_code >= 400:
        detail = r.json().get("detail") if "json" in r.headers.get("content-type", "") else r.text
        print(f"{ROUTE} answered {r.status_code}: {detail}", file=sys.stderr)
        return 1
    print(report(r.json()))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    with httpx.Client(base_url=args.url, timeout=120) as c:
        try:
            return run(c, args)
        except httpx.ConnectError as exc:
            print(f"cannot reach {args.url}: {exc}. Is Odysseus up?", file=sys.stderr)
            return 2


if __name__ == "__main__":
    sys.exit(main())
