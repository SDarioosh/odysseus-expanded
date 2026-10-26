#!/usr/bin/env python3
"""Fork: configure an Odysseus instance to use its modules over MCP.

Works through the admin API, so it runs against any instance and leaves upstream
code alone. By default it keeps every Odysseus feature:

* Leaves the tool toggles alone (Settings > Tools stays as the admin set it).
* Sets the default chat model to "Auto" tool mode: each turn is offered the
  tools that per-turn retrieval picks, module MCP tools included.
* Reports each configured MCP server and whether it is connected, and warns when
  a module server named with ``--module`` is missing or disconnected.

``--trim`` restores the old memory-only setup: every built-in tool except a small
keep-list is turned off, the built-in email/browser/image MCP tools are hidden,
and the model is set to "Regular tools" (``full``), which offers every enabled
tool on every turn. Fewer tools make a small local model faster and keep it from
picking a built-in (tasks, calendar, chat search) over a module tool.
``--untrim`` turns every tool back on (and sets "Auto").

Don't combine "Regular tools" with every tool on: all ~230 tool schemas go into
each request (~35k tokens), more than a 32k context holds.

Action items: resumes the built-in "Module Action Items" task (src/module_action_items.py,
ships paused), which files voice-note action items into the calendar and to-dos and
pushes them to the phone; ``--no-action-items`` leaves it alone. (It logs a warning at
run time when the module's CONVO_TIMEZONE isn't Odysseus' timezone.)

Slash-command skills: ``/sound <question>`` offers Athena's tools for that
one message, and ``/context <question>`` the tools of every trusted module. This
script creates or updates those skills through the skills API (source
``managed``, so they need no skill audit; see src/skill_invocation.py):

* one per ``--module-skill NAME=SERVER`` (default ``sound=convo``), requiring
  ``mcp:SERVER``; its procedure is the module's usage instructions, read from
  ``--skill-text NAME=FILE`` or else from the Custom preset's system prompt (the
  module doc says to paste the same text there);
* ``--context-skill`` (default ``context``), requiring ``mcp:<server>`` for each
  server in ``--trusted`` (default: ``ODYSSEUS_TRUSTED_READONLY_MCP``, else the
  ``--module`` list). ``--no-skills`` skips both.

Trusting a module's read-only tools is not done here; that is the
``ODYSSEUS_TRUSTED_READONLY_MCP`` environment variable (see src/mcp_trust.py).

Usage:
    python scripts/modules_setup.py --url http://127.0.0.1:7000 \
        --user admin --password-file ~/.config/odysseus-fork/admin.txt \
        [--trim [--keep a,b] | --untrim] [--model-mode auto|full|unchanged]
        [--module-skill sound=convo] [--skill-text sound=FILE] [--context-skill context]
        [--trusted convo,...] [--no-skills] [--dry-run]

Re-run after each upgrade. With --trim, new upstream tools are turned off unless
added to KEEP or --keep.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import httpx

# Built-ins kept by --trim. Everything else is turned off; add more with --keep.
# - manage_tasks / manage_calendar / manage_notes: off until module action items
#   sync into them. While they're empty, the model asks them "when is my next
#   appointment?" instead of the conversation store.
KEEP = {
    "ask_user",
    "update_plan",
}
# Built-in MCP servers whose tools --trim hides from the model.
BUILTIN_MCP_OFF = {"email", "builtin_browser", "image_gen"}
MODEL_MODES = {"full": "full", "auto": "", "unchanged": None}
TRUSTED_ENV = "ODYSSEUS_TRUSTED_READONLY_MCP"
MANAGED_SOURCE = "managed"  # src/skill_invocation.py: skips the skill audit gates
# src/module_action_items.py's built-in task (ships paused): files voice-note items into
# the calendar and to-dos and pushes them. Named here, not imported: this script only
# talks to the admin API.
ACTION_ITEMS_ACTION = "barvis_sync_action_items"
ACTION_ITEMS_TASK = "Module Action Items"
CONTEXT_PROCEDURE = [
    ("Answer from the user's own data in the connected modules (recorded conversations "
     "and any other module listed in requires_toolsets), not from general knowledge."),
    ("Resolve relative dates (\"last Wednesday\", \"yesterday\") against the current date "
     "in context and pass them as YYYY-MM-DD dates to the module tools."),
    ("Search every module whose tools you were given: call each module's search tool "
     "with the people, topic and dates from the request. Open the best hits "
     "(get_conversation / get_transcript or the module's equivalent) when the search "
     "result is not enough."),
    ("Answer briefly. Put the source link from the tool result next to each fact you use, "
     "exactly as the tool returned it. Never invent a link."),
    "If no module has an answer, say so plainly; do not guess.",
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://127.0.0.1:7000")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password-file", required=True, type=Path)
    tools = ap.add_mutually_exclusive_group()
    tools.add_argument("--trim", action="store_true",
                       help="turn off every built-in tool except a small keep-list")
    tools.add_argument("--untrim", action="store_true",
                       help="turn every tool back on (undoes --trim)")
    ap.add_argument("--keep", default="", help="with --trim: extra comma-separated tools to keep")
    ap.add_argument("--model-mode", choices=sorted(MODEL_MODES), default=None,
                    help="tool mode for the default chat model "
                         "(default: full = Regular tools with --trim, else auto)")
    ap.add_argument("--no-model-modes", action="store_true",
                    help="same as --model-mode unchanged (kept for old scripts)")
    ap.add_argument("--module", action="append", default=None,
                    help="MCP server name a module registers (default: convo); repeatable")
    ap.add_argument("--module-skill", action="append", default=None, metavar="NAME=SERVER",
                    help="slash skill offering one module's tools (default: sound=convo); "
                         "repeatable")
    ap.add_argument("--skill-text", action="append", default=[], metavar="NAME=FILE",
                    help="procedure text for a module skill (default: the Custom preset's "
                         "system prompt)")
    ap.add_argument("--context-skill", default="context",
                    help="slash skill offering every trusted module's tools (default: context)")
    ap.add_argument("--trusted", default=None,
                    help=f"comma-separated servers for the context skill (default: ${TRUSTED_ENV}, "
                         "else the --module list)")
    ap.add_argument("--no-skills", action="store_true", help="don't create or update skills")
    ap.add_argument("--no-action-items", action="store_true",
                    help=f"leave the {ACTION_ITEMS_TASK!r} scheduled task as it is "
                         "(default: resume it)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if args.no_model_modes:
        args.model_mode = "unchanged"
    elif args.model_mode is None:
        args.model_mode = "full" if args.trim else "auto"
    if args.keep and not args.trim:
        ap.error("--keep only applies with --trim")
    if args.module is None:
        args.module = ["convo"]
    if args.module_skill is None:
        args.module_skill = ["sound=convo"]
    try:
        args.module_skill = [_pair(p, "--module-skill") for p in args.module_skill]
        args.skill_text = dict(_pair(p, "--skill-text") for p in args.skill_text)
    except ValueError as exc:
        ap.error(str(exc))
    if args.trusted is None:
        args.trusted = os.environ.get(TRUSTED_ENV, "") or ",".join(args.module)
    args.trusted = [t.strip() for t in args.trusted.split(",") if t.strip()]
    return args


def _pair(text: str, flag: str) -> tuple[str, str]:
    name, sep, value = text.partition("=")
    if not sep or not name.strip() or not value.strip():
        raise ValueError(f"{flag} expects NAME=VALUE, got {text!r}")
    return name.strip(), value.strip()


def procedure_steps(text: str) -> list[str]:
    """Split instructions into procedure steps: one per paragraph or list item."""
    steps = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if lines and all(re.match(r"^([-*]|\d+[.)])\s+", ln) for ln in lines):
            steps += [re.sub(r"^([-*]|\d+[.)])\s+", "", ln) for ln in lines]
        elif lines:
            steps.append(" ".join(lines))
    return steps


def skill_specs(c: httpx.Client, args: argparse.Namespace) -> list[dict]:
    """The skills to create or update, as skills-API bodies."""
    specs = []
    preset_text = None
    for name, server in args.module_skill:
        if name in args.skill_text:
            text = Path(args.skill_text[name]).expanduser().read_text()
        else:
            if preset_text is None:
                presets = c.get("/api/presets").json()
                preset_text = str((presets.get("custom") or {}).get("system_prompt") or "")
            text = preset_text
        steps = procedure_steps(text)
        if not steps:
            print(f"warning: no instructions for skill {name!r} (empty Custom preset; "
                  f"use --skill-text {name}=FILE)", file=sys.stderr)
            steps = [(f"Use the {server} module's tools to answer from the user's data, "
                      "and cite the link each result gives next to each fact.")]
        specs.append({
            "name": name,
            "description": f"Use the {server} module's tools for this message",
            "category": "modules",
            # casefolded: with_mcp_toolsets offers "mcp:<casefolded name>" for every server
            "requires_toolsets": [f"mcp:{server.casefold()}"],
            "procedure": steps,
        })
    if args.context_skill and args.trusted:
        specs.append({
            "name": args.context_skill,
            "description": "Search every connected module for this message: "
                           + ", ".join(args.trusted),
            "category": "modules",
            "requires_toolsets": [f"mcp:{s.casefold()}" for s in args.trusted],
            "procedure": list(CONTEXT_PROCEDURE),
        })
    return specs


def apply_skills(c: httpx.Client, specs: list[dict], dry_run: bool) -> list[str]:
    """Create or update managed skills; never overwrite a skill someone else made."""
    existing = {s.get("name"): s for s in c.get("/api/skills").json().get("skills", [])}
    warnings = []
    for spec in specs:
        name = spec["name"]
        current = existing.get(name)
        if current is not None and current.get("source") != MANAGED_SOURCE:
            warnings.append(f"skill {name!r} exists and isn't managed by this script; "
                            f"left alone (rename or delete it, then rerun)")
            continue
        action = "update" if current is not None else "create"
        print(f"skill /{name}: {action}, requires {spec['requires_toolsets']}, "
              f"{len(spec['procedure'])} procedure steps")
        if dry_run:
            continue
        if current is None:
            c.post("/api/skills/add", json={**spec, "status": "published",
                                            "source": MANAGED_SOURCE,
                                            "confidence": 1.0}).raise_for_status()
        else:
            c.put(f"/api/skills/{name}", json={**spec, "status": "published"}).raise_for_status()
    return warnings


def trimmed_tools(c: httpx.Client, keep: set[str]) -> tuple[list[str], list[str]]:
    """Return (all built-in tool ids, the list --trim turns off)."""
    tools = [t["id"] for t in c.get("/api/tools").json()["tools"]]
    disabled = sorted(t for t in tools if t not in keep)
    # The built-in MCP servers are offered under qualified mcp__<server>__* names,
    # which the bare names above don't cover.
    mcp = c.get("/api/mcp/tools").json()
    mcp = mcp.get("tools", mcp) if isinstance(mcp, dict) else mcp
    disabled += sorted(t["qualified_name"] for t in mcp
                       if t.get("server_id") in BUILTIN_MCP_OFF)
    return tools, disabled


def report_modules(c: httpx.Client, modules: list[str]) -> list[str]:
    """Print every MCP server's status; return warnings for missing modules."""
    servers = c.get("/api/mcp/servers").json()
    for s in servers:
        state = s.get("status", "?") if s.get("is_enabled", True) else "disabled"
        err = f" ({s['error']})" if s.get("error") else ""
        print(f"mcp server {s.get('name')!r}: {state}, "
              f"{s.get('enabled_tool_count', 0)} tools{err}")
    warnings = []
    by_name = {str(s.get("name", "")).casefold(): s for s in servers}
    for name in modules:
        s = by_name.get(name.casefold())
        if s is None:
            warnings.append(f"module MCP server {name!r} is not registered "
                            f"(Settings > MCP; see docs/fork/modules.md)")
        elif s.get("status") != "connected" or not s.get("is_enabled", True):
            warnings.append(f"module MCP server {name!r} is not connected")
    return warnings


