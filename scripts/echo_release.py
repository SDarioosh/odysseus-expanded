#!/usr/bin/env python3
"""Fork: publish a build of the Echo app for the Echos to update to (docs/fork/voice-assistant.md#app-updates).

Copies the APK to ``<data dir>/echo_app/odysseus-echo.apk`` (a temp file, then renamed),
works out its size and sha256, and writes ``release.json`` beside it
(``fork/voice/app_release.py``). The Echos read it from ``GET …/voice/v1/app/latest`` and
offer the update when its ``--version-code`` is higher than theirs. Refuses a version code
lower than the published one unless ``--force``. It doesn't read the APK's manifest: pass
the version the Echo build printed (``./gradlew printVersionName printVersionCode``).

    python scripts/echo_release.py odysseus-echo-release.apk \\
        --version-name 0.14.0+odysseus.30 --version-code 1400030 \\
        --notes "Kiosk settings; Today page." --data-dir /srv/odysseus/data
    python scripts/echo_release.py --show --data-dir /srv/odysseus/data

Without ``--data-dir`` it uses the hub's data folder (``ODYSSEUS_DATA_DIR``, else the
default ``data/``). Run it as the user that owns the data folder, so the hub can read the
files. No restart is needed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fork.voice import app_release


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("apk", nargs="?", help="the APK to publish")
    ap.add_argument("--version-name", help="e.g. 0.14.0+odysseus.30")
    ap.add_argument("--version-code", type=int, help="e.g. 1400030 (what devices compare)")
    ap.add_argument("--notes", default="", help="one line on what changed")
    ap.add_argument("--force", action="store_true",
                    help="publish even if the version code is lower than the published one")
    ap.add_argument("--data-dir", help="the hub's data folder (default: DATA_DIR)")
    ap.add_argument("--show", action="store_true", help="print the published release and exit")
    args = ap.parse_args(argv)

    if args.show:
        release = app_release.current(args.data_dir)
        if release is None:
            print(f"No release published in {app_release.release_dir(args.data_dir)}.")
            return 1
        print(json.dumps(release, indent=2))
        return 0
    if not args.apk or not args.version_name or args.version_code is None:
        ap.error("the APK, --version-name and --version-code are required")
    before = app_release.current(args.data_dir)
    try:
        release = app_release.publish(
            args.apk, version_name=args.version_name, version_code=args.version_code,
            notes=args.notes, force=args.force, data_dir=args.data_dir)
    except app_release.ReleaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if before:
        print(f"was:       {before['version_name']} ({before['version_code']})")
        if before["version_code"] == release["version_code"]:
            print("warning: same version code as before: Echos that have it won't update.")
    print(f"published: {release['version_name']} ({release['version_code']}), "
          f"{release['size']} bytes, sha256 {release['sha256']}")
    print(f"in:        {app_release.release_dir(args.data_dir)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
