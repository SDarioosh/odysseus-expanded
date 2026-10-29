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