def enable_action_items(c: httpx.Client, dry_run: bool) -> list[str]:
    """Resume the module action-item task; return warnings.

    Odysseus re-pauses ship-paused built-ins until the Tasks page has been opened once,
    so this marks it opened (without resuming every other built-in, as "enable" would)."""
    tasks = c.get("/api/tasks").json()
    tasks = tasks.get("tasks", []) if isinstance(tasks, dict) else tasks
    task = next((t for t in tasks or [] if t.get("action") == ACTION_ITEMS_ACTION), None)
    if task is None:
        return [f"scheduled task {ACTION_ITEMS_TASK!r} not found (is this our Odysseus fork?)"]
    if task.get("status") == "active":
        print(f"task {ACTION_ITEMS_TASK!r}: active")
        return []
    print(f"task {ACTION_ITEMS_TASK!r}: {task.get('status')} -> active")
    if dry_run:
        return []
    onboarding = c.get("/api/tasks/onboarding").json()
    if not (isinstance(onboarding, dict) and onboarding.get("opened")):
        c.post("/api/tasks/onboarding", json={"enabled": False}).raise_for_status()
    c.post(f"/api/tasks/{task['id']}/resume").raise_for_status()
    return []


def run(c: httpx.Client, args: argparse.Namespace) -> int:
    pw = args.password_file.expanduser().read_text().strip()
    c.post("/api/auth/login", json={"username": args.user, "password": pw,
                                    "remember": False}).raise_for_status()
    plan: dict = {}

    if args.trim:
        keep = KEEP | {t.strip() for t in args.keep.split(",") if t.strip()}
        tools, disabled = trimmed_tools(c, keep)
        print(f"--trim: {len(tools)} built-in tools; keeping {sorted(set(tools) & keep)}; "
              f"turning off {len(disabled)}")
        plan["disabled_tools"] = disabled
    elif args.untrim:
        print("--untrim: turning every tool back on")
        plan["disabled_tools"] = []
    else:
        print("tools: left as they are (use --trim or --untrim to change them)")
    if "disabled_tools" in plan and not args.dry_run:
        c.post("/api/tools", json={"disabled": plan["disabled_tools"]}).raise_for_status()

    mode = MODEL_MODES[args.model_mode]
    if mode is not None:
        dc = c.get("/api/default-chat").json()
        endpoint, model = dc.get("endpoint_id"), dc.get("model")
        if endpoint and model:
            label = "full (Regular tools)" if mode else "Auto"
            print(f"model tool mode: {model} -> {label}")
            plan["model_tool_mode"] = {model: mode}
            if not args.dry_run:
                c.patch(f"/api/model-endpoints/{endpoint}/models",
                        json={"model_tool_modes": {model: mode}}).raise_for_status()
        else:
            print("no default chat model; skipping tool mode", file=sys.stderr)

    warnings = report_modules(c, args.module)
    if not args.no_action_items:
        plan["action_items_task"] = "active"
        warnings += enable_action_items(c, args.dry_run)
    if not args.no_skills:
        specs = skill_specs(c, args)
        plan["skills"] = [{"name": s["name"], "requires_toolsets": s["requires_toolsets"]}
                          for s in specs]
        warnings += apply_skills(c, specs, args.dry_run)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)

    if args.dry_run:
        print(json.dumps({"would_apply": plan}, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    with httpx.Client(base_url=args.url, timeout=60) as c:
        try:
            return run(c, args)
        except httpx.ConnectError as exc:
            print(f"cannot reach {args.url}: {exc}. Is Odysseus up? "
                  f"Check: curl {args.url}/api/health", file=sys.stderr)
            return 2


if __name__ == "__main__":
    sys.exit(main())
