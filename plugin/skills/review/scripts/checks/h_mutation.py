import ast
import re
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import _diff  # noqa: E402
import _env  # noqa: E402

GUARD = re.compile(
    r"guard|check|verify|validate|enforce|policy|mask|redact|anonym", re.I
)
OPS = {
    ast.Gt: (">", "<="),
    ast.Lt: ("<", ">="),
    ast.GtE: (">=", "<"),
    ast.LtE: ("<=", ">"),
    ast.Eq: ("==", "!="),
    ast.NotEq: ("!=", "=="),
}
MAX_MUTANTS = 10
# phase0 SIGTERMs a check at HELMSMAN_PHASE0_CHECK_S; the env sync and every test run must fit under it
BUDGET_S = _diff.CHECK_BUDGET_S
PER_MUTANT_S = 120
MIN_RUN_S = 20
STOP_SIGNALS = (_diff.STOP_SIGNAL, signal.SIGINT)


class _Terminated(Exception):
    pass


def _raise_terminated(signum, frame):
    for s in STOP_SIGNALS:
        signal.signal(s, signal.SIG_IGN)  # a repeat stop must not interrupt the restore
    raise _Terminated(f"signal {signum} received, restored and exiting")


def _restore(path, data):
    if not hasattr(signal, "pthread_sigmask"):
        _restore_deferring_stops(path, data)
        return
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, set(STOP_SIGNALS))
    try:
        path.write_bytes(data)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def _restore_deferring_stops(path, data):
    """Windows has no signal mask, so a stop landing mid-write is held and replayed after it."""
    held = []
    previous = {s: signal.signal(s, lambda n, _f: held.append(n)) for s in STOP_SIGNALS}
    try:
        path.write_bytes(data)
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)
    if held and callable(previous[held[0]]):
        previous[held[0]](held[0], None)


def _install_signal_handlers():
    try:
        for s in STOP_SIGNALS:
            signal.signal(s, _raise_terminated)
    except ValueError:
        pass


def _mutants(src):
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    out = []
    for fn in [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and GUARD.search(n.name)
    ]:
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Compare)
                and len(node.ops) == 1
                and type(node.ops[0]) in OPS
                and node.lineno == node.end_lineno
            ):
                cur, flipped = OPS[type(node.ops[0])]
                line = lines[node.lineno - 1]
                left_end = node.left.end_col_offset
                right_start = node.comparators[0].col_offset
                opseg = line[left_end:right_start]
                if cur not in opseg:
                    continue
                new_line = (
                    line[:left_end]
                    + opseg.replace(cur, flipped, 1)
                    + line[right_start:]
                )
                out.append((node.lineno, new_line, f"{cur} -> {flipped} in {fn.name}"))
    return out


def _run_pkg_tests(d, worktree, pkg_dir, python=None, timeout=PER_MUTANT_S):
    for tool in [
        *_env.project_runner_commands(worktree, pkg_dir),
        *_env.pytest_commands(worktree, pkg_dir, python),
    ]:
        code, out = d.run_cmd(
            [*tool, str(pkg_dir), "-q", "-x"], cwd=worktree, timeout=timeout
        )
        if not _env.runner_unavailable(code, out):
            return code
    return -2


