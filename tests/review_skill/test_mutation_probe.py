import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from .conftest import NEW_GROUP, SCRIPTS, _git, commit_all, posix_only, stop
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
    codes = iter([0, 2])
    monkeypatch.setattr(mod, "_run_pkg_tests", lambda *a: next(codes))
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
    codes = iter([0, 7])
    monkeypatch.setattr(mod, "_run_pkg_tests", lambda *a: next(codes))
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
        "def test_guard():\n    if check_guard(5):\n        time.sleep(30)\n"
        "    assert check_guard(50)\n"
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
        **NEW_GROUP,
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
        stop(proc)
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


def _repo_without_pyproject(tmp_path):
    repo = tmp_path / "repo_no_pkg"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "loose").mkdir()
    (repo / "loose" / "mod.py").write_text("def check_guard(n):\n    return n > 1\n")
    commit_all(repo, "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / "loose" / "mod.py").write_text("def check_guard(n):\n    return n > 10\n")
    commit_all(repo, "change")
    return repo


def test_no_owning_package_is_skipped_not_uncovered(tmp_path, monkeypatch):
    """Without a pyproject.toml there is no test dir, which says nothing about coverage."""
    repo = _repo_without_pyproject(tmp_path)
    mod = load("h_mutation", "checks/h_mutation.py")
    monkeypatch.setattr(mod, "_run_pkg_tests", lambda *a: pytest.fail("ran tests"))
    out = mod.run(str(repo), "origin/main")
    assert out.startswith("SKIPPED:"), out
    assert "UNCOVERED" not in out
    assert "loose/mod.py has no owning package" in out


def test_env_sync_time_counts_against_the_budget(fixture_repo, monkeypatch):
    """A slow sync leaves too little budget for a test run, so no mutant is started."""
    (fixture_repo / "pkg" / "mod.py").write_text(
        "def check_guard(n):\n    return n > 10\n"
    )
    commit_all(fixture_repo)
    mod = load("h_mutation", "checks/h_mutation.py")
    clock = [0.0]

    class FakeTime:
        @staticmethod
        def monotonic():
            return clock[0]

    def slow_sync(worktree, timeout):
        clock[0] = mod.BUDGET_S - 5
        return None, False, "synced"

    monkeypatch.setattr(mod, "time", FakeTime)
    monkeypatch.setattr(mod._env, "ensure_project_env", slow_sync)
    monkeypatch.setattr(mod, "_run_pkg_tests", lambda *a: pytest.fail("ran tests"))
    out = mod.run(str(fixture_repo), "origin/main")
    assert out.startswith("SKIPPED:"), out
    assert "budget spent" in out


def test_termination_keeps_already_survived_mutants(fixture_repo, monkeypatch):
    """A SIGTERM mid-run must still report the survivors found before it."""
    mod_path = fixture_repo / "pkg" / "mod.py"
    original = "def check_guard(n):\n    return n > 10 and n < 20\n"
    mod_path.write_text(original)
    commit_all(fixture_repo)
    mod = load("h_mutation", "checks/h_mutation.py")
    codes = iter([0, 0])

    def run_tests(*a):
        code = next(codes, None)
        if code is None:
            raise mod._Terminated("signal 15 received, restored and exiting")
        return code

    monkeypatch.setattr(mod, "_run_pkg_tests", run_tests)
    out = mod.run(str(fixture_repo), "origin/main")
    assert out.startswith("FINDINGS: 1/1 mutants survived"), out
    assert "SURVIVED" in out and "results are partial" in out
    assert mod_path.read_text() == original


@needs_pytest
def test_unrelated_red_test_never_counts_mutants_as_killed(fixture_repo):
    """A suite already red without a mutant says nothing about any mutant."""
    (fixture_repo / "pkg" / "mod.py").write_text(
        "def check_guard(n):\n    return n > 10\n"
    )
    (fixture_repo / "pkg" / "test_mod.py").write_text(
        "from pkg.mod import check_guard\n\n\n"
        "def test_db():\n    raise ConnectionError('db down')\n\n\n"
        "def test_guard():\n    assert check_guard(50) in (True, False)\n"
    )
    commit_all(fixture_repo)
    out = load("h_mutation", "checks/h_mutation.py").run(
        str(fixture_repo), "origin/main"
    )
    assert out.startswith("SKIPPED: no mutant was run"), out
    assert "pkg tests exit 1 with no mutant" in out


@posix_only
def test_project_runner_is_used_for_mutants(fixture_repo, tmp_path, monkeypatch):
    """HELMSMAN_PYTEST_RUNNER replaces plain pytest here as it does in c_pytest."""
    runner = tmp_path / "runner.sh"
    runner.write_text("#!/bin/sh\nexit 0\n")
    runner.chmod(0o755)
    monkeypatch.setenv("HELMSMAN_PYTEST_RUNNER", str(runner))
    (fixture_repo / "pkg" / "mod.py").write_text(
        "def check_guard(n):\n    return n > 10\n"
    )
    commit_all(fixture_repo)
    out = load("h_mutation", "checks/h_mutation.py").run(
        str(fixture_repo), "origin/main"
    )
    assert "SURVIVED: pkg/mod.py:2" in out, out


def test_async_guards_are_mutated():
    mod = load("h_mutation", "checks/h_mutation.py")
    muts = mod._mutants("async def verify_token(n):\n    return n > 1\n")
    assert [label for _, _, label in muts] == ["> -> <= in verify_token"]


def test_mutant_is_not_started_when_less_time_is_left_than_the_baseline_took(
    fixture_repo, monkeypatch
):
    """A green mutant run takes as long as the baseline, so it must fit the budget left."""
    (fixture_repo / "pkg" / "mod.py").write_text(
        "def check_guard(n):\n    return n > 10\n"
    )
    commit_all(fixture_repo)
    mod = load("h_mutation", "checks/h_mutation.py")
    clock = [0.0]

    class FakeTime:
        @staticmethod
        def monotonic():
            return clock[0]

    def baseline_then_survivor(*a):
        clock[0] += 115
        return 0

    monkeypatch.setattr(mod, "time", FakeTime)
    monkeypatch.setattr(mod, "BUDGET_S", 220)
    monkeypatch.setattr(
        mod._env, "ensure_project_env", lambda worktree, timeout: (None, False, "")
    )
    monkeypatch.setattr(mod, "_run_pkg_tests", baseline_then_survivor)
    out = mod.run(str(fixture_repo), "origin/main")
    assert out.startswith("SKIPPED: no mutant was run"), out
    assert "check budget spent" in out


def test_a_timed_out_baseline_is_not_described_as_a_failing_suite(
    fixture_repo, monkeypatch
):
    (fixture_repo / "pkg" / "mod.py").write_text(
        "def check_guard(n):\n    return n > 10\n"
    )
    commit_all(fixture_repo)
    mod = load("h_mutation", "checks/h_mutation.py")
    monkeypatch.setattr(mod, "_run_pkg_tests", lambda *a: -1)
    out = mod.run(str(fixture_repo), "origin/main")
    assert "pkg tests did not finish within" in out, out
    assert "exit -1" not in out


def test_stop_handler_ignores_a_repeat_stop_during_restore():
    """Once the stop handler fires, a second stop signal or SIGINT cannot interrupt the restore."""
    mod = load("h_mutation", "checks/h_mutation.py")
    saved = {s: signal.getsignal(s) for s in mod.STOP_SIGNALS}
    try:
        with pytest.raises(mod._Terminated):
            mod._raise_terminated(mod.STOP_SIGNALS[0], None)
        assert all(signal.getsignal(s) == signal.SIG_IGN for s in mod.STOP_SIGNALS)
    finally:
        for s, handler in saved.items():
            signal.signal(s, handler)


@pytest.mark.parametrize("sigmask", [True, False], ids=["sigmask", "no-sigmask"])
def test_stop_arriving_mid_restore_still_leaves_the_file_whole(
    fixture_repo, monkeypatch, sigmask
):
    """A first stop landing during the write-back is held until the original bytes are on disk, with or without a signal mask (Windows)."""
    if sigmask and not hasattr(signal, "pthread_sigmask"):
        pytest.skip("no signal mask on this platform")
    if not sigmask:
        monkeypatch.delattr(signal, "pthread_sigmask", raising=False)
    mod_path = fixture_repo / "pkg" / "mod.py"
    mod_path.write_text("def check_guard(n):\n    return n > 10\n")
    commit_all(fixture_repo)
    original = mod_path.read_bytes()
    mod = load("h_mutation", "checks/h_mutation.py")
    monkeypatch.setattr(mod, "_run_pkg_tests", lambda *a: 0)
    real_write = Path.write_bytes

    def write_then_stopped(self, data):
        if data == original:
            signal.raise_signal(mod.STOP_SIGNALS[0])
        return real_write(self, data)

    monkeypatch.setattr(Path, "write_bytes", write_then_stopped)
    saved = {s: signal.getsignal(s) for s in mod.STOP_SIGNALS}
    try:
        out = mod.run(str(fixture_repo), "origin/main")
    finally:
        for s, handler in saved.items():
            signal.signal(s, handler)
    assert mod_path.read_bytes() == original
    assert "stopped early" in out
