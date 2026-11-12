import shutil
import signal
import subprocess
import sys
import time

import pytest

from .conftest import SCRIPTS, _git, commit_all
from .test_phase0 import load
from .test_toolchain_checks import _isolated_bin, _shim

needs_pytest = pytest.mark.skipif(
    not (shutil.which("pytest") or shutil.which("uv")), reason="no pytest on PATH"
)


@needs_pytest
def test_survivor_reported_for_vacuous_test(fixture_repo):
    (fixture_repo / "pkg" / "mod.py").write_text(
        "def check_guard(n):\n    return n > 10\n"
    )
    (fixture_repo / "pkg" / "test_mod.py").write_text(
        "from pkg.mod import check_guard\n\n\ndef test_guard():\n    assert check_guard(50) in (True, False)\n"
    )
    commit_all(fixture_repo)
    mod = load("h_mutation", "checks/h_mutation.py")
    out = mod.run(str(fixture_repo), "origin/main")
    assert out.startswith("FINDINGS:") and "SURVIVED" in out
    assert (
        fixture_repo / "pkg" / "mod.py"
    ).read_text() == "def check_guard(n):\n    return n > 10\n"


@needs_pytest
def test_uncovered_guard_not_reported_as_killed(fixture_repo):
    (fixture_repo / "pkg" / "mod.py").write_text(
        "def check_guard(n):\n    return n > 10\n"
    )
    commit_all(fixture_repo)
    mod = load("h_mutation", "checks/h_mutation.py")
    out = mod.run(str(fixture_repo), "origin/main")
    assert "all 1 probed mutants killed" not in out
    assert "check_guard" in out


def test_inconclusive_runner_not_reported_as_killed(fixture_repo, monkeypatch):
    (fixture_repo / "pkg" / "mod.py").write_text(
        "def check_guard(n):\n    return n > 10\n"
    )
    commit_all(fixture_repo)
    mod = load("h_mutation", "checks/h_mutation.py")
    monkeypatch.setattr(mod, "_run_pkg_tests", lambda *a: 2)
    out = mod.run(str(fixture_repo), "origin/main")
    assert "all 1 probed mutants killed" not in out
    assert "check_guard" in out
    assert "exited 2" in out


def test_inconclusive_runner_reports_its_own_exit_code(fixture_repo, monkeypatch):
    (fixture_repo / "pkg" / "mod.py").write_text(
        "def check_guard(n):\n    return n > 10\n"
    )
    commit_all(fixture_repo)
    mod = load("h_mutation", "checks/h_mutation.py")
    monkeypatch.setattr(mod, "_run_pkg_tests", lambda *a: 7)
    out = mod.run(str(fixture_repo), "origin/main")
    assert "all 1 probed mutants killed" not in out
    assert "check_guard" in out
    assert "exited 7" in out
    assert "exited 2" not in out


@needs_pytest
def test_killed_mutants_pass_clean(fixture_repo):
    (fixture_repo / "pkg" / "mod.py").write_text(
        "def check_guard(n):\n    return n > 10\n"
    )
    (fixture_repo / "pkg" / "test_mod.py").write_text(
        "from pkg.mod import check_guard\n\n\ndef test_guard():\n    assert check_guard(50)\n    assert not check_guard(3)\n"
    )
    commit_all(fixture_repo)
    out = load("h_mutation", "checks/h_mutation.py").run(
        str(fixture_repo), "origin/main"
    )
    assert out.startswith("OK:")


