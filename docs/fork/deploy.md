# Deploying the fork on the server

Our Odysseus fork runs on the owner's server in two places:

| | Production | Test instance |
|---|---|---|
| What | the owner's daily install: all Odysseus features plus modules | throwaway copy for checks and evals |
| How | Docker Compose, image `odysseus-fork:<commit>` built locally | from source (`uvicorn`), its own data dir |
| Where | `~/models/odysseus`, port 7000 | `~/projects/barvis`, `127.0.0.1:7010` (`~/.hermes/cache/scratch/barvis-run.sh`) |
| Changed by | the owner only, with `scripts/fork_deploy.sh` | verification steps in PRs |

Production ran upstream Odysseus until 2026-10-18 and was converted in place, so it kept
its accounts, chats, settings, ChromaDB and ntfy data.

The flow for every change: **PR → sandbox checks → merge into `barvis` → test instance
(Server verification) → `fork_deploy.sh deploy` on production.**

## Update production

One command updates the modules and Odysseus, from any directory:

```bash
odysseus-deploy deploy          # modules, then the latest barvis branch
odysseus-deploy status          # what is running, modules included
odysseus-deploy modules         # only update and restart the modules
odysseus-deploy rollback        # Odysseus back to the image before the last deploy
```

`odysseus-deploy` is a link to `scripts/fork_deploy.sh` in the production checkout
([one-time setup](#one-command-setup-once)); the full path works too.

`deploy` does, in order:

1. Refuses to run if tracked files have local changes, or if
   `docker-compose.override.yml` doesn't select an `odysseus-fork:` image (or the former
   `barvis:` one).
2. Runs each module's deploy command from `FORK_MODULES`
   ([modules.md](modules.md#deploying-a-module)). For Athena that updates its
   checkout, installs it, restarts `convo-server serve`, the MCP server and (if
   configured) whisper-server as background services, and waits until they answer. A
   failed module rolls itself back; Odysseus is still deployed and the run ends with an
   error naming the module.
3. Fetches the fork's remote and fast-forwards the local `barvis` branch (or checks out
   the ref you pass, e.g. a tag).
4. Builds `odysseus-fork:<short sha>`, tags the running image `odysseus-fork:previous`.
5. Stops the app container and backs up the data dir to
   `~/odysseus-backups/data-<date>-<old sha>.tgz` (keeps the last 5).
6. Starts the new image, waits up to 2 minutes for `/api/health`. If it never comes up,
   it switches back to `odysseus-fork:previous` by itself and says so.
7. Runs `scripts/modules_setup.py` if `~/.config/odysseus-fork/admin.txt` exists.

Run it as your user, not with `sudo`: it calls `sudo docker` itself, and the modules run
as your user.

Athena's own `server/scripts/deploy.sh deploy` does the same in the other order:
it updates the module, then runs this script with no modules (it finds this checkout from the
running Odysseus container, or from `BARVIS_SOUND_ODYSSEUS_DIR` in its `server/.env`). Either
command updates both. Started from here, the module's script skips that step
(`FORK_DEPLOY_RUNNING=1`).

Settings (all optional), as `KEY=value` lines in `~/.config/odysseus-fork/deploy.env` or in the
environment (which wins): `FORK_MODULES`, `DOCKER` (default `sudo docker`),
`FORK_REMOTE`, `FORK_BACKUP_DIR`, `FORK_KEEP_BACKUPS`, `FORK_PASSWORD_FILE`,
`FORK_PY`, `FORK_SETUP_ARGS`. See the top of the script. The former `BARVIS_*`
names, `~/.config/barvis/` and `~/barvis-backups` still work ([below](#moving-from-the-barvis-name-once)).

### One-command setup (once)

On the owner's server, with Athena in `~/projects/barvis-ai-wt/main` (adjust the
path if the folder was renamed):

```bash
mkdir -p ~/.local/bin ~/.config/odysseus-fork
ln -sf ~/models/odysseus/scripts/fork_deploy.sh ~/.local/bin/odysseus-deploy
cat > ~/.config/odysseus-fork/deploy.env <<'EOF'
FORK_MODULES=~/projects/barvis-ai-wt/main/server/scripts/deploy.sh
FORK_PY=~/projects/barvis/venv/bin/python
EOF
```

`modules_setup.py` needs Python with `httpx`, hence `FORK_PY`. In Athena's
`server/.env`, set what used to be typed on the command line, so the services get it:

```bash
CONVO_MCP_TOKEN=<the token in Odysseus' convo MCP URL>
CONVO_MCP_HOST=172.17.0.1
# optional, whisper-server as a service too (full paths):
BARVIS_SOUND_WHISPER_CMD=/path/to/whisper-server -m /path/to/ggml-large-v3-turbo-q5_0.bin --host 127.0.0.1 --port 8178
```

Stop any `convo-server` or `whisper-server` started by hand (the module's deploy refuses
to start next to them), then run `odysseus-deploy deploy`. After that the module services
start at boot and restart on a crash. Athena's side:
[server/README.md](https://github.com/SDarioosh/barvis-sound/blob/main/server/README.md#deploy-one-command-runs-in-the-background).

### Moving from the Barvis name (once)

The fork was called Barvis until 2026-10-19. The first deploy after the rename works
with the old setup: the old `barvis-deploy` link still runs the old script, and the old
override keeps the `barvis:` images. Its last step can't find `barvis_setup.py` any more
and says "barvis_setup.py failed"; that is expected, the next deploy runs
`modules_setup.py`. Then switch to the new names:

```bash
cd ~/models/odysseus
cp docker/fork.override.yml docker-compose.override.yml   # image: odysseus-fork:...
rm -f ~/.local/bin/barvis-deploy
ln -sf ~/models/odysseus/scripts/fork_deploy.sh ~/.local/bin/odysseus-deploy
odysseus-deploy deploy        # keeps barvis:current as the rollback image
```

Optional, any time later: `mv ~/.config/barvis ~/.config/odysseus-fork`, rename the
`BARVIS_*` lines in `deploy.env` to `FORK_*`, and `mv ~/barvis-backups ~/odysseus-backups`.
Until then the script reads the old names. Old `barvis:<sha>` images can be removed with
`sudo docker image rm` once a rollback to them is no longer wanted.

**Rolling back data.** `rollback` switches the image only. If the newer version migrated
the database, the old one may not read it; restore the backup taken before that deploy
(the command `rollback` prints):

```bash
sudo docker compose stop odysseus
sudo rm -rf data && sudo tar xzf ~/odysseus-backups/data-<date>-<sha>.tgz -C .
sudo docker compose up -d
```

## Features and modules

Production keeps every Odysseus feature. `scripts/modules_setup.py` (run by `deploy`):

- leaves the tool toggles in Settings > Tools alone;
- sets the default chat model to "Auto" tool mode;
- lists the MCP servers and warns if `convo` (or each `--module`) is missing or
  disconnected;
- creates or updates the slash-command skills `/sound` and `/context` (below).

`--trim` turns off every built-in except a small keep-list and sets "Regular tools"
(the old memory-only setup); `--untrim` turns everything back on.
`--model-mode auto|full|unchanged` overrides the tool mode. Put the flags in
`FORK_SETUP_ARGS` to make `deploy` use them.

**Asking a module: `/sound` and `/context`.** With every Odysseus tool on and "Auto",
a plain question doesn't reach module tools: per-turn selection picks built-ins
(calendar, files) instead. Start the message with a slash command to ask the modules:

```
/sound what did Alice say about the budget?
/context what did I tell Emily I'd do last Wednesday?
```

- `/sound` offers Athena's tools (`mcp:convo`); `/context` offers the tools of
  every server in `ODYSSEUS_TRUSTED_READONLY_MCP`. That message gets only those tools
  plus `ask_user`/`update_plan` (~10 instead of ~130), so a small model finds them.
- It applies to that one message. The next message without a slash command is routed
  normally, but once a module tool has run in a chat, later messages in that chat are
  offered all of that module's tools again (for example "Speaker 2 was Kath" after a
  `/sound` question gets `name_speaker`). A new chat needs the command again.
- Read-only module tools run without approval (trusted server); write tools
  (`update_action_item`, `name_speaker`) still ask.
- The model gets today's date on every turn (Odysseus' "Current date and time"
  context), so "last Wednesday" becomes a date.
- Both are skills (Settings > Skills, category `modules`) with `source: managed`:
  `modules_setup.py` creates and updates them, and they need no skill audit. `/sound`'s
  procedure is the Custom preset's text (Athena's `INSTRUCTIONS`): edit the
  preset and rerun setup; don't edit the skill by hand. `--no-skills` skips them;
  `--module-skill NAME=SERVER`, `--skill-text NAME=FILE`, `--context-skill NAME` and
  `--trusted a,b` change them.
- `modules_setup.py` runs outside the container, so it can't see the container's
  `ODYSSEUS_TRUSTED_READONLY_MCP`. Without `--trusted` or that variable in its own
  environment, `/context` covers the `--module` list (default `convo`).
- If a module's server is down, the slash message answers that its tools are
  unavailable (`unavailable: mcp:convo`) instead of guessing.
- Works in "Auto". In "Regular tools" every enabled tool is sent anyway, so with all
  tools on it still overflows a 32k context; use `--trim` there.

A module plugs in as an MCP server ([modules.md](modules.md#registering-a-module-in-odysseus)).
For Athena on this server:

- Run its MCP server on the Docker bridge address, so the container can reach it:
  `CONVO_MCP_HOST=172.17.0.1` in its `server/.env` (port 8001). `odysseus-deploy` keeps it
  running ([above](#one-command-setup-once)).
- In Settings > MCP add an HTTP server named `convo`:
  `http://host.docker.internal:8001/mcp?access_token=<CONVO_MCP_TOKEN>`.
- `ODYSSEUS_TRUSTED_READONLY_MCP=convo` is set in `docker-compose.override.yml`.
- Paste its `INSTRUCTIONS` text into a Custom preset
  ([modules/odysseus-sound.md](modules/odysseus-sound.md#connect-it)).

Check reachability from inside the container:

```bash
sudo docker compose exec odysseus python -c "import urllib.request as u
try: u.urlopen('http://host.docker.internal:8001/mcp', timeout=5); print('reachable')
except u.HTTPError as e: print('reachable, HTTP', e.code)
except Exception as e: print('NOT reachable:', e)"
```

### Call recording and phone pairing

**Record a call** and **Pair a phone** ([modules/odysseus-sound.md](modules/odysseus-sound.md#uploads-through-the-mcp-server))
need no settings: they use the `convo` MCP server Odysseus already has (Settings → MCP,
`http://host.docker.internal:8001/mcp?access_token=…`). They need Athena with MCP
0.8.0 or newer (`scripts/fork_deploy.sh deploy` updates both); with an older module the
pages say "update Athena". The module's MCP server forwards uploads to its ingest
API on `CONVO_HOST:CONVO_PORT` on the same machine, so the container never needs to
reach port 8000.

- Capture needs a secure page: open Odysseus over `https://` (for example
  `tailscale serve` with a Tailscale certificate) or on `localhost`. Over plain
  `http://<tailscale-ip>:7000` the browser refuses and the page says so.
- The phone must reach Odysseus at the address in the QR code (its Tailscale address).
- The `SOUND_MODULE_API_URL` and `SOUND_MODULE_INGEST_TOKEN` settings of an earlier draft
  are gone; if you added them to `.env` or `docker-compose.override.yml`, remove them.

## One-time setup (done 2026-10-18)

How an upstream Odysseus Compose install becomes fork production. Kept for a rebuild.

```bash
cd ~/models/odysseus
git status                                    # local edits to tracked files? move them to .env first
sudo docker compose stop odysseus
sudo tar czf ~/odysseus-data-$(date +%F).tgz data/
git remote add barvis https://github.com/SDarioosh/Barvis.git
git fetch barvis && git checkout -b barvis barvis/barvis
cp docker/fork.override.yml docker-compose.override.yml
printf 'APP_BIND=0.0.0.0\nAPP_PORT=7000\n' >> .env
mkdir -p ~/.config/odysseus-fork && chmod 700 ~/.config/odysseus-fork
read -rsp 'Odysseus admin password: ' P; printf '%s' "$P" > ~/.config/odysseus-fork/admin.txt; unset P; echo
chmod 600 ~/.config/odysseus-fork/admin.txt
scripts/fork_deploy.sh deploy
```

The admin password is the existing Odysseus one: the fork keeps the accounts.

Pitfalls met during the switch:

- **Compose runs upstream's image unless told otherwise.** `docker-compose.yml` pulls
  `ghcr.io/odysseus-dev/odysseus` and only builds locally if the pull fails. The override
  selects `odysseus-fork:current` with `pull_policy: never`.
- **Variables reach the container only if listed.** `ODYSSEUS_TRUSTED_READONLY_MCP` is
  not in upstream's `environment:` list, so it lives in the override.
- **Stay in the same folder.** Compose names volumes (ChromaDB, ntfy, SearXNG) after the
  folder; a new checkout elsewhere starts with empty ones.
- **Ports go in `.env`.** The fallback port is 7011, and a `ports:` entry in the override
  is added to the base one instead of replacing it.
- **Docker needs `sudo` here,** including inside `$( … )`.
- **"Connection reset by peer"** right after a start means the app is still starting
  (5–15 s); `deploy` waits for `/api/health`.
- Harmless warnings: `ODYSSEUS_TTS_CACHE_MAX_BYTES variable is not set` (upstream's
  compose file has no default for it) and "current commit information was not captured"
  during `sudo docker build` (root can't read a repo owned by your user; the script labels
  the image with the commit instead).
