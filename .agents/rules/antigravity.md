---
trigger: always_on
description: Antigravity-specific notes for Odysseus. Shared rules are in AGENTS.md.
---

# Antigravity notes

- `AGENTS.md` at the workspace root is the source of truth and is loaded automatically.
  Add shared rules there, not here.
- Terminal commands and browser actions never target the owner's production Odysseus
  (Docker, `/opt/odysseus`, formerly `~/models/odysseus`, port 7000); only the owner deploys it, with
  `scripts/fork_deploy.sh`.
- When reviewing a PR, check that changes affecting answers, devices or deployment have a
  Server verification section (`docs/fork/sandbox.md`).
