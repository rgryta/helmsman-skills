import concurrent.futures
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERIAL = {"h_mutation"}
CHECKS: list | None = None
# a gate caller (fold-review) needs a real suite to finish where a review's
# fact pass only needs it not to hang
PER_CHECK_S = int(os.environ.get("HELMSMAN_PHASE0_CHECK_S", "240"))
GRACE_S = 5
PARALLEL_BUDGET_S = PER_CHECK_S + 10
SERIAL_BUDGET_S = PER_CHECK_S + 10


def _discover_names():
    return sorted(
        p.stem for p in (HERE / "checks").glob("*.py") if not p.stem.startswith("_")
    )


def _merge_base(worktree):
    try:
        r = subprocess.run(
            ["git", "merge-base", "origin/main", "HEAD"],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    out = r.stdout.strip()
    return out if r.returncode == 0 and out else None


def _write_atomic(path, text):
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(text)
    os.replace(tmp, path)


def _dirty(worktree):
    try:
        r = subprocess.run(
            ["git", "-C", worktree, "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    return {
        line[3:].strip()
        for line in r.stdout.splitlines()
        if line.strip() and not line.startswith("??")
    }


def _repair_worktree(worktree, preexisting):
    """Revert what a check dirtied, never what the operator had open.

    ``git checkout --`` is unrecoverable, so a file already modified when
    Phase 0 started is left alone even though a check may also have touched
    it: uncommitted work is worth more than a clean fact run.
    """
    now = _dirty(worktree)
    if not now:
        return [], []
    held = sorted(now & preexisting) if preexisting else []
    dirty = sorted(now - set(held))
    if dirty:
        try:
            subprocess.run(
                ["git", "-C", worktree, "checkout", "--", *dirty],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (subprocess.TimeoutExpired, OSError):
            pass
    return dirty, held


def _run_subprocess_check(name, worktree, base):
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "checks" / f"{name}.py"), worktree, base],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        out, err = proc.communicate(timeout=PER_CHECK_S)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            out, err = proc.communicate(timeout=GRACE_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return "SKIPPED: check timed out"
        if proc.returncode != 0:
            # killed by the terminate, so returncode is -15: a budget overrun,
            # never the crash the branch below would otherwise report
            return "SKIPPED: check timed out"
        # a check that handled the signal and printed its own partial verdict
        # keeps it, so fall through
    out = out.strip()
    if proc.returncode != 0:
        tail = " | ".join(err.strip().splitlines()[-3:])[:300]
        return f"ERROR: check crashed (exit {proc.returncode}) {tail}"
    return out or "ERROR: check produced no output"


def _run_wave(entries, worktree, base, budget_s, sections, injected):
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=8)
    try:
        if injected:
            futs = {ex.submit(mod.run, worktree, base): name for name, mod in entries}
        else:
            futs = {
                ex.submit(_run_subprocess_check, name, worktree, base): name
                for name in entries
            }
        done, pending = concurrent.futures.wait(futs, timeout=budget_s)
        for fut in done:
            name = futs[fut]
            try:
                sections[name] = fut.result()
            except Exception as e:
                sections[name] = f"ERROR: check crashed ({type(e).__name__}: {e})"
        for fut in pending:
            sections[futs[fut]] = "SKIPPED: check timed out"
            fut.cancel()
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


def main(argv):
    if len(argv) < 2:
        print("usage: phase0.py <worktree> <out_file>", file=sys.stderr)
        return 2
    worktree, out_file = argv[0], argv[1]
    base = _merge_base(worktree)
    if base is None:
        try:
            _write_atomic(
                Path(out_file),
                "# Phase 0 facts (base unresolved)\n\n"
                "SKIPPED: could not resolve merge-base vs origin/main - no deterministic facts\n",
            )
        except OSError as e:
            print(f"failed to write FACTS: {e}", file=sys.stderr)
        return 0
    injected = CHECKS is not None
    entries = _discover_names() if CHECKS is None else CHECKS
    names = [n for n, _ in entries] if injected else list(entries)
    par = [e for e in entries if (e[0] if injected else e) not in SERIAL]
    ser = [e for e in entries if (e[0] if injected else e) in SERIAL]
    sections = {}
    preexisting = _dirty(worktree) or set()
    try:
        _run_wave(par, worktree, base, PARALLEL_BUDGET_S, sections, injected)
        _run_wave(ser, worktree, base, SERIAL_BUDGET_S, sections, injected)
    except Exception as e:
        sections["_runner"] = f"ERROR: runner crashed ({type(e).__name__}: {e})"
        names.append("_runner")
    dirty, held = _repair_worktree(worktree, preexisting)
    if dirty or held:
        parts = []
        if dirty:
            parts.append(
                f"a check left {len(dirty)} tracked file(s) modified (likely a "
                "forced termination that skipped its own cleanup) and they were "
                "restored to HEAD before these facts were written: " + ", ".join(dirty)
            )
        if held:
            parts.append(
                f"{len(held)} tracked file(s) were already modified when Phase 0 "
                "started and were left untouched, so these facts describe a tree "
                "that is not at HEAD: " + ", ".join(held)
            )
        sections["_worktree_repair"] = "SKIPPED: worktree state - " + "; ".join(parts)
        names.append("_worktree_repair")
    body = "\n\n".join(f"## {n}\n{sections.get(n, 'ERROR: no result')}" for n in names)
    try:
        _write_atomic(Path(out_file), f"# Phase 0 facts (base {base[:10]})\n\n{body}\n")
    except OSError as e:
        print(f"failed to write FACTS: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
