#!/usr/bin/env python3
"""Fork: set up Home Assistant for Odysseus's voice endpoint without a browser.

The same run as Settings → Integrations → Voice assistant → Set up Home Assistant
(fork/voice/ha_setup.py), for when Odysseus can't open Home Assistant's login page. Run it
inside the Odysseus checkout or container (it writes Odysseus's database):

    HA_TOKEN=<HA long-lived token of an administrator> \\
    python scripts/ha_voice_setup.py --ha-url http://homeassistant.local:8123 \\
        --odysseus-url https://odysseus.<tailnet>.ts.net --owner <odysseus admin>

The HA token is read from the environment (HA_TOKEN); --ha-token works but leaves it in
the shell history. It is only used, never revoked or stored: it is yours (delete it
under your HA profile → Security when done).

A running Odysseus learns about API tokens made by another process only when its token
list changes or it restarts. So the voice token made here is given to Home Assistant
without HA checking it (``skip_authentication``), and the report says to restart
Odysseus. To avoid that, make the token in Settings (Voice assistant → Make a voice
token) and pass it as VOICE_TOKEN: then nothing is minted and HA checks it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ha-url", required=True, help="Home Assistant address")
    parser.add_argument("--ha-token", default="",
                        help="HA long-lived token (prefer the HA_TOKEN environment variable)")
    parser.add_argument("--odysseus-url", required=True,
                        help="Odysseus as Home Assistant reaches it")
    parser.add_argument("--owner", required=True, help="the Odysseus admin who owns the token")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    return parser.parse_args(argv)


def known_owner(owner: str) -> bool:
    from core.auth import AuthManager

    return owner in AuthManager().users


async def run(args: argparse.Namespace) -> dict:
    from fork.voice import ha_setup
    from fork.voice.ha_settings import set_ha_url, set_odysseus_url

    ha_token = os.environ.get("HA_TOKEN") or args.ha_token
    if not ha_token:
        raise SystemExit("Give the Home Assistant token in HA_TOKEN (or --ha-token).")
    voice_token = os.environ.get("VOICE_TOKEN", "").strip()
    if voice_token:
        report = await ha_setup.setup_voice(args.ha_url, ha_token, args.odysseus_url,
                                            voice_token)
        report["token_name"] = "(the VOICE_TOKEN you gave)"
    else:
        report = await ha_setup.run_setup(args.owner, args.ha_url, ha_token,
                                          args.odysseus_url, validate=False)
        if report.get("token_name"):
            report["note"] = ("Restart Odysseus so it accepts the new voice token "
                              f"\"{report['token_name']}\".")
    if report.get("ok"):  # a failed setup's address isn't handed to Echos
        set_ha_url(args.ha_url)
        set_odysseus_url(args.odysseus_url)
    return report


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        from fork.voice.ha_setup import check_ha_url

        from fork.voice.announce import Refused, check_url

        check_ha_url(args.ha_url)
        asyncio.run(check_url(args.ha_url))  # only Home Assistant on the owner's network
    except (ValueError, Refused) as exc:
        print(exc, file=sys.stderr)
        return 2
    if not os.environ.get("VOICE_TOKEN") and not known_owner(args.owner):
        print(f"No Odysseus user {args.owner!r}.", file=sys.stderr)
        return 2
    report = asyncio.run(run(args))
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for step in report["steps"]:
            detail = f" — {step['detail']}" if step["detail"] else ""
            print(f"{step['status']:>8}  {step['step']}{detail}")
        if report.get("token_name"):
            print(f"\nVoice token: {report['token_name']}")
        if report.get("note"):
            print(report["note"])
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
