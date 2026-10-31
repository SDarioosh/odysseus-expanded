#!/usr/bin/env bash
# Fork: update the modules, then build and deploy our Odysseus fork with Docker Compose,
# with backup and rollback.
#
# Works from any directory: it uses the checkout it lives in, which holds
# docker-compose.yml, .env and docker-compose.override.yml (see docs/fork/deploy.md).
#
#   scripts/fork_deploy.sh deploy [REF]   # modules, then build REF (default: <remote>/barvis) and switch to it
#   scripts/fork_deploy.sh modules        # only update and restart the modules
#   scripts/fork_deploy.sh rollback       # switch back to the image before the last deploy
#   scripts/fork_deploy.sh status         # what is running, modules included
#
# Settings: environment, or KEY=value lines in ~/.config/odysseus-fork/deploy.env (all optional):
#   FORK_MODULES        module deploy commands, space separated; each is run with
#                       "deploy" or "status" (docs/fork/modules.md#deploying-a-module),
#                       e.g. ~/projects/barvis-sound/server/scripts/deploy.sh
#   DOCKER              docker command (default: "sudo docker")
#   FORK_REMOTE         git remote of this repo (default: the one pointing at
#                       SDarioosh/odysseus-expanded or SDarioosh/Barvis)
#   FORK_BACKUP_DIR     where data backups go (default: ~/odysseus-backups)
#   FORK_KEEP_BACKUPS   how many backups to keep (default: 5)
#   FORK_PASSWORD_FILE  admin password file for modules_setup.py (default: ~/.config/odysseus-fork/admin.txt)
#   FORK_PY             python with httpx for modules_setup.py (default: venv/bin/python, else python3;
#                       without httpx there, it runs inside the odysseus container)
#   FORK_SETUP_IN       host (default) or container: where modules_setup.py runs
#   FORK_SETUP_ARGS     extra arguments for modules_setup.py (e.g. "--trim")
#   FORK_INSTALL_OPTIONAL  build the image with requirements-optional.txt (PDF viewer and
#                       forms via PyMuPDF, local OCR and speech-to-text, Office extraction):
#                       true (default) or false
#   FORK_ENV_FILES      more env files for compose, space separated, e.g. a root-only
#                       /etc/odysseus/secrets.env with the tokens and keys (.env keeps the rest)
#
# The former names still work: BARVIS_<name> for each FORK_<name>, ~/.config/barvis/,
# ~/barvis-backups, and the barvis:<commit> images (see docs/fork/deploy.md).
set -euo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.."
# former config dir, used while the new one doesn't exist
CONFIG_DIR=$HOME/.config/odysseus-fork
[ -d "$CONFIG_DIR" ] || [ ! -d "$HOME/.config/barvis" ] || CONFIG_DIR=$HOME/.config/barvis
CONFIG=${FORK_DEPLOY_CONFIG:-${BARVIS_DEPLOY_CONFIG:-$CONFIG_DIR/deploy.env}}
if [ -f "$CONFIG" ]; then  # KEY=value lines; the environment wins
  while IFS= read -r line || [ -n "$line" ]; do
    [[ $line =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
    key=${BASH_REMATCH[1]} value=${BASH_REMATCH[2]}
    value=${value#[\"\']} value=${value%[\"\']} value=${value/#\~/$HOME}
    [ -n "${!key+x}" ] || export "$key=$value"
  done < "$CONFIG"
fi

for name in MODULES REMOTE BACKUP_DIR KEEP_BACKUPS PASSWORD_FILE PY SETUP_ARGS INSTALL_OPTIONAL SETUP_IN ENV_FILES; do
  old=BARVIS_$name new=FORK_$name  # former names, still read
  [ -n "${!new+x}" ] || [ -z "${!old+x}" ] || export "$new=${!old}"
done

default_backups=$HOME/odysseus-backups
[ -d "$default_backups" ] || [ ! -d "$HOME/barvis-backups" ] || default_backups=$HOME/barvis-backups
DOCKER=${DOCKER:-sudo docker}
BACKUP_DIR=${FORK_BACKUP_DIR:-$default_backups}
KEEP_BACKUPS=${FORK_KEEP_BACKUPS:-5}
# Upstream's image leaves the optional extras out; the fork is AGPL already, so PyMuPDF's AGPL adds nothing
INSTALL_OPTIONAL=${FORK_INSTALL_OPTIONAL:-true}
PASSWORD_FILE=${FORK_PASSWORD_FILE:-$CONFIG_DIR/admin.txt}
SERVICE=odysseus
LEGACY_IMAGE=barvis  # the images' former name

die() { echo "error: $*" >&2; exit 1; }
log() { echo "==> $*"; }

# Compose reads .env by itself; FORK_ENV_FILES adds files to it (passing --env-file turns
# the automatic .env off, so .env is passed first). A root-only file works because compose
# runs as root (DOCKER=sudo docker). Nothing here reads or prints those files.
COMPOSE_ENV=()
if [ -n "${FORK_ENV_FILES:-}" ]; then
  [ ! -f .env ] || COMPOSE_ENV+=(--env-file .env)
  for f in $FORK_ENV_FILES; do COMPOSE_ENV+=(--env-file "${f/#\~/$HOME}"); done
fi
compose() { $DOCKER compose ${COMPOSE_ENV[@]+"${COMPOSE_ENV[@]}"} "$@"; }

env_value() {  # value of KEY in .env, or the default
  local v=""
  # Never guess: a default port or data dir would make deploy check the wrong URL or back
  # up the wrong folder. Secrets belong in FORK_ENV_FILES; .env must stay readable.
  [ ! -e .env ] || [ -r .env ] \
    || die "$(pwd)/.env exists but $(id -un) can't read it; keep the non-secret settings there (readable) and move the secrets to a file in FORK_ENV_FILES (docs/fork/deploy.md)"
  [ -f .env ] && v=$(grep -E "^$1=" .env | tail -n 1 | cut -d= -f2- | tr -d '"'"'" || true)
  echo "${v:-$2}"
}

as_backup_owner() {  # run "$@" as whoever can write the backup dir: this user, else root
  if [ -w "$BACKUP_DIR" ] && [ -x "$BACKUP_DIR" ]; then "$@"; else sudo "$@"; fi
}

APP_PORT=$(env_value APP_PORT 7011)
DATA_DIR=$(env_value APP_DATA_DIR ./data)
URL="http://127.0.0.1:${APP_PORT}"

# Image name: whatever docker-compose.override.yml runs, odysseus-fork (docker/fork.override.yml)
# or the former barvis, so an override copied before the rename keeps working.
override_image() {
  sed -nE 's/^[[:space:]]*image:[[:space:]]*(odysseus-fork|barvis):.*/\1/p' docker-compose.override.yml 2>/dev/null | head -n 1
}
IMAGE=$(override_image)
IMAGE=${IMAGE:-odysseus-fork}

preflight() {
  [ -f docker-compose.yml ] || die "run this from the fork's checkout (no docker-compose.yml here)"
  [ -f docker-compose.override.yml ] || die "docker-compose.override.yml is missing; copy docker/fork.override.yml (docs/fork/deploy.md)"
  [ -n "$(override_image)" ] \
    || die "docker-compose.override.yml does not set 'image: odysseus-fork:...'; compose would run upstream's image"
  local f
  for f in ${FORK_ENV_FILES:-}; do
    f=${f/#\~/$HOME}
    [ -f "$f" ] || sudo test -f "$f" || die "FORK_ENV_FILES names $f, which doesn't exist"
  done
}

has_image() { $DOCKER image inspect "$1" >/dev/null 2>&1; }

wait_healthy() {
  local i
  for i in $(seq 1 60); do
    if curl -sf "$URL/api/health" >/dev/null 2>&1; then
      log "healthy on $URL"
      return 0
    fi
    sleep 2
  done
  return 1
}

image_rev() {  # commit label of an image (or its former label), or "none"
  local rev
  rev=$($DOCKER image inspect -f '{{or (index .Config.Labels "odysseus-fork.revision") (index .Config.Labels "barvis.revision")}}' "$1" 2>/dev/null | head -n 1 || true)
  [ "$rev" = "<no value>" ] && rev=""
  echo "${rev:-none}"
}

switch_to() {  # retag $1 as $IMAGE:current and recreate the container
  $DOCKER tag "$1" "$IMAGE:current" && compose up -d
}

backup_data() {
  # The backup dir may be root-only (backups hold app.db): then root writes and prunes it.
  mkdir -p "$BACKUP_DIR" 2>/dev/null || sudo install -d -m 0700 "$BACKUP_DIR"
  # Checked before anything is stopped: the data dir may be readable by its owner only.
  [ -d "$DATA_DIR" ] || sudo test -d "$DATA_DIR" \
    || die "data dir $DATA_DIR (APP_DATA_DIR in .env) not found; nothing was stopped"
  local file
  file="$BACKUP_DIR/data-$(date +%Y%m%d-%H%M%S)-$(image_rev "$IMAGE:previous").tgz"  # the image being replaced
  log "stopping $SERVICE and backing up $DATA_DIR to $file"
  compose stop "$SERVICE"
  sudo sh -c 'umask 077 && exec tar czf "$1" -C "$2" "$3"' sh "$file" "$(dirname "$DATA_DIR")" "$(basename "$DATA_DIR")"
  as_backup_owner sh -c 'ls -1t "$1"/data-*.tgz 2>/dev/null | tail -n +"$2" | xargs -r rm -f' \
    sh "$BACKUP_DIR" "$((KEEP_BACKUPS + 1))"
}

# Fork: rename Athena's stored names (convo / "odysseus-sound" -> athena) once, while
# Odysseus is stopped and right after backup_data. Idempotent: later deploys find nothing
# to change. The code reads both names, so a failure here leaves a working hub.
# FORK_ATHENA_NAMES=off skips it. Sets NAMES_BACKUP when it changed something, and keeps
# it in $NAMES_MARK so a later `rollback` to the image before this deploy undoes it too.
NAMES_BACKUP=""
NAMES_MARK=$BACKUP_DIR/athena-names-last
migrate_names() {
  local out
  as_backup_owner rm -f "$NAMES_MARK"  # only the deploy that renamed may undo it
  [ "${FORK_ATHENA_NAMES:-on}" != off ] || { log "FORK_ATHENA_NAMES=off; not renaming Athena's stored names"; return 0; }
  [ -f scripts/athena_names_migrate.py ] || return 0
  log "renaming Athena's stored names (scripts/athena_names_migrate.py; backup first)"
  if out=$(sudo python3 scripts/athena_names_migrate.py --data-dir "$DATA_DIR" --apply --i-stopped-the-hub 2>&1); then
    echo "$out" | sed 's/^/    /'
    NAMES_BACKUP=$(echo "$out" | sed -n 's/^backup: //p' | tail -n 1)
    [ -z "$NAMES_BACKUP" ] || printf '%s\n' "$NAMES_BACKUP" | as_backup_owner tee "$NAMES_MARK" >/dev/null
  else
    echo "$out" | sed 's/^/    /' >&2
    log "renaming the stored names failed; nothing was renamed by this step, the hub reads the old names as before"
  fi
}

undo_names() {  # before going back to an older image, which may know only the old names
  [ -n "$NAMES_BACKUP" ] || return 0
  log "undoing the stored-name rename ($NAMES_BACKUP)"
  compose stop "$SERVICE" || true
  sudo python3 scripts/athena_names_migrate.py --data-dir "$DATA_DIR" --rollback "$NAMES_BACKUP" --i-stopped-the-hub \
    || log "could not undo it; restore the newest backup in $BACKUP_DIR (docs/fork/deploy.md)"
  as_backup_owner rm -f "$NAMES_MARK"
  NAMES_BACKUP=""
}

password() {  # the admin password file's text on stdout, read as root if it is root-only
  if [ -r "$PASSWORD_FILE" ]; then cat "$PASSWORD_FILE"; else sudo cat "$PASSWORD_FILE"; fi
}

run_setup() {
  if [ ! -f "$PASSWORD_FILE" ] && ! sudo test -f "$PASSWORD_FILE"; then
    log "no $PASSWORD_FILE; run scripts/modules_setup.py yourself"
    return 0
  fi
  local py=${FORK_PY:-} where=${FORK_SETUP_IN:-host}
  case $where in host|container) ;; *) die "FORK_SETUP_IN must be host or container, not \"$where\"" ;; esac
  if [ "$where" = host ]; then
    if [ -n "$py" ] && [ ! -x "${py/#\~/$HOME}" ]; then
      log "FORK_PY=$py does not exist; running modules_setup.py inside the container instead"
      where=container
    elif [ -z "$py" ]; then
      if [ -x venv/bin/python ]; then py=venv/bin/python; else py=python3; fi
    fi
  fi
  if [ "$where" = host ] && ! "${py/#\~/$HOME}" -c 'import httpx' >/dev/null 2>&1; then
    log "$py has no httpx; running modules_setup.py inside the container instead"
    where=container
  fi
  log "running modules_setup.py ($where)"
  # The password goes in on stdin, never on a command line or into a file.
  if [ "$where" = container ]; then
    # shellcheck disable=SC2086
    password | compose exec -T --user "$(env_value PUID 1000):$(env_value PGID 1000)" "$SERVICE" \
      python scripts/modules_setup.py --url http://127.0.0.1:7000 --password-file /dev/stdin ${FORK_SETUP_ARGS:-} \
      || log "modules_setup.py failed in the container; Odysseus is running. Rerun it by hand (docs/fork/deploy.md)"
  else
    # shellcheck disable=SC2086
    password | "${py/#\~/$HOME}" scripts/modules_setup.py --url "$URL" --password-file /dev/stdin ${FORK_SETUP_ARGS:-} \
      || log "modules_setup.py failed; Odysseus is running. Rerun it by hand, or set FORK_SETUP_IN=container (docs/fork/deploy.md)"
  fi
}

remote() {
  if [ -n "${FORK_REMOTE:-}" ]; then echo "$FORK_REMOTE"; return; fi
  # The repo is SDarioosh/odysseus-expanded, formerly SDarioosh/Barvis: match both.
  git remote -v | awk 'tolower($2) ~ /[\/:]sdarioosh\/(barvis|odysseus-expanded)(\.git)?$/ {print $1; exit}'
}

module_cmds() {  # FORK_MODULES, one per line, ~ expanded
  local m
  for m in ${FORK_MODULES:-}; do echo "${m/#\~/$HOME}"; done
}

# Update and restart each module before Odysseus, so it comes up against the new
# module servers. A failed module is reported at the end; Odysseus is still deployed.
MODULES_FAILED=()
deploy_modules() {
  local m
  [ -n "${FORK_MODULES:-}" ] || { log "no FORK_MODULES set; skipping modules"; return 0; }
  if [ "$(id -u)" = 0 ] && [ -n "${SUDO_USER:-}" ]; then
    log "started with sudo; skipping modules (they run as your user; run this script without sudo, it calls sudo for docker itself)"
    MODULES_FAILED+=(all)
    return 0
  fi
  while IFS= read -r m; do
    if [ ! -x "$m" ]; then
      log "module deploy command not found or not executable: $m"
      MODULES_FAILED+=("$m")
      continue
    fi
    log "module: $m deploy"
    # FORK_DEPLOY_RUNNING: a module deploy that can also update Odysseus (Athena's
    # deploy.sh) leaves that to this script.
    FORK_DEPLOY_RUNNING=1 "$m" deploy < /dev/null || MODULES_FAILED+=("$m")
  done < <(module_cmds)
}

modules_result() {
  [ ${#MODULES_FAILED[@]} -eq 0 ] || die "module deploy failed: ${MODULES_FAILED[*]} (see the output above)"
}

cmd_deploy() {
  preflight
  git diff --quiet HEAD -- || die "tracked files have local changes; move them to .env or docker-compose.override.yml first (git status)"
  deploy_modules
  local r ref sha
  r=$(remote); [ -n "$r" ] || die "no git remote points at the fork's repo (SDarioosh/odysseus-expanded or SDarioosh/Barvis); set FORK_REMOTE"
  ref=${1:-$r/barvis}
  log "fetching $r"
  git fetch -q "$r"
  if [ "$ref" = "$r/barvis" ] && git show-ref -q --verify refs/heads/barvis; then
    git checkout -q barvis
    git merge -q --ff-only "$ref" || die "local branch barvis has diverged from $ref"
  else
    git checkout -q --detach "$ref"
  fi
  sha=$(git rev-parse --short HEAD)
  log "building $IMAGE:$sha"
  $DOCKER build -q --build-arg "INSTALL_OPTIONAL=$INSTALL_OPTIONAL" \
    --label "odysseus-fork.revision=$sha" -t "$IMAGE:$sha" . >/dev/null

  if has_image "$IMAGE:current"; then
    $DOCKER tag "$IMAGE:current" "$IMAGE:previous"
  elif has_image "$LEGACY_IMAGE:current"; then  # first deploy after the rename
    $DOCKER tag "$LEGACY_IMAGE:current" "$IMAGE:previous"
  fi
  backup_data
  migrate_names
  log "starting $IMAGE:$sha"
  if ! { switch_to "$IMAGE:$sha" && wait_healthy; }; then
    compose logs --tail 40 "$SERVICE" >&2 || true
    if has_image "$IMAGE:previous"; then
      log "$IMAGE:$sha did not start or is not healthy after 2 min; rolling back to $IMAGE:previous"
      undo_names
      switch_to "$IMAGE:previous" && wait_healthy || true
      die "deploy of $sha failed and was rolled back. If the database was migrated, restore the newest backup in $BACKUP_DIR (docs/fork/deploy.md)"
    fi
    die "deploy of $sha failed and there is no previous image to roll back to"
  fi
  run_setup
  log "deployed $sha"
  modules_result
}

cmd_rollback() {
  preflight
  has_image "$IMAGE:previous" || die "no $IMAGE:previous image"
  NAMES_BACKUP=$(as_backup_owner cat "$NAMES_MARK" 2>/dev/null || true)
  undo_names
  log "switching back to $IMAGE:previous"
  switch_to "$IMAGE:previous" && wait_healthy || die "not healthy after rollback; see: $DOCKER compose logs $SERVICE"
  local latest
  latest=$(as_backup_owner sh -c 'ls -1t "$1"/data-*.tgz 2>/dev/null | head -n 1' sh "$BACKUP_DIR" || true)
  log "rolled back. If the newer version changed the database, restore its pre-deploy backup:"
  echo "    $DOCKER compose ${COMPOSE_ENV[*]:-}${COMPOSE_ENV[*]:+ }stop $SERVICE && sudo rm -rf $DATA_DIR && sudo tar xzf ${latest:-<backup>} -C $(dirname "$DATA_DIR") && $DOCKER compose ${COMPOSE_ENV[*]:-}${COMPOSE_ENV[*]:+ }up -d"
}

cmd_status() {
  echo "running image: $IMAGE:current (revision $(image_rev "$IMAGE:current"))"
  echo "checkout:      $(git rev-parse --short HEAD) $(git log -1 --format=%s)"
  echo -n "health:        "; curl -s "$URL/api/health" || echo "not answering on $URL"
  echo
  local m
  while IFS= read -r m; do
    [ -x "$m" ] && "$m" status < /dev/null || echo "module $m: status failed"
  done < <(module_cmds)
}

case "${1:-}" in
  deploy) shift; cmd_deploy "$@" ;;
  modules) deploy_modules; modules_result; log "modules done" ;;
  rollback) cmd_rollback ;;
  status) cmd_status ;;
  *) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
