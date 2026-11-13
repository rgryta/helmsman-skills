import os
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import _diff  # noqa: E402
import _env  # noqa: E402

# phase0 kills a check at HELMSMAN_PHASE0_CHECK_S; finishing first keeps the packages that already ran
BUDGET_S = int(os.environ.get("HELMSMAN_PHASE0_CHECK_S", "240")) - 20
PER_PKG_S = 150
MIN_RUN_S = 20
MAX_PKGS = 8
SCOPE_ENV = "HELMSMAN_PYTEST_SCOPE"
_SCOPE_NOTE = (
    "scope: only test files named after a changed module (test_<stem>.py / "
    "<stem>_test.py) plus changed test files themselves, inside the owning "
    "package - a package suite that would have failed elsewhere is not run here"
)
_GATE_NOTE = (
    "scope: gate mode - every test directory holding a test named after a changed "
    "module, so a sibling test broken in the same directory fails here; a package "
    "suite outside those directories is still not run"
)
_INFRA_MARKERS = (
    "DockerException",
    "Error while fetching server API version",
    "Cannot connect to the Docker daemon",
    "docker.errors",
)
_VENDORED_DIRS = frozenset(
    {".venv", "venv", "node_modules", "site-packages", ".tox", "build", "dist", ".git"}
)
INFRA_UNAVAILABLE = "test runner infra unavailable"
REFUSED_UNVERIFIED = "connection refused, infra or code unverified"


def _infra_kind(out):
    """A bare refusal can come from the code under test, so it never reads as runner infra."""
    if any(marker in out for marker in _INFRA_MARKERS):
        return INFRA_UNAVAILABLE
    if "Connection refused" in out:
        return REFUSED_UNVERIFIED
    return None


def _packages(worktree, files):
    root = Path(worktree)
    owners = (_diff.owning_pkg(root, Path(worktree, f)) for f in files)
    return sorted({p for p in owners if p is not None})


def _is_test_file(rel):
    name = Path(rel).name
    return name.startswith("test_") or name.endswith("_test.py")


def _mapped_tests(pkg, module):
    """Test files named after the module, searched within its own package."""
    stem = Path(module).stem
    found = set()
    for pattern in (f"test_{stem}.py", f"{stem}_test.py"):
        found.update(
            q
            for q in pkg.rglob(pattern)
            if q.is_file() and _VENDORED_DIRS.isdisjoint(q.relative_to(pkg).parts)
        )
    return found


def _gate_scope():
    return os.environ.get(SCOPE_ENV, "diff") == "gate"


def _widen(tests):
    """The directories holding the matched tests, minus any nested under another.

    A fold that breaks a sibling test in the same directory is the case the
    named-file selection cannot see, and it is the case a gate exists for.
    """
    dirs = {q.parent for q in tests}
    return sorted(d for d in dirs if not any(p in d.parents for p in dirs))


def _selection(worktree, files):
    """Packages most touched first, each with its tests, plus what nothing tests.

    Ordering is load-bearing: the run is capped, and the package a diff changes
    most is the one whose tests a reviewer needs, never the alphabetically first.
    """
    root = Path(worktree)
    by_pkg, touched, unmapped = {}, {}, []
    for f in files:
        path = Path(worktree, f)
        pkg = _diff.owning_pkg(root, path)
        if pkg is None:
            continue
        touched[pkg] = touched.get(pkg, 0) + 1
        if _is_test_file(f):
            if path.exists():
                by_pkg.setdefault(pkg, set()).add(path)
            continue
        mapped = _mapped_tests(pkg, f)
        if mapped:
            by_pkg.setdefault(pkg, set()).update(mapped)
        else:
            unmapped.append(f)
    order = sorted(by_pkg, key=lambda k: (-touched[k], str(k)))
    widen = _widen if _gate_scope() else sorted
    return {k: widen(by_pkg[k]) for k in order}, unmapped


