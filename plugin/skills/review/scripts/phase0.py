import concurrent.futures
import contextlib
import fcntl
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERIAL = {"h_mutation"}
HEAVY = {"c_pytest", "h_mutation"}
SKIP_ENV = "HELMSMAN_PHASE0_SKIP"
CLI_ENV = "HELMSMAN_CLI"
MANIFEST = HERE.parents[2] / ".claude-plugin" / "plugin.json"
FETCH = HERE / "fetch_cli.py"
FETCH_TIMEOUT_S = 30
LOCK_PATH = Path(
    os.environ.get("HELMSMAN_PHASE0_LOCK", Path.home() / ".helmsman" / "phase0.lock")
)
CHECKS: list | None = None
# a gate caller (fold-review) needs a real suite to finish where a review's
# fact pass only needs it not to hang
PER_CHECK_S = int(os.environ.get("HELMSMAN_PHASE0_CHECK_S", "240"))
GRACE_S = 5
PARALLEL_BUDGET_S = PER_CHECK_S + 10
SERIAL_BUDGET_S = PER_CHECK_S + 10
# callers collect the whole run at 600 s
LOCK_WAIT_S = max(0, 590 - PARALLEL_BUDGET_S - SERIAL_BUDGET_S)


def _name(entry):
    return entry[0] if isinstance(entry, tuple) else entry


@contextlib.contextmanager
def _heavy_lock():
    """One heavy wave per machine: parallel sessions' suites turn load into flaky tests."""
    try:
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        handle = open(LOCK_PATH, "a")
    except OSError:
        yield True
        return
    with handle:
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(1)
        yield True


def _run_heavy(entries, worktree, base, sections, injected, fast_wave, routes):
    if not entries:
        return
    with _heavy_lock() as held:
        if not held:
            for e in entries:
                sections[_name(e)] = (
                    f"SKIPPED: another Phase 0 run held the machine-wide heavy-check "
                    f"lock ({LOCK_PATH}) for {LOCK_WAIT_S}s"
                )
            return
        par = [e for e in entries if _name(e) not in SERIAL]
        ser = [e for e in entries if _name(e) in SERIAL]
        _run_wave(par, worktree, base, PARALLEL_BUDGET_S, sections, injected, routes)
        fast_wave.result()  # mutation rewrites source that fast checks read
        _run_wave(ser, worktree, base, SERIAL_BUDGET_S, sections, injected, routes)


def _discover_names():
    return sorted(
        p.stem for p in (HERE / "checks").glob("*.py") if not p.stem.startswith("_")
    )


