import importlib.util
import os
import signal
import shutil
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from .conftest import (
    NEW_GROUP,
    SCRIPTS,
    STOP_SIGNAL,
    commit_all,
    gone_within,
    posix_only,
    stop,
)

if os.name == "nt":
    import msvcrt
else:
    import fcntl

needs_pytest = pytest.mark.skipif(
    not (shutil.which("pytest") or shutil.which("uv")), reason="no pytest on PATH"
)


def load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / rel)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def fake_check(fn):
    return type("M", (), {"run": staticmethod(fn)})


def test_sections_written_and_broken_check_degrades(fixture_repo, tmp_path):
    phase0 = load("phase0", "phase0.py")
    out = tmp_path / "FACTS.md"
    setattr(
        phase0,
        "CHECKS",
        [
            ("okcheck", fake_check(lambda w, b: "OK: fine")),
            (
                "badcheck",
                fake_check(lambda w, b: (_ for _ in ()).throw(RuntimeError("boom"))),
            ),
        ],
    )
    assert phase0.main([str(fixture_repo), str(out)]) == 0
    text = out.read_text()
    assert "## okcheck" in text and "OK: fine" in text
    assert "## badcheck" in text and "ERROR: check crashed" in text


def test_slow_check_times_out_and_facts_still_written(fixture_repo, tmp_path):
    phase0 = load("phase0", "phase0.py")
    out = tmp_path / "FACTS.md"
    setattr(phase0, "PARALLEL_BUDGET_S", 1)
    setattr(
        phase0,
        "CHECKS",
        [
            ("slow", fake_check(lambda w, b: (time.sleep(3), "OK: late")[1])),
            ("fast", fake_check(lambda w, b: "OK: fast")),
        ],
    )
    assert phase0.main([str(fixture_repo), str(out)]) == 0
    text = out.read_text()
    assert "## slow" in text and "SKIPPED: check timed out" in text
    assert "OK: fast" in text


def test_unresolvable_merge_base_writes_honest_facts(tmp_path):
    phase0 = load("phase0", "phase0.py")
    repo = tmp_path / "norepo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=repo, capture_output=True, check=True
    )
    out = tmp_path / "FACTS.md"
    assert phase0.main([str(repo), str(out)]) == 0
    assert "base unresolved" in out.read_text()


def test_cli_runs_end_to_end(fixture_repo, tmp_path):
    out = tmp_path / "FACTS.md"
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "phase0.py"), str(fixture_repo), str(out)],
        capture_output=True,
        encoding="utf-8",
        timeout=600,
    )
    assert r.returncode == 0
    assert out.read_text().startswith("# Phase 0 facts")


def test_phase0_guard_preserves_successful_facts(tmp_path, monkeypatch):
    guard = load("run_phase0_success", "run_phase0.py")
    child = tmp_path / "success.py"
    child.write_text(
        "import pathlib, sys\npathlib.Path(sys.argv[2]).write_text('# facts\\n')\n"
    )
    monkeypatch.setattr(guard, "PHASE0", child)
    out = tmp_path / "FACTS.md"
    assert guard.main([str(tmp_path), str(out)]) == 0
    assert out.read_text() == "# facts\n"


@posix_only
def test_phase0_guard_records_child_signal_without_facts(tmp_path, monkeypatch):
    guard = load("run_phase0_signal", "run_phase0.py")
    child = tmp_path / "terminated.py"
    child.write_text(
        "import os, signal, sys\n"
        "print('progress one', flush=True)\n"
        "print('progress two', flush=True)\n"
        "print('progress three', flush=True)\n"
        "print('child failed before facts', file=sys.stderr, flush=True)\n"
        "os.kill(os.getpid(), signal.SIGTERM)\n"
    )
    monkeypatch.setattr(guard, "PHASE0", child)
    out = tmp_path / "FACTS.md"
    assert guard.main([str(tmp_path), str(out)]) == 128 + signal.SIGTERM
    text = out.read_text()
    assert "Phase 0 unavailable" in text
    assert "signal 15 (SIGTERM)" in text
    assert "child failed before facts" in text
    assert "progress three" in text