def test_restore_happens_even_when_tests_crash(fixture_repo, monkeypatch):
    (fixture_repo / "pkg" / "mod.py").write_text(
        "def check_guard(n):\n    return n > 10\n"
    )
    commit_all(fixture_repo)
    mod = load("h_mutation", "checks/h_mutation.py")
    monkeypatch.setattr(
        mod, "_run_pkg_tests", lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    with pytest.raises(RuntimeError):
        mod.run(str(fixture_repo), "origin/main")
    assert (
        fixture_repo / "pkg" / "mod.py"
    ).read_text() == "def check_guard(n):\n    return n > 10\n"


def test_restore_is_byte_exact_for_crlf_line_endings(fixture_repo, monkeypatch):
    content = "def check_guard(n):\r\n    return n > 10  # café\r\n"
    mod_path = fixture_repo / "pkg" / "mod.py"
    mod_path.write_bytes(content.encode("utf-8"))
    commit_all(fixture_repo)
    mod = load("h_mutation", "checks/h_mutation.py")
    monkeypatch.setattr(mod, "_run_pkg_tests", lambda *a: 1)
    mod.run(str(fixture_repo), "origin/main")
    assert mod_path.read_bytes() == content.encode("utf-8")


@needs_pytest
def test_test_file_own_assertions_are_not_mutated(fixture_repo):
    (fixture_repo / "pkg" / "test_validate_thing.py").write_text(
        "def test_validate_thing():\n    n = 50\n    assert n > 10\n"
    )
    commit_all(fixture_repo)
    mod = load("h_mutation", "checks/h_mutation.py")
    out = mod.run(str(fixture_repo), "origin/main")
    assert "probed mutants killed" not in out
    assert out.startswith("OK:")


@needs_pytest
def test_pkg_dir_walks_up_to_owning_pyproject(tmp_path):
    repo = tmp_path / "repo2"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "conftest.py").write_text("")
    proj = repo / "proj"
    (proj / "sub").mkdir(parents=True)
    (proj / "tests").mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "proj"\nversion = "0"\n')
    (proj / "conftest.py").write_text(
        "import sys\nfrom pathlib import Path\n\n"
        "sys.path.insert(0, str(Path(__file__).parent / 'sub'))\n"
    )
    (proj / "sub" / "mod.py").write_text("def check_guard(n):\n    return n > 10\n")
    (proj / "tests" / "test_mod.py").write_text(
        "from mod import check_guard\n\n\n"
        "def test_guard():\n    assert check_guard(50)\n    assert not check_guard(3)\n"
    )
    commit_all(repo, "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (proj / "sub" / "mod.py").write_text(
        "def check_guard(n):\n    return n > 10\n\n\n# touched\n"
    )
    commit_all(repo, "change")
    mod = load("h_mutation", "checks/h_mutation.py")
    out = mod.run(str(repo), "origin/main")
    assert "UNCOVERED" not in out
    assert out.startswith("OK:")


@needs_pytest
def test_pkg_dir_finds_pyproject_at_git_root(tmp_path):
    repo = tmp_path / "repo3"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "pyproject.toml").write_text('[project]\nname = "root"\nversion = "0"\n')
    (repo / "conftest.py").write_text(
        "import sys\nfrom pathlib import Path\n\n"
        "sys.path.insert(0, str(Path(__file__).parent / 'src'))\n"
    )
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "src" / "pkg" / "__init__.py").write_text("")
    (repo / "tests").mkdir()
    (repo / "src" / "pkg" / "mod.py").write_text(
        "def check_guard(n):\n    return n > 10\n"
    )
    (repo / "tests" / "test_mod.py").write_text(
        "from pkg.mod import check_guard\n\n\n"
        "def test_guard():\n    assert check_guard(50)\n    assert not check_guard(3)\n"
    )
    commit_all(repo, "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / "src" / "pkg" / "mod.py").write_text(
        "def check_guard(n):\n    return n > 10\n\n\n# touched\n"
    )
    commit_all(repo, "change")
    mod = load("h_mutation", "checks/h_mutation.py")
    out = mod.run(str(repo), "origin/main")
    assert "UNCOVERED" not in out
    assert out.startswith("OK:")


@needs_pytest
def test_sigterm_restores_mutated_file_before_exit(fixture_repo):
    (fixture_repo / "pkg" / "mod.py").write_text(
        "def check_guard(n):\n    return n > 10\n"
    )
    (fixture_repo / "pkg" / "test_mod.py").write_text(
        "import time\n\nfrom pkg.mod import check_guard\n\n\n"
        "def test_guard():\n    time.sleep(6)\n    assert check_guard(50)\n"
    )
    commit_all(fixture_repo)
    mod_path = fixture_repo / "pkg" / "mod.py"
    original = mod_path.read_bytes()
    proc = subprocess.Popen(
        [
            sys.executable,
            str(SCRIPTS / "checks" / "h_mutation.py"),
            str(fixture_repo),
            "origin/main",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 20
        mutated_seen = False
        while time.monotonic() < deadline:
            if mod_path.read_bytes() != original:
                mutated_seen = True
                break
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        if not mutated_seen:
            pytest.fail("mutant file was never observed before the process exited")
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    assert mod_path.read_bytes() == original


def test_probe_skips_when_the_runner_never_spawns(fixture_repo, tmp_path, monkeypatch):
    """uv failing to start pytest is no verdict on any mutant."""
    bin_dir = _isolated_bin(tmp_path, monkeypatch)
    _shim(
        bin_dir,
        "uv",
        '#!/bin/sh\ncase "$1" in sync) exit 0;; esac\n'
        "echo 'error: Failed to spawn: `pytest`' 1>&2\nexit 2\n",
    )
    (fixture_repo / "uv.lock").write_text("")
    (fixture_repo / "pkg" / "mod.py").write_text(
        "def check_guard(n):\n    return n > 10\n"
    )
    commit_all(fixture_repo)
    out = load("h_mutation", "checks/h_mutation.py").run(
        str(fixture_repo), "origin/main"
    )
    assert out.startswith("SKIPPED: pytest unavailable"), out
    assert "INCONCLUSIVE" not in out
    assert (
        fixture_repo / "pkg" / "mod.py"
    ).read_text() == "def check_guard(n):\n    return n > 10\n"