def run(worktree, base):
    files = _diff.changed_py_files(worktree, base)
    if not files:
        return "OK: no python changes"
    if not _packages(worktree, files):
        return "SKIPPED: no owning package (pyproject.toml) found for changed files"
    by_pkg, unmapped = _selection(worktree, files)
    gap = (
        "\n- NOT COVERED: no test file named for "
        + ", ".join(sorted(unmapped)[:8])
        + (" (+%d more)" % (len(unmapped) - 8) if len(unmapped) > 8 else "")
        if unmapped
        else ""
    )
    if not by_pkg:
        return (
            "SKIPPED: no test file maps to any changed module, so this diff's "
            f"behaviour is unverified here{gap}\n"
            f"{_GATE_NOTE if _gate_scope() else _SCOPE_NOTE}"
        )
    started = time.monotonic()
    python, _, env = _env.ensure_project_env(
        worktree, timeout=min(_env.SYNC_TIMEOUT_S, BUDGET_S - MIN_RUN_S)
    )
    results, missing, last = [], 0, ""
    for pkg, tests in list(by_pkg.items())[:MAX_PKGS]:
        if BUDGET_S - (time.monotonic() - started) < MIN_RUN_S:
            results.append(_skipped(pkg, "check budget spent before it ran"))
            continue
        rel = [str(q.relative_to(worktree)) for q in tests]
        runners = _env.project_runner_commands(worktree, pkg)
        for tool in [*runners, *_env.pytest_commands(worktree, pkg, python)]:
            left = min(PER_PKG_S, BUDGET_S - (time.monotonic() - started))
            if left < MIN_RUN_S:
                results.append(_skipped(pkg, "check budget spent before it ran"))
                break
            code, out = _diff.run_cmd(
                [*tool, *rel, "-q", "-x"], cwd=worktree, timeout=left
            )
            if _env.runner_unavailable(code, out):
                last = out
                continue
            tail = "\n".join(out.splitlines()[-6:])
            unit = "test dir(s), gate scope" if _gate_scope() else "test file(s)"
            via = f", via {_env.RUNNER_ENV}" if tool in runners else ""
            label = f"{pkg} ({len(rel)} {unit}{via})"
            if code == -1:
                results.append(_skipped(label, "timed out", f" after {left:.0f}s"))
            elif code == 5:
                results.append(_skipped(label, "selection collected no tests"))
            elif code == 0:
                results.append(("OK", f"- {label}: OK\n```\n{tail}\n```"))
            elif kind := _infra_kind(out):
                results.append(_skipped(label, kind, f", exit {code}", tail))
            else:
                results.append(
                    ("FAIL", f"- {label}: FAIL (exit {code})\n```\n{tail}\n```")
                )
            break
        else:
            missing += 1
            results.append(_skipped(pkg, "pytest unavailable"))
    if missing == len(results):
        why = " ".join(last.split())[:200]
        return f"SKIPPED: pytest unavailable (env: {env}; last runner said: {why})"
    for pkg in list(by_pkg)[MAX_PKGS:]:
        results.append(_skipped(pkg, "over the package cap", f" of {MAX_PKGS}"))
    return (
        f"{_header([verdict for verdict, _ in results])} pytest scoped to the tests "
        f"named for this diff (env: {env})\n"
        + "\n".join(line for _, line in results)
        + gap
        + f"\n{_GATE_NOTE if _gate_scope() else _SCOPE_NOTE}"
    )


def _skipped(label, kind, detail="", tail=None):
    block = f"\n```\n{tail}\n```" if tail else ""
    return (kind, f"- {label}: SKIPPED ({kind}{detail}){block}")


def _header(verdicts):
    """FINDINGS beats OK beats SKIPPED, so a package that never ran cannot hide under OK."""
    if "FAIL" in verdicts:
        return "FINDINGS:"
    skipped = Counter(v for v in verdicts if v != "OK")
    if not skipped:
        return "OK:"
    ran = len(verdicts) - sum(skipped.values())
    reasons = "; ".join(f"{kind}: {n}" for kind, n in skipped.most_common())
    outcome = f"the {ran} that ran passed" if ran else "none ran"
    return (
        f"SKIPPED: {sum(skipped.values())} of {len(verdicts)} package(s) did not run "
        f"({reasons}), {outcome} -"
    )


if __name__ == "__main__":
    try:
        print(run(sys.argv[1], sys.argv[2]))
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
    sys.exit(0)