@posix_only
def test_phase0_guard_normalizes_signal_after_facts(tmp_path, monkeypatch):
    guard = load("run_phase0_signal_after_facts", "run_phase0.py")
    child = tmp_path / "terminated_after_facts.py"
    child.write_text(
        "import os, pathlib, signal, sys\n"
        "pathlib.Path(sys.argv[2]).write_text('# facts before signal\\n')\n"
        "os.kill(os.getpid(), signal.SIGTERM)\n"
    )
    monkeypatch.setattr(guard, "PHASE0", child)
    out = tmp_path / "FACTS.md"
    assert guard.main([str(tmp_path), str(out)]) == 128 + signal.SIGTERM
    assert out.read_text() == "# facts before signal\n"


def test_phase0_guard_streams_and_stops_child_when_signalled(tmp_path):
    guard = tmp_path / "run_phase0.py"
    shutil.copy(SCRIPTS / "run_phase0.py", guard)
    child = tmp_path / "phase0.py"
    child_pid = tmp_path / "child.pid"
    child.write_text(
        "import os, pathlib, sys, time\n"
        f"pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid()))\n"
        "print('phase0 progress', flush=True)\n"
        "time.sleep(30)\n"
    )
    out = tmp_path / "FACTS.md"
    proc = subprocess.Popen(
        [sys.executable, str(guard), str(tmp_path), str(out)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        **NEW_GROUP,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline() == "phase0 progress\n"
        pid = int(child_pid.read_text())
        stop(proc)
        stdout, stderr = proc.communicate(timeout=10)
        assert proc.returncode == 128 + STOP_SIGNAL
        assert stdout == ""
        name = signal.Signals(STOP_SIGNAL).name
        assert f"guard received signal {int(STOP_SIGNAL)} ({name})" in stderr
        assert "phase0 progress" in out.read_text()
        assert gone_within(pid, 5), "Phase 0 child survived guard termination"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_run_subprocess_check_uses_graceful_sigterm_output(tmp_path, monkeypatch):
    phase0 = load("phase0", "phase0.py")
    checks_dir = tmp_path / "checks"
    checks_dir.mkdir()
    (checks_dir / "slowgraceful.py").write_text(
        "import signal, sys, time\n\n\n"
        "def _h(signum, frame):\n"
        "    print('SKIPPED: graceful shutdown')\n"
        "    sys.exit(0)\n\n\n"
        f"signal.signal({int(STOP_SIGNAL)}, _h)\n"
        "for _ in range(300):\n"
        "    time.sleep(0.1)\n"
    )
    monkeypatch.setattr(phase0, "HERE", tmp_path)
    monkeypatch.setattr(phase0, "PER_CHECK_S", 1)
    monkeypatch.setattr(phase0, "GRACE_S", 5)
    start = time.monotonic()
    out = phase0._run_subprocess_check("slowgraceful", str(tmp_path), "origin/main")
    elapsed = time.monotonic() - start
    assert out == "SKIPPED: graceful shutdown"
    assert elapsed < 4


def test_run_subprocess_check_grace_period_then_sigkills_unresponsive(
    tmp_path, monkeypatch
):
    phase0 = load("phase0", "phase0.py")
    checks_dir = tmp_path / "checks"
    checks_dir.mkdir()
    (checks_dir / "stubborn.py").write_text(
        f"import signal, time\n\nsignal.signal({int(STOP_SIGNAL)}, signal.SIG_IGN)\ntime.sleep(30)\n"
    )
    monkeypatch.setattr(phase0, "HERE", tmp_path)
    monkeypatch.setattr(phase0, "PER_CHECK_S", 1)
    monkeypatch.setattr(phase0, "GRACE_S", 2)
    start = time.monotonic()
    out = phase0._run_subprocess_check("stubborn", str(tmp_path), "origin/main")
    elapsed = time.monotonic() - start
    assert out == "SKIPPED: check timed out"
    assert elapsed >= 2.5
    assert elapsed < 10


def test_repair_worktree_restores_dirty_tracked_files(fixture_repo):
    phase0 = load("phase0", "phase0.py")
    target = fixture_repo / "pkg" / "mod.py"
    original = target.read_text()
    target.write_text(original + "\n# corrupted\n")
    dirty, held, error = phase0._repair_worktree(str(fixture_repo), set())
    assert dirty == ["pkg/mod.py"]
    assert error is None
    assert target.read_text() == original


def test_main_repairs_dirty_worktree_and_records_it_in_facts(fixture_repo, tmp_path):
    phase0 = load("phase0", "phase0.py")
    out = tmp_path / "FACTS.md"
    target = fixture_repo / "pkg" / "mod.py"
    original = target.read_text()

    def _corrupt_and_abandon(w, b):
        target.write_text(original + "\n# abandoned\n")
        return "OK: fine"

    setattr(phase0, "CHECKS", [("okcheck", fake_check(_corrupt_and_abandon))])
    assert phase0.main([str(fixture_repo), str(out)]) == 0
    text = out.read_text()
    assert "## _worktree_repair" in text
    assert "SKIPPED" in text.split("## _worktree_repair")[1]
    assert target.read_text() == original


def test_budget_constants_stay_inside_the_poll_window():
    phase0 = load("phase0", "phase0.py")
    assert phase0.PARALLEL_BUDGET_S + phase0.SERIAL_BUDGET_S + phase0.LOCK_WAIT_S < 600
    assert phase0.PARALLEL_BUDGET_S >= phase0.PER_CHECK_S + phase0.GRACE_S
    assert phase0.SERIAL_BUDGET_S >= phase0.PER_CHECK_S + phase0.GRACE_S


def test_main_with_missing_args_does_not_crash(tmp_path):
    phase0 = load("phase0", "phase0.py")
    assert phase0.main([]) == 2
    assert phase0.main(["only-worktree"]) == 2


def test_discovery_excludes_underscore_prefixed_helpers(
    fixture_repo, tmp_path, monkeypatch
):
    phase0 = load("phase0", "phase0.py")
    checks_dir = tmp_path / "checks"
    checks_dir.mkdir()
    helper_file = checks_dir / "_helper.py"
    real_file = checks_dir / "real.py"
    helper_file.write_text(
        "def run(w, b):\n    return 'OK: helper'\nif __name__ == '__main__':\n    import sys\n    print(run(sys.argv[1], sys.argv[2]))\n"
    )
    real_file.write_text(
        "def run(w, b):\n    return 'OK: real'\nif __name__ == '__main__':\n    import sys\n    print(run(sys.argv[1], sys.argv[2]))\n"
    )
    monkeypatch.setattr(phase0, "HERE", tmp_path)
    out = tmp_path / "FACTS.md"
    setattr(phase0, "CHECKS", None)
    assert phase0.main([str(fixture_repo), str(out)]) == 0
    text = out.read_text()
    assert "## real" in text and "OK: real" in text
    assert "## _helper" not in text


def test_run_cmd_survives_output_that_is_not_utf8(tmp_path):
    """A byte no codec accepts is a fact about the repo, never a crashed check."""
    d = load("_diff", "checks/_diff.py")
    script = tmp_path / "emit.py"
    script.write_text(
        "import sys\nsys.stdout.buffer.write(b'ok \\x9f tail\\n')\n",
    )
    code, out = d.run_cmd([sys.executable, str(script)], str(tmp_path), 30)
    assert code == 0 and "ok" in out and "tail" in out


def test_no_check_answers_a_broken_base_with_a_traceback(tmp_path):
    """Phase 0 reads stdout: a check that dies on stderr reports nothing at all."""
    checks = sorted((SCRIPTS / "checks").glob("[a-l]_*.py"))
    assert len(checks) == 12
    prefixes = ("OK:", "FINDINGS:", "SKIPPED:", "ERROR:")
    crashed = []
    for check in checks:
        r = subprocess.run(
            [sys.executable, str(check), str(tmp_path), "not-a-ref"],
            capture_output=True,
            encoding="utf-8",
            timeout=120,
        )
        assert r.returncode == 0, (check.name, r.stderr[-200:])
        assert r.stdout.startswith(prefixes), (check.name, r.stdout[:120])
        if r.stdout.startswith("ERROR:"):
            crashed.append(check.name)
    assert "k_claims.py" in crashed


def test_crashed_check_carries_the_exception_not_the_traceback_header(
    fixture_repo, tmp_path, monkeypatch
):
    """The first 200 chars of a traceback name no cause; the last lines do."""
    phase0 = load("phase0", "phase0.py")
    fake_root = tmp_path / "scripts"
    (fake_root / "checks").mkdir(parents=True)
    (fake_root / "checks" / "boom.py").write_text(
        "import sys\nraise RuntimeError('the actual cause')\n"
    )
    monkeypatch.setattr(phase0, "HERE", fake_root)
    out = phase0._run_subprocess_check("boom", str(fixture_repo), "origin/main")
    assert out.startswith("ERROR: check crashed")
    assert "the actual cause" in out


def test_repair_never_reverts_work_that_was_open_before_the_run(fixture_repo, tmp_path):
    """git checkout -- is unrecoverable, so uncommitted work outranks a clean run."""
    phase0 = load("phase0", "phase0.py")
    out = tmp_path / "FACTS.md"
    target = fixture_repo / "pkg" / "mod.py"
    mine = target.read_text() + "\n# work in progress\n"
    target.write_text(mine)

    def _also_dirty_it(w, b):
        target.write_text(mine + "# a check scribbled here\n")
        return "OK: fine"

    setattr(phase0, "CHECKS", [("okcheck", fake_check(_also_dirty_it))])
    assert phase0.main([str(fixture_repo), str(out)]) == 0
    assert target.read_text().startswith(mine)
    assert "were already modified when Phase 0 started" in out.read_text()


def test_a_check_killed_on_its_own_budget_reads_as_a_timeout_not_a_crash(
    fixture_repo, tmp_path, monkeypatch
):
    """SIGTERM leaves returncode -15, which the crash branch would otherwise claim."""
    phase0 = load("phase0", "phase0.py")
    fake_root = tmp_path / "scripts"
    (fake_root / "checks").mkdir(parents=True)
    (fake_root / "checks" / "slow.py").write_text("import time\ntime.sleep(30)\n")
    monkeypatch.setattr(phase0, "HERE", fake_root)
    monkeypatch.setattr(phase0, "PER_CHECK_S", 1)
    out = phase0._run_subprocess_check("slow", str(fixture_repo), "origin/main")
    assert out == "SKIPPED: check timed out"
    assert "crashed" not in out


def test_repair_handles_spaced_paths_and_renames(fixture_repo):
    """Porcelain paths with spaces and both sides of a rename are restored to HEAD."""
    phase0 = load("phase0", "phase0.py")
    spaced = fixture_repo / "pkg" / "a b.py"
    spaced.write_text("x = 1\n")
    commit_all(fixture_repo, "spaced")
    plain = fixture_repo / "pkg" / "mod.py"
    original = plain.read_text()
    spaced.write_text("x = 2\n")
    plain.write_text(original + "# dirty\n")
    subprocess.run(
        ["git", "mv", "pkg/__init__.py", "pkg/moved.py"],
        cwd=fixture_repo,
        check=True,
        capture_output=True,
    )
    assert phase0._dirty(str(fixture_repo)) == {
        "pkg/a b.py",
        "pkg/mod.py",
        "pkg/__init__.py",
        "pkg/moved.py",
    }
    dirty, held, error = phase0._repair_worktree(str(fixture_repo), set())
    assert error is None
    assert len(dirty) == 4 and held == []
    assert spaced.read_text() == "x = 1\n"
    assert plain.read_text() == original
    assert (fixture_repo / "pkg" / "__init__.py").exists()
    assert not (fixture_repo / "pkg" / "moved.py").exists()
    assert phase0._dirty(str(fixture_repo)) == set()


def test_repair_treats_glob_characters_literally(fixture_repo):
    """A dirtied path like `[ab].py` must not restore a held `a.py` it would match as a glob."""
    phase0 = load("phase0", "phase0.py")
    held_file = fixture_repo / "a.py"
    glob_file = fixture_repo / "[ab].py"
    held_file.write_text("x = 1\n")
    glob_file.write_text("y = 1\n")
    commit_all(fixture_repo, "glob names")
    held_file.write_text("x = 2\n")
    preexisting = phase0._dirty(str(fixture_repo))
    glob_file.write_text("y = 2\n")
    dirty, held, error = phase0._repair_worktree(str(fixture_repo), preexisting)
    assert (dirty, held, error) == (["[ab].py"], ["a.py"], None)
    assert held_file.read_text() == "x = 2\n"
    assert glob_file.read_text() == "y = 1\n"


def test_failed_restore_is_an_error_section_not_a_claimed_restore(
    fixture_repo, tmp_path, monkeypatch
):
    """A restore that fails must never be reported as restored to HEAD."""
    phase0 = load("phase0", "phase0.py")
    out = tmp_path / "FACTS.md"
    setattr(phase0, "CHECKS", [("okcheck", fake_check(lambda w, b: "OK: fine"))])
    monkeypatch.setattr(
        phase0,
        "_repair_worktree",
        lambda w, p: (["pkg/a b.py"], [], "exit 128: fatal: boom"),
    )
    assert phase0.main([str(fixture_repo), str(out)]) == 0
    section = out.read_text().split("## _worktree_repair\n")[1]
    assert section.startswith("ERROR: worktree restore failed (exit 128: fatal: boom)")
    assert "pkg/a b.py" in section
    assert "restored to HEAD" not in section


def test_base_ref_argument_is_used_and_an_unknown_one_is_unresolved(
    fixture_repo, tmp_path
):
    """An explicit base ref is honoured; without one the remote default is tried."""
    phase0 = load("phase0", "phase0.py")
    setattr(phase0, "CHECKS", [("okcheck", fake_check(lambda w, b: "OK: fine"))])
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/release", "HEAD"],
        cwd=fixture_repo,
        check=True,
    )
    out = tmp_path / "FACTS.md"
    assert phase0.main([str(fixture_repo), str(out), "origin/release"]) == 0
    assert "from origin/release)" in out.read_text().splitlines()[0]
    assert phase0.main([str(fixture_repo), str(out)]) == 0
    assert "from origin/main)" in out.read_text().splitlines()[0]
    assert phase0.main([str(fixture_repo), str(out), "origin/nope"]) == 0
    assert "merge-base vs origin/nope" in out.read_text()


def test_timed_out_check_takes_its_tool_process_group_down(tmp_path, monkeypatch):
    """A check killed on its budget must not leave the tool it started running."""
    phase0 = load("phase0", "phase0.py")
    checks_dir = tmp_path / "checks"
    checks_dir.mkdir()
    shutil.copy(SCRIPTS / "checks" / "_diff.py", checks_dir / "_diff.py")
    tool_pid = tmp_path / "tool.pid"
    tool = (
        f"import os, pathlib, time; "
        f"pathlib.Path({str(tool_pid)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    (checks_dir / "hangs.py").write_text(
        "import sys\n"
        "import _diff\n"
        f"_diff.run_cmd([{sys.executable!r}, '-c', {tool!r}], cwd='.', timeout=60)\n"
    )
    monkeypatch.setattr(phase0, "HERE", tmp_path)
    monkeypatch.setattr(phase0, "PER_CHECK_S", 5)
    assert phase0._run_subprocess_check("hangs", str(tmp_path), "HEAD") == (
        "SKIPPED: check timed out"
    )
    assert gone_within(int(tool_pid.read_text()), 5), (
        "tool outlived its timed-out check"
    )


def test_skip_env_disables_named_checks_without_running_them(
    fixture_repo, tmp_path, monkeypatch
):
    """A project whose tests run in CI only names the checks Phase 0 must not run."""
    phase0 = load("phase0", "phase0.py")
    out = tmp_path / "FACTS.md"
    ran = []
    setattr(
        phase0,
        "CHECKS",
        [
            ("c_pytest", fake_check(lambda w, b: ran.append("c_pytest") or "OK: ran")),
            ("a_ruff", fake_check(lambda w, b: "OK: lint")),
        ],
    )
    monkeypatch.setenv("HELMSMAN_PHASE0_SKIP", "c_pytest, h_mutation")
    assert phase0.main([str(fixture_repo), str(out)]) == 0
    text = out.read_text()
    assert "## c_pytest\nSKIPPED: disabled by HELMSMAN_PHASE0_SKIP" in text
    assert "OK: lint" in text
    assert ran == []


def test_held_lock_skips_heavy_checks_and_never_delays_fast_ones(
    fixture_repo, tmp_path
):
    """Only pytest and mutation queue on the machine-wide lock."""
    phase0 = load("phase0", "phase0.py")
    out = tmp_path / "FACTS.md"
    lock = tmp_path / "phase0.lock"
    setattr(phase0, "LOCK_PATH", lock)
    setattr(phase0, "LOCK_WAIT_S", 0)
    setattr(
        phase0,
        "CHECKS",
        [
            ("c_pytest", fake_check(lambda w, b: "OK: tests")),
            ("b_pyright", fake_check(lambda w, b: "OK: types")),
            ("a_ruff", fake_check(lambda w, b: "OK: lint")),
        ],
    )
    with open(lock, "a") as held:
        _lock(held)
        assert phase0.main([str(fixture_repo), str(out)]) == 0
    text = out.read_text()
    assert text.split("## c_pytest\n")[1].startswith(
        "SKIPPED: another Phase 0 run held"
    )
    assert "OK: types" in text
    assert "OK: lint" in text
    assert phase0.main([str(fixture_repo), str(out)]) == 0
    assert "OK: tests" in out.read_text()


def test_mutation_waits_for_fast_checks(fixture_repo, tmp_path):
    """Mutation rewrites source files, so it never starts while a fast check still reads them."""
    phase0 = load("phase0", "phase0.py")
    out = tmp_path / "FACTS.md"
    events = []

    def slow_fast(w, b):
        time.sleep(0.5)
        events.append("fast done")
        return "OK: lint"

    def mutation(w, b):
        events.append("mutation start")
        return "OK: mutants"

    setattr(
        phase0,
        "CHECKS",
        [("a_ruff", fake_check(slow_fast)), ("h_mutation", fake_check(mutation))],
    )
    assert phase0.main([str(fixture_repo), str(out)]) == 0
    assert events == ["fast done", "mutation start"]


def test_unreadable_start_state_restores_nothing(fixture_repo, tmp_path, monkeypatch):
    """Without the start state, a restore could revert the operator's own edits."""
    phase0 = load("phase0", "phase0.py")
    out = tmp_path / "FACTS.md"
    target = fixture_repo / "pkg" / "mod.py"
    mine = target.read_text() + "\n# work in progress\n"
    target.write_text(mine)
    real_dirty = phase0._dirty
    calls = []

    def first_status_fails(worktree):
        calls.append(worktree)
        return None if len(calls) == 1 else real_dirty(worktree)

    monkeypatch.setattr(phase0, "_dirty", first_status_fails)
    setattr(phase0, "CHECKS", [("okcheck", fake_check(lambda w, b: "OK: fine"))])
    assert phase0.main([str(fixture_repo), str(out)]) == 0
    assert target.read_text() == mine
    repair = out.read_text().split("## _worktree_repair")[1]
    assert repair.lstrip().startswith("ERROR:") and "nothing was restored" in repair


def test_phase0_guard_drops_a_stale_facts_file(tmp_path, monkeypatch):
    """A re-run into the same outdir must never report the previous run's facts."""
    guard = load("run_phase0_stale", "run_phase0.py")
    child = tmp_path / "phase0.py"
    child.write_text("import sys\nsys.exit(3)\n")
    monkeypatch.setattr(guard, "PHASE0", child)
    out = tmp_path / "FACTS.md"
    out.write_text("# Phase 0 facts from the previous run\n")
    assert guard.main([str(tmp_path), str(out)]) == 3
    assert out.read_text().startswith("Phase 0 unavailable: child ended with exit 3")


def _mutation_only_guard(fixture_repo, tmp_path):
    phase0 = load("phase0", "phase0.py")
    skip = ",".join(n for n in phase0._discover_names() if n != "h_mutation")
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
    # a Windows venv python.exe is a launcher whose job takes the whole tree down when it is killed
    python = sys._base_executable if os.name == "nt" else sys.executable
    proc = subprocess.Popen(
        [
            python,
            str(SCRIPTS / "run_phase0.py"),
            str(fixture_repo),
            str(tmp_path / "FACTS.md"),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "HELMSMAN_PHASE0_SKIP": skip},
        **NEW_GROUP,
    )
    deadline = time.monotonic() + 60
    while mod_path.read_bytes() == original:
        if proc.poll() is not None or time.monotonic() > deadline:
            proc.kill()
            raise AssertionError("the mutant was never written")
        time.sleep(0.1)
    return proc, mod_path, original


def _restored_within(mod_path, original, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if mod_path.read_bytes() == original:
            return True
        time.sleep(0.1)
    return False


@posix_only
@needs_pytest
def test_sighup_to_the_guard_restores_the_mutant(fixture_repo, tmp_path):
    """A hung-up session must not leave a flipped comparison in the source."""
    proc, mod_path, original = _mutation_only_guard(fixture_repo, tmp_path)
    try:
        proc.send_signal(signal.SIGHUP)
        assert proc.wait(timeout=20) == 128 + signal.SIGHUP
        assert _restored_within(mod_path, original, 5)
    finally:
        if proc.poll() is None:
            proc.kill()


@needs_pytest
def test_killed_guard_takes_phase0_down_and_restores_the_mutant(fixture_repo, tmp_path):
    """A SIGKILLed guard cannot forward a signal, so Phase 0 notices it is orphaned."""
    proc, mod_path, original = _mutation_only_guard(fixture_repo, tmp_path)
    proc.kill()
    proc.wait()
    assert _restored_within(mod_path, original, 15)


@posix_only
def test_stop_group_does_not_resend_a_forwarded_sigterm(monkeypatch):
    """After the guard forwarded a stop, cleanup only waits and kills; a second SIGTERM would interrupt the restore."""
    guard = load("run_phase0_stop_group", "run_phase0.py")
    sent = []
    monkeypatch.setattr(guard, "_signal_group", lambda pid, signum: sent.append(signum))

    def gone(pid, signum):
        raise ProcessLookupError

    monkeypatch.setattr(guard.os, "killpg", gone)
    guard._stop_group(12345, already_signalled=True)
    assert sent == []
    guard._stop_group(12345, already_signalled=False)
    assert sent == [signal.SIGTERM]


def _lock(handle):
    if os.name == "nt":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle):
    if os.name == "nt":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle, fcntl.LOCK_UN)


