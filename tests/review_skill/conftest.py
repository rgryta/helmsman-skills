import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

if os.name == "nt":
    import _winapi

SCRIPTS = (
    Path(__file__).resolve().parents[2] / "plugin" / "skills" / "review" / "scripts"
)
CLI_MANIFEST = Path(__file__).resolve().parents[2] / "cli" / "Cargo.toml"
STOP_SIGNAL = signal.SIGBREAK if os.name == "nt" else signal.SIGTERM
NEW_GROUP = (
    {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
)
posix_only = pytest.mark.skipif(
    os.name == "nt", reason="exercises POSIX signals or shell shims"
)


def skip_shims_on_windows():
    if os.name == "nt":
        pytest.skip("#!/bin/sh shims cannot be executed on Windows")


def stop(proc):
    """What Phase 0 sends a process it stops: SIGTERM, or a console break to a NEW_GROUP child."""
    if os.name == "nt":
        os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
    else:
        proc.send_signal(signal.SIGTERM)


def alive(pid):
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True
    try:
        handle = _winapi.OpenProcess(_winapi.SYNCHRONIZE, False, pid)
    except OSError:
        return False
    try:
        return _winapi.WaitForSingleObject(handle, 0) != _winapi.WAIT_OBJECT_0
    finally:
        _winapi.CloseHandle(handle)


def gone_within(pid, seconds):
    """A killed orphan stays a zombie until init reaps it."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return False


@pytest.fixture(autouse=True)
def _phase0_lock_in_tmp(monkeypatch, tmp_path):
    """The machine-wide heavy-check lock must never be a real one under the home dir."""
    monkeypatch.setenv("HELMSMAN_PHASE0_LOCK", str(tmp_path / "phase0.lock"))


@pytest.fixture(autouse=True)
def _no_inherited_phase0_env(monkeypatch):
    """Phase 0 gating this repo exports these, and a test must not inherit its scope."""
    monkeypatch.delenv("HELMSMAN_PYTEST_SCOPE", raising=False)
    monkeypatch.delenv("HELMSMAN_PHASE0_CHECK_S", raising=False)
    monkeypatch.delenv("HELMSMAN_PYTEST_RUNNER", raising=False)
    monkeypatch.delenv("HELMSMAN_PHASE0_SKIP", raising=False)
    monkeypatch.setenv("HELMSMAN_CLI", "off")


@pytest.fixture(autouse=True)
def _no_commit_signing(monkeypatch):
    """A global commit.gpgsign makes every fixture commit call the signing agent, which fails under load."""
    for i, (key, value) in enumerate(
        (("commit.gpgsign", "false"), ("tag.gpgsign", "false"))
    ):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{i}", key)
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{i}", value)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def commit_all(repo: Path, msg: str = "x") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", msg)


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "conftest.py").write_text("")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "pyproject.toml").write_text(
        '[project]\nname = "pkg"\nversion = "0"\n'
    )
    (repo / "pkg" / "mod.py").write_text("def existing(x):\n    return x + 1\n")
    commit_all(repo, "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / "pkg" / "mod.py").write_text(
        "def existing(x):\n    return x + 1\n\n\ndef added(y):\n    return y * 2\n"
    )
    commit_all(repo, "change")
    return repo


@pytest.fixture(scope="session")
def helmsman_cli() -> Path:
    """The real binary: HELMSMAN_CLI when set, else a release build of cli/."""
    env = os.environ.get("HELMSMAN_CLI")
    if env and env != "off":
        return Path(env)
    if not shutil.which("cargo"):
        pytest.skip("cargo not on PATH and HELMSMAN_CLI unset, so no binary to test")
    r = subprocess.run(
        ["cargo", "build", "--release", "--manifest-path", str(CLI_MANIFEST)],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr[-2000:]
    exe = "helmsman.exe" if os.name == "nt" else "helmsman"
    return CLI_MANIFEST.parent / "target" / "release" / exe


def ported_checks(cli: Path) -> list[str]:
    r = subprocess.run([str(cli), "checks"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.split()


def assert_parity(cli: Path, name: str, repo: Path, base: str = "origin/main"):
    """The binary's `check` prints exactly what the Python check prints, with its exit code."""
    py = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(SCRIPTS / "checks" / f"{name}.py"),
            str(repo),
            base,
        ],
        capture_output=True,
        text=True,
    )
    rs = subprocess.run(
        [str(cli), "check", name, str(repo), base], capture_output=True, text=True
    )
    assert (rs.stdout, rs.returncode) == (py.stdout, py.returncode)
