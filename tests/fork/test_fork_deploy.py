"""Fork: scripts/fork_deploy.sh with stub docker, curl and sudo.

Covers the switch from the fork's former name: an override that still runs
``barvis:...``, ``BARVIS_*`` settings, and the first deploy that has to keep the old
``barvis:current`` image as the rollback target.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

DOCKER = r"""#!/usr/bin/env bash
# images are lines in $STUB_IMAGES; every call is logged to $STUB_LOG
echo "docker $*" >> "$STUB_LOG"
case "$1 $2" in
  "image inspect")
    shift 2
    fmt=""
    if [ "$1" = "-f" ]; then fmt=$2; shift 2; fi
    grep -qx "$1" "$STUB_IMAGES" || exit 1
    [ -z "$fmt" ] || echo "rev-of-$1"
    ;;
  "tag "*) echo "$3" >> "$STUB_IMAGES" ;;
  "build -q") for a in "$@"; do prev_t=$t; t=$a; [ "$prev_t" = "-t" ] && echo "$a" >> "$STUB_IMAGES"; done ;;
esac
exit 0
"""


def _write_exec(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


@pytest.fixture
def checkout(tmp_path):
    """A checkout with the script, a remote named like the fork's repo, and stubs."""
    if not shutil.which("git") or not shutil.which("bash"):
        pytest.skip("needs git and bash")
    remote = tmp_path / "SDarioosh" / "Barvis.git"
    work = tmp_path / "work"
    (work / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "fork_deploy.sh", work / "scripts" / "fork_deploy.sh")
    (work / "docker-compose.yml").write_text("services: {}\n")
    (work / ".gitignore").write_text("data/\ndocker-compose.override.yml\n")
    (work / "data").mkdir()
    git = ["git", "-C", str(work), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "-b", "barvis", str(work)], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(remote)], check=True)
    subprocess.run([*git, "remote", "add", "origin", str(remote)], check=True)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_exec(bin_dir / "docker", DOCKER)
    _write_exec(bin_dir / "curl", "#!/bin/sh\necho ok\n")
    _write_exec(bin_dir / "sudo", '#!/bin/sh\nexec "$@"\n')
    home = tmp_path / "home"
    home.mkdir()
    images = tmp_path / "images"
    images.write_text("")
    log = tmp_path / "docker.log"
    log.write_text("")

    def run(*args, override="odysseus-fork", env=None):
        (work / "docker-compose.override.yml").write_text(
            f"services:\n  odysseus:\n    image: {override}:${{FORK_TAG:-current}}\n")
        base = {k: v for k, v in os.environ.items()
                if not k.startswith(("FORK_", "BARVIS_"))}
        full = {**base, "HOME": str(home), "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "DOCKER": "docker", "STUB_IMAGES": str(images), "STUB_LOG": str(log),
                **(env or {})}
        return subprocess.run(["bash", str(work / "scripts" / "fork_deploy.sh"), *args],
                              env=full, capture_output=True, text=True, timeout=60)

    run.work, run.home, run.images, run.log = work, home, images, log
    return run


def images(run) -> set[str]:
    return set(run.images.read_text().split())


