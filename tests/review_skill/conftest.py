import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = (
    Path(__file__).resolve().parents[2] / "plugin" / "skills" / "review" / "scripts"
)
CLI_MANIFEST = Path(__file__).resolve().parents[2] / "cli" / "Cargo.toml"


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
        [sys.executable, str(SCRIPTS / "checks" / f"{name}.py"), str(repo), base],
        capture_output=True,
        text=True,
    )
    rs = subprocess.run(
        [str(cli), "check", name, str(repo), base], capture_output=True, text=True
    )
    assert (rs.stdout, rs.returncode) == (py.stdout, py.returncode)