def _as_windows(monkeypatch, mod):
    """On POSIX, drive the module's Windows branch with an msvcrt stand-in built on flock."""
    if os.name == "nt":
        return

    def locking(fd, mode, nbytes):
        assert os.lseek(fd, 0, os.SEEK_CUR) == 0 and nbytes == 1
        if mode == 0:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PermissionError(13, "Permission denied") from None

    fake = SimpleNamespace(locking=locking, LK_UNLCK=0, LK_NBLCK=2)
    monkeypatch.setattr(mod, "msvcrt", fake, raising=False)
    monkeypatch.setattr(mod, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))


def test_windows_heavy_lock_locks_byte_zero_and_skips_while_held(tmp_path, monkeypatch):
    """The append-mode lock file opens at EOF, so a Windows run must seek to byte 0 before locking."""
    phase0 = load("phase0_nt", "phase0.py")
    lock = tmp_path / "phase0.lock"
    lock.write_text("left by an earlier run\n")
    _as_windows(monkeypatch, phase0)
    monkeypatch.setattr(phase0, "LOCK_PATH", lock)
    monkeypatch.setattr(phase0, "LOCK_WAIT_S", 0)
    with open(lock, "a") as held:
        _lock(held)
        with phase0._heavy_lock() as got:
            assert got is False
    with phase0._heavy_lock() as got:
        assert got is True


