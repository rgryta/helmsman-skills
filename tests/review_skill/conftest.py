import subprocess
from pathlib import Path

import pytest

SCRIPTS = (
    Path(__file__).resolve().parents[2] / "plugin" / "skills" / "review" / "scripts"
)


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