def test_first_deploy_after_rename_keeps_the_barvis_image_for_rollback(checkout):
    checkout.images.write_text("barvis:current\n")
    r = checkout("deploy")
    assert r.returncode == 0, r.stdout + r.stderr
    sha = subprocess.run(["git", "-C", str(checkout.work), "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert {"odysseus-fork:previous", "odysseus-fork:current", f"odysseus-fork:{sha}"} <= images(checkout)
    assert f"--label odysseus-fork.revision={sha}" in checkout.log.read_text()
    assert (checkout.home / "odysseus-backups").is_dir()

    r = checkout("rollback")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "docker tag odysseus-fork:previous odysseus-fork:current" in checkout.log.read_text()


def test_override_copied_before_the_rename_keeps_using_barvis_images(checkout):
    checkout.images.write_text("barvis:current\nbarvis:previous\n")
    r = checkout("rollback", override="barvis")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "docker tag barvis:previous barvis:current" in checkout.log.read_text()
    assert "odysseus-fork" not in checkout.log.read_text()


def test_former_settings_and_config_dir_still_apply(checkout):
    module = checkout.home / "module.sh"
    _write_exec(module, '#!/bin/sh\necho "module $1"\n')
    old_config = checkout.home / ".config" / "barvis"
    old_config.mkdir(parents=True)
    (old_config / "deploy.env").write_text(f"BARVIS_MODULES={module}\n")
    (checkout.home / "barvis-backups").mkdir()
    checkout.images.write_text("odysseus-fork:current\n")

    r = checkout("status")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "running image: odysseus-fork:current (revision rev-of-odysseus-fork:current)" in r.stdout
    assert "module status" in r.stdout

    r = checkout("modules", env={"BARVIS_MODULES": "", "FORK_MODULES": str(module)})
    assert "module deploy" in r.stdout

    r = checkout("deploy")
    assert r.returncode == 0, r.stdout + r.stderr
    assert list((checkout.home / "barvis-backups").glob("data-*.tgz"))
    assert not (checkout.home / "odysseus-backups").exists()


def test_override_without_a_fork_image_is_refused(checkout):
    r = checkout("rollback", override="ghcr.io/odysseus-dev/odysseus")
    assert r.returncode != 0 and "image: odysseus-fork" in r.stderr


def test_modules_are_told_this_script_deploys_odysseus(checkout):
    # odysseus-sound's deploy.sh also updates Odysseus unless FORK_DEPLOY_RUNNING is set
    module = checkout.home / "module.sh"
    _write_exec(module, '#!/bin/sh\necho "module $1 running=$FORK_DEPLOY_RUNNING"\n')
    r = checkout("modules", env={"FORK_MODULES": str(module)})
    assert "module deploy running=1" in r.stdout


def test_empty_module_list_from_the_environment_beats_the_config(checkout):
    # how a module's deploy script runs this one without being called back
    module = checkout.home / "module.sh"
    _write_exec(module, '#!/bin/sh\necho "module $1"\n')
    config = checkout.home / ".config" / "odysseus-fork"
    config.mkdir(parents=True)
    (config / "deploy.env").write_text(f"FORK_MODULES={module}\n")
    r = checkout("deploy", env={"FORK_MODULES": "", "BARVIS_MODULES": ""})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "module deploy" not in r.stdout
    assert "no FORK_MODULES set; skipping modules" in r.stdout


def _remote_lookup(tmp_path: Path, urls: dict[str, str], env=None) -> str:
    """Run the script's remote() in a repo whose remotes have these URLs."""
    if not shutil.which("git") or not shutil.which("bash"):
        pytest.skip("needs git and bash")
    script = (ROOT / "scripts" / "fork_deploy.sh").read_text()
    start = script.index("\nremote() {") + 1
    func = script[start:script.index("\n}\n", start) + 3]
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for name, url in urls.items():
        subprocess.run(["git", "-C", str(repo), "remote", "add", name, url], check=True)
    base = {k: v for k, v in os.environ.items() if not k.startswith(("FORK_", "BARVIS_"))}
    r = subprocess.run(["bash", "-c", f"{func}\nremote"], cwd=repo, env={**base, **(env or {})},
                       capture_output=True, text=True, check=True, timeout=30)
    return r.stdout.strip()


@pytest.mark.parametrize("url", [
    "https://github.com/SDarioosh/odysseus-expanded",
    "https://github.com/SDarioosh/odysseus-expanded.git",
    "git@github.com:SDarioosh/odysseus-expanded.git",
    "ssh://git@github.com/sdarioosh/Odysseus-Expanded",
    "https://github.com/SDarioosh/Barvis",
    "git@github.com:SDarioosh/Barvis.git",
])
def test_remote_finds_the_fork_under_its_old_and_new_repo_name(tmp_path, url):
    urls = {"upstream": "https://github.com/odysseus-dev/odysseus.git", "fork": url}
    assert _remote_lookup(tmp_path, urls) == "fork"


@pytest.mark.parametrize("url", [
    "https://github.com/odysseus-dev/odysseus.git",
    "https://github.com/SDarioosh/odysseus-expanded-old",
    "https://github.com/someone/odysseus-expanded",
    "https://github.com/SDarioosh/Athena",
])
def test_remote_ignores_other_repos(tmp_path, url):
    assert _remote_lookup(tmp_path, {"other": url}) == ""


def test_remote_setting_beats_the_lookup(tmp_path):
    urls = {"origin": "https://github.com/SDarioosh/odysseus-expanded"}
    assert _remote_lookup(tmp_path, urls, env={"FORK_REMOTE": "mine"}) == "mine"


def test_deploy_from_a_clone_of_odysseus_expanded(checkout, tmp_path):
    renamed = tmp_path / "SDarioosh" / "odysseus-expanded.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(checkout.work), str(renamed)], check=True)
    subprocess.run(["git", "-C", str(checkout.work), "remote", "set-url", "origin", str(renamed)],
                   check=True)
    checkout.images.write_text("odysseus-fork:current\n")
    r = checkout("deploy")
    assert r.returncode == 0, r.stdout + r.stderr


def _athena_data(checkout) -> Path:
    """The names script and a fictional hub data dir whose MCP server is still `convo`."""
    import sqlite3
    work = checkout.work
    shutil.copy(ROOT / "scripts" / "athena_names_migrate.py", work / "scripts")
    (work / "fork" / "common").mkdir(parents=True)
    for rel in ("fork/__init__.py", "fork/common/__init__.py", "fork/common/athena_names.py"):
        shutil.copy(ROOT / rel, work / rel)
    db = work / "data" / "app.db"
    c = sqlite3.connect(db)
    c.execute("create table mcp_servers(id text, name text)")
    c.execute("insert into mcp_servers values('ab12cd34', 'convo')")
    c.commit()
    c.close()  # an open handle would make the script refuse (the hub looks running)
    return db


def _server_name(db: Path) -> str:
    import sqlite3
    c = sqlite3.connect(db)
    try:
        return c.execute("select name from mcp_servers").fetchone()[0]
    finally:
        c.close()


def test_deploy_renames_athenas_stored_names_once_and_rollback_undoes_it(checkout):
    db = _athena_data(checkout)
    checkout.images.write_text("odysseus-fork:current\n")
    mark = checkout.home / "odysseus-backups" / "athena-names-last"

    r = checkout("deploy")
    assert r.returncode == 0, r.stdout + r.stderr
    log = checkout.log.read_text()
    # after the data backup (Odysseus stopped), before the new image starts
    assert r.stdout.index("backing up") < r.stdout.index("renaming Athena's stored names") \
        < r.stdout.index("starting odysseus-fork:")
    assert "mcp_servers.name convo -> athena" in r.stdout
    assert _server_name(db) == "athena" and mark.is_file()
    assert "compose stop odysseus" in log

    r = checkout("rollback")  # back to the image from before: the old name comes back
    assert r.returncode == 0, r.stdout + r.stderr
    assert "undoing the stored-name rename" in r.stdout
    assert _server_name(db) == "convo" and not mark.exists()

    r = checkout("deploy")
    assert _server_name(db) == "athena"
    r = checkout("deploy")  # nothing left to rename: a later rollback must not undo anything
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Nothing to change" in r.stdout and not mark.exists()
    r = checkout("rollback")
    assert "undoing" not in r.stdout and _server_name(db) == "athena"


def test_deploy_can_skip_the_rename(checkout):
    db = _athena_data(checkout)
    checkout.images.write_text("odysseus-fork:current\n")
    r = checkout("deploy", env={"FORK_ATHENA_NAMES": "off"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FORK_ATHENA_NAMES=off" in r.stdout and _server_name(db) == "convo"


def test_deploy_without_the_names_script_goes_on(checkout):
    checkout.images.write_text("odysseus-fork:current\n")
    r = checkout("deploy")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "renaming" not in r.stdout


def test_deploy_builds_with_the_optional_extras(checkout):
    r = checkout("deploy")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "--build-arg INSTALL_OPTIONAL=true" in checkout.log.read_text()


@pytest.mark.parametrize("name", ["FORK_INSTALL_OPTIONAL", "BARVIS_INSTALL_OPTIONAL"])
def test_deploy_can_leave_the_optional_extras_out(checkout, name):
    r = checkout("deploy", env={name: "false"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "--build-arg INSTALL_OPTIONAL=false" in checkout.log.read_text()


# --- service users: secrets file, root-only backups and password, setup in the container ---

def _admin_password(checkout) -> Path:
    pw = checkout.home / "admin.txt"
    pw.write_text("fictional-admin-password\n")
    pw.chmod(0o600)
    return pw


def test_env_files_reach_every_compose_call_and_a_missing_one_stops_first(checkout):
    secrets = checkout.home / "secrets.env"
    secrets.write_text("OPENAI_API_KEY=fictional\n")
    (checkout.work / ".env").write_text("APP_PORT=7000\n")
    checkout.images.write_text("odysseus-fork:current\n")
    r = checkout("deploy", env={"FORK_ENV_FILES": str(secrets)})
    assert r.returncode == 0, r.stdout + r.stderr
    calls = [c for c in checkout.log.read_text().splitlines() if c.startswith("docker compose")]
    assert calls and all(c.startswith(f"docker compose --env-file .env --env-file {secrets} ")
                         for c in calls)
    assert "fictional" not in r.stdout + r.stderr  # the file is passed by name, never read

    checkout.log.write_text("")
    r = checkout("deploy", env={"FORK_ENV_FILES": str(checkout.home / "missing.env")})
    assert r.returncode != 0 and "missing.env, which doesn't exist" in r.stderr
    assert checkout.log.read_text() == ""  # nothing built or stopped


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_an_unreadable_env_file_stops_before_anything(checkout):
    env_file = checkout.work / ".env"
    env_file.write_text("APP_PORT=7000\nAPP_DATA_DIR=/srv/odysseus/data\n")
    env_file.chmod(0)
    try:
        r = checkout("deploy")
    finally:
        env_file.chmod(0o600)
    assert r.returncode != 0 and "can't read it" in r.stderr
    assert checkout.log.read_text() == ""


def test_a_missing_data_dir_stops_before_odysseus_is_stopped(checkout):
    (checkout.work / ".env").write_text(f"APP_DATA_DIR={checkout.home / 'no-such-data'}\n")
    checkout.images.write_text("odysseus-fork:current\n")
    r = checkout("deploy")
    assert r.returncode != 0 and "not found; nothing was stopped" in r.stderr
    assert "compose stop" not in checkout.log.read_text()


def test_backups_are_private(checkout):
    checkout.images.write_text("odysseus-fork:current\n")
    r = checkout("deploy")
    assert r.returncode == 0, r.stdout + r.stderr
    (backup,) = (checkout.home / "odysseus-backups").glob("data-*.tgz")
    assert backup.stat().st_mode & 0o077 == 0


def test_setup_runs_in_the_container_when_fork_py_is_missing(checkout):
    pw = _admin_password(checkout)
    checkout.images.write_text("odysseus-fork:current\n")
    r = checkout("deploy", env={"FORK_PASSWORD_FILE": str(pw),
                                "FORK_PY": str(checkout.home / "gone" / "python")})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "does not exist; running modules_setup.py inside the container" in r.stdout
    log = checkout.log.read_text()
    assert ("compose exec -T --user 1000:1000 odysseus python scripts/modules_setup.py "
            "--url http://127.0.0.1:7000 --password-file /dev/stdin") in log
    assert "fictional-admin-password" not in log  # on stdin, never a command line


def test_setup_on_the_host_gets_the_password_on_stdin(checkout):
    pw = _admin_password(checkout)
    seen = checkout.home / "py-seen"
    py = checkout.home / "python"
    _write_exec(py, f'#!/bin/sh\n[ "$1" = -c ] && exit 0\necho "args $*" >> {seen}\n'
                    f'echo "stdin $(cat)" >> {seen}\n')
    checkout.images.write_text("odysseus-fork:current\n")
    r = checkout("deploy", env={"FORK_PASSWORD_FILE": str(pw), "FORK_PY": str(py)})
    assert r.returncode == 0, r.stdout + r.stderr
    text = seen.read_text()
    assert "--password-file /dev/stdin" in text and "stdin fictional-admin-password" in text
    assert "fictional-admin-password" not in text.split("stdin")[0]


def test_setup_place_setting(checkout):
    pw = _admin_password(checkout)
    checkout.images.write_text("odysseus-fork:current\n")
    r = checkout("deploy", env={"FORK_PASSWORD_FILE": str(pw), "FORK_SETUP_IN": "container"})
    assert "running modules_setup.py (container)" in r.stdout
    r = checkout("deploy", env={"FORK_PASSWORD_FILE": str(pw), "FORK_SETUP_IN": "elsewhere"})
    assert r.returncode != 0 and "FORK_SETUP_IN must be host or container" in r.stderr


@pytest.mark.skipif(not shutil.which("docker"), reason="needs the docker compose CLI")
def test_opt_in_network_and_hardening_files_resolve(tmp_path):
    import json
    (tmp_path / "docker").mkdir()
    shutil.copy(ROOT / "docker-compose.yml", tmp_path)
    shutil.copy(ROOT / "docker" / "fork.override.yml", tmp_path / "docker-compose.override.yml")
    for name in ("fork.network.yml", "fork.hardening.yml"):
        shutil.copy(ROOT / "docker" / name, tmp_path / "docker" / name)
    (tmp_path / ".env").write_text(
        "COMPOSE_FILE=docker-compose.yml:docker-compose.override.yml:docker/fork.network.yml"
        ":docker/fork.hardening.yml\nFORK_NET_SUBNET=172.29.20.0/24\n"
        "ODYSSEUS_TTS_CACHE_MAX_BYTES=524288000\n")
    (tmp_path / "secrets.env").write_text("OPENAI_API_KEY=fictional-key\n")
    r = subprocess.run(["docker", "compose", "--env-file", ".env", "--env-file", "secrets.env",
                        "config", "--format", "json"], cwd=tmp_path, capture_output=True,
                       text=True, timeout=60)
    if r.returncode != 0 and "is not a docker command" in r.stderr:
        pytest.skip("no compose plugin")
    assert r.returncode == 0, r.stderr
    cfg = json.loads(r.stdout)
    app = cfg["services"]["odysseus"]
    assert app["image"] == "odysseus-fork:current"
    assert app["security_opt"] == ["no-new-privileges:true"] and app["cap_drop"] == ["ALL"]
    assert set(app["cap_add"]) == {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"}
    assert app["environment"]["OPENAI_API_KEY"] == "fictional-key"  # from the second file
    net = cfg["networks"]["default"]
    assert net["name"] == "odysseus-net" and net["ipam"]["config"][0]["subnet"] == "172.29.20.0/24"
    assert net["driver_opts"]["com.docker.network.bridge.name"] == "br-odysseus"