def run(worktree, base):
    _install_signal_handlers()
    files = _diff.changed_py_files(worktree, base)
    if not files:
        return "OK: no python changes"
    prod_files = [f for f in files if not _diff.is_test_path(f)]
    if not prod_files:
        return "OK: no production files changed (only test files) - nothing to probe"
    start = time.monotonic()
    survived, uncovered, inconclusive, no_pkg, tried = [], [], [], [], 0
    python, synced, stopped = None, False, ""
    baselines, baseline_s, red = {}, {}, []
    try:
        for f in prod_files:
            path = Path(worktree, f)
            orig_bytes = path.read_bytes()
            src = orig_bytes.decode("utf-8", errors="ignore")
            try:
                muts = _mutants(src)
            except SyntaxError:
                continue
            pkg_dir = _diff.owning_pkg(Path(worktree), path)
            if muts and pkg_dir is None:
                no_pkg.append(f)
                continue
            for lineno, new_line, label in muts:
                if tried >= MAX_MUTANTS:
                    break
                if not synced:
                    python, _, _ = _env.ensure_project_env(
                        worktree, timeout=min(_env.SYNC_TIMEOUT_S, BUDGET_S - MIN_RUN_S)
                    )
                    synced = True
                left = min(PER_MUTANT_S, BUDGET_S - (time.monotonic() - start))
                if left < max(MIN_RUN_S, baseline_s.get(pkg_dir, 0)):
                    stopped = "check budget spent"
                    break
                if pkg_dir not in baselines:
                    t0 = time.monotonic()
                    baselines[pkg_dir] = _run_pkg_tests(
                        _diff, worktree, pkg_dir, python, left
                    )
                    baseline_s[pkg_dir] = time.monotonic() - t0
                    if baselines[pkg_dir] == -2:
                        return "SKIPPED: pytest unavailable for mutation probe"
                    if baselines[pkg_dir] not in (0, 5):
                        outcome = (
                            f"did not finish within {left:.0f}s"
                            if baselines[pkg_dir] == -1
                            else f"exit {baselines[pkg_dir]}"
                        )
                        red.append(
                            f"\n- NOTE: {pkg_dir.relative_to(worktree)} tests {outcome} "
                            "with no mutant, so a run under a mutant says nothing - "
                            "its guards were not probed"
                        )
                    left = min(PER_MUTANT_S, BUDGET_S - (time.monotonic() - start))
                    if left < max(MIN_RUN_S, baseline_s[pkg_dir]):
                        stopped = "check budget spent"
                        break
                if baselines[pkg_dir] not in (0, 5):
                    break
                lines = src.splitlines(keepends=True)
                lines[lineno - 1] = new_line
                try:
                    path.write_bytes("".join(lines).encode("utf-8"))
                    code = _run_pkg_tests(_diff, worktree, pkg_dir, python, left)
                finally:
                    _restore(path, orig_bytes)
                tried += 1
                if code == 0:
                    survived.append(
                        f"- SURVIVED: {f}:{lineno} ({label}) - tests stay green under this mutant"
                    )
                elif code == -2:
                    return "SKIPPED: pytest unavailable for mutation probe"
                elif code == 5:
                    uncovered.append(
                        f"- UNCOVERED: {f}:{lineno} ({label}) - no tests collected for this guard"
                    )
                elif code != 1:
                    inconclusive.append(
                        f"- INCONCLUSIVE: {f}:{lineno} ({label}) - test run exited {code}, mutant not evaluated"
                    )
            if stopped:
                break
    except _Terminated as e:
        stopped = str(e)
    notes = "".join(
        red
        + [
            f"\n- NOTE: stopped early ({stopped}), results are partial"
            if stopped
            else ""
        ]
        + [
            f"\n- NOTE: {f} has no owning package (pyproject.toml), so no test dir to run - not probed"
            for f in no_pkg
        ]
    )
    if not tried:
        if no_pkg or stopped or red:
            return "SKIPPED: no mutant was run" + notes
        return "OK: no guard-like changed functions to probe"
    if survived or uncovered or inconclusive:
        return (
            f"FINDINGS: {len(survived)}/{tried} mutants survived, "
            f"{len(uncovered)}/{tried} uncovered by any test, "
            f"{len(inconclusive)}/{tried} inconclusive\n"
            + "\n".join(survived + uncovered + inconclusive)
            + notes
        )
    if stopped:
        return f"SKIPPED: only {tried} mutant(s) ran, all killed by tests" + notes
    return f"OK: all {tried} probed mutants killed by tests" + notes


if __name__ == "__main__":
    try:
        print(run(sys.argv[1], sys.argv[2]))
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
    sys.exit(0)