def _merge_base(worktree, ref):
    try:
        r = subprocess.run(
            ["git", "merge-base", ref, "HEAD"],
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


def _resolve_base(worktree, ref):
    for candidate in [ref] if ref else ["origin/HEAD", "origin/main"]:
        base = _merge_base(worktree, candidate)
        if base:
            return candidate, base
    return ref or "origin/HEAD or origin/main", None


def _dirty(worktree):
    try:
        r = subprocess.run(
            ["git", "-C", worktree, "status", "--porcelain", "-z"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    paths = set()
    records = iter(r.stdout.split("\0"))
    for rec in records:
        if not rec:
            continue
        if rec[0] in "RC":  # -z gives a rename's source path its own record
            paths.add(next(records, ""))
        if not rec.startswith("??"):
            paths.add(rec[3:])
    paths.discard("")
    return paths


def _repair_worktree(worktree, preexisting):
    """Revert what a check dirtied, never what the operator had open.

    ``git restore`` is unrecoverable, so a file already modified when
    Phase 0 started is left alone even though a check may also have touched
    it: uncommitted work is worth more than a clean fact run.
    """
    if preexisting is None:
        return (
            [],
            [],
            "could not read git status before the checks ran, so nothing was restored",
        )
    now = _dirty(worktree)
    if now is None:
        return [], [], "could not read git status after the checks ran"
    held = sorted(now & preexisting) if preexisting else []
    dirty = sorted(now - set(held))
    if not dirty:
        return [], held, None
    try:
        r = subprocess.run(
            [
                "git",
                "--literal-pathspecs",
                "-C",
                worktree,
                "restore",
                "--source=HEAD",
                "--staged",
                "--worktree",
                "--",
                *dirty,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return dirty, held, f"{type(e).__name__}: {e}"
    if r.returncode != 0:
        return dirty, held, f"exit {r.returncode}: {r.stderr.strip()[:200]}"
    return dirty, held, None


def _cli_path():
    env = os.environ.get(CLI_ENV)
    if env:
        return None if env == "off" else Path(env)
    try:
        version = json.loads(MANIFEST.read_text())["version"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    exe = "helmsman.exe" if os.name == "nt" else "helmsman"
    path = Path.home() / ".helmsman" / "bin" / version / exe
    return path if path.is_file() else _fetch_cli()


def _fetch_cli():
    try:
        r = subprocess.run(
            [sys.executable, str(FETCH)],
            stdout=subprocess.PIPE,
            text=True,
            timeout=FETCH_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    path = r.stdout.strip()
    return Path(path) if r.returncode == 0 and path else None


def _cli_routes():
    """Map each check the binary ports to the binary; any doubt runs it all in Python."""
    cli = _cli_path()
    if cli is None:
        return {}
    try:
        r = subprocess.run(
            [str(cli), "checks"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError) as e:
        reason = f"{type(e).__name__}: {e}"
    else:
        if r.returncode == 0:
            return {name: str(cli) for name in r.stdout.split()}
        reason = f"`checks` exited {r.returncode}"
    print(
        f"NOTE: {cli} unusable ({reason}), running every check in Python",
        file=sys.stderr,
    )
    return {}


def _run_subprocess_check(name, worktree, base, cli=None):
    argv = (
        [cli, "check", name]
        if cli
        else [sys.executable, str(HERE / "checks" / f"{name}.py")]
    )
    proc = subprocess.Popen(
        [*argv, worktree, base],
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
            # killed by the terminate (-15, or 143 via _diff's handler): a budget overrun,
            # never the crash the branch below would otherwise report
            return "SKIPPED: check timed out"
        # a check that handled the signal and printed its own partial verdict
        # keeps it, so fall through
    out = out.strip()
    if proc.returncode != 0:
        tail = " | ".join(err.strip().splitlines()[-3:])[:300]
        return f"ERROR: check crashed (exit {proc.returncode}) {tail}"
    return out or "ERROR: check produced no output"


def _run_wave(entries, worktree, base, budget_s, sections, injected, routes):
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=8)
    try:
        if injected:
            futs = {ex.submit(mod.run, worktree, base): name for name, mod in entries}
        else:
            futs = {
                ex.submit(
                    _run_subprocess_check, name, worktree, base, routes.get(name)
                ): name
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
        print("usage: phase0.py <worktree> <out_file> [base_ref]", file=sys.stderr)
        return 2
    worktree, out_file = argv[0], argv[1]
    ref, base = _resolve_base(worktree, argv[2] if len(argv) > 2 else None)
    if base is None:
        try:
            _write_atomic(
                Path(out_file),
                "# Phase 0 facts (base unresolved)\n\n"
                f"SKIPPED: could not resolve merge-base vs {ref} - no deterministic facts\n",
            )
        except OSError as e:
            print(f"failed to write FACTS: {e}", file=sys.stderr)
        return 0
    injected = CHECKS is not None
    entries = _discover_names() if CHECKS is None else CHECKS
    names = [_name(e) for e in entries]
    skip = {s.strip() for s in os.environ.get(SKIP_ENV, "").split(",") if s.strip()}
    sections = {n: f"SKIPPED: disabled by {SKIP_ENV}" for n in names if n in skip}
    entries = [e for e in entries if _name(e) not in skip]
    fast = [e for e in entries if _name(e) not in HEAVY]
    heavy = [e for e in entries if _name(e) in HEAVY]
    routes = {} if injected else _cli_routes()
    preexisting = _dirty(worktree)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fast_wave = ex.submit(
                _run_wave,
                fast,
                worktree,
                base,
                PARALLEL_BUDGET_S,
                sections,
                injected,
                routes,
            )
            _run_heavy(heavy, worktree, base, sections, injected, fast_wave, routes)
            fast_wave.result()
    except Exception as e:
        sections["_runner"] = f"ERROR: runner crashed ({type(e).__name__}: {e})"
        names.append("_runner")
    dirty, held, repair_error = _repair_worktree(worktree, preexisting)
    if repair_error:
        sections["_worktree_repair"] = (
            f"ERROR: worktree restore failed ({repair_error}), so these facts may "
            "describe a tree that is not at HEAD and the worktree may still carry "
            "what the checks changed" + (": " + ", ".join(dirty) if dirty else "")
        )
        names.append("_worktree_repair")
    elif dirty or held:
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
        _write_atomic(
            Path(out_file), f"# Phase 0 facts (base {base[:10]} from {ref})\n\n{body}\n"
        )
    except OSError as e:
        print(f"failed to write FACTS: {e}", file=sys.stderr)
    return 0


def _exit_with_parent(poll_s=1.0):
    """A SIGKILLed guard cannot forward anything, so its orphan stops itself."""
    parent = os.getppid()

    def watch():
        while os.getppid() == parent:
            time.sleep(poll_s)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpid(), signal.SIGTERM)
        os._exit(128 + signal.SIGTERM)

    threading.Thread(target=watch, daemon=True).start()


if __name__ == "__main__":
    _exit_with_parent()
    sys.exit(main(sys.argv[1:]))