def test_windows_ledger_waits_for_a_held_lock_then_releases_it(tmp_path, monkeypatch):
    """msvcrt has no blocking lock without a 10 s give-up, so the ledger polls until the holder lets go."""
    ledger = load("ledger_nt", "ledger.py")
    _as_windows(monkeypatch, ledger)
    args = ["round-start", "--sha", "aaa", "--repo", "o/r", "--pr", "7"]
    assert ledger.main([*args, "--dir", str(tmp_path)]) == 0
    lock_file = next(tmp_path.rglob("*.lock"))
    with open(lock_file, "w") as held:
        _lock(held)
        threading.Timer(0.3, _unlock, (held,)).start()
        started = time.monotonic()
        assert ledger.main([*args, "--dir", str(tmp_path)]) == 0
        assert time.monotonic() - started >= 0.25
    with open(lock_file, "w") as probe:
        _lock(probe)
        _unlock(probe)


@needs_pytest
def test_stop_to_the_guard_restores_the_mutant(fixture_repo, tmp_path):
    """SIGTERM, or a console break on Windows, reaches h_mutation through the guard and Phase 0."""
    proc, mod_path, original = _mutation_only_guard(fixture_repo, tmp_path)
    try:
        stop(proc)
        assert proc.wait(timeout=30) == 128 + STOP_SIGNAL
        assert _restored_within(mod_path, original, 5)
    finally:
        if proc.poll() is None:
            proc.kill()
