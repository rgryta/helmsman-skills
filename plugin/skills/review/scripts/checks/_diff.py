import contextlib
import os
import re
import shutil
import signal
import subprocess


def run_cmd(args, cwd, timeout):
    if shutil.which(args[0]) is None:
        return -2, "MISSING"
    try:
        proc = subprocess.Popen(
            args,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            start_new_session=True,
        )
    except OSError as e:
        return -3, f"ERROR {e}"
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # a runner's own children (uv's pytest, a parallel harness's workers) outlive a plain kill
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.communicate(timeout=5)
        return -1, "TIMEOUT"
    out = stdout.strip()
    return proc.returncode, out if out else stderr.strip()


def changed_py_files(worktree, base):
    r = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", base, "HEAD"],
        cwd=worktree,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError(f"git diff failed: {r.stderr.strip()[:200]}")
    return [f for f in r.stdout.splitlines() if f.endswith(".py")]


_DIFF_HEADER = re.compile(r"^diff --git ")
_HUNK = re.compile(r"^@@ -\S+ \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")
_PLUS_FILE = re.compile(r"^\+\+\+ (?:b/)?(?P<path>.+)$")
_RENAME_TO = re.compile(r"^rename to (?P<path>.+)$")


def added_lines_by_path(worktree, base, paths):
    """Map each path to the line numbers this diff ADDED, or None if unknown.

    One `git diff -M` for the whole diff rather than one per path: scoping to a
    single pathspec defeats rename detection, so a pure rename reads as a wholly
    new file. `None` means provenance could not be determined and is deliberately
    distinct from an empty set, which means the file genuinely gained no lines.
    """
    code, out = run_cmd(
        ["git", "diff", "-U0", "-M", base, "HEAD"], cwd=worktree, timeout=120
    )
    if code != 0:
        return {p: None for p in paths}
    seen, current, result = set(), None, {}
    for line in out.splitlines():
        if _DIFF_HEADER.match(line):
            current = None
            continue
        m = _PLUS_FILE.match(line) or _RENAME_TO.match(line)
        if m:
            # a pure rename carries `rename to` and no +++ line at all
            current = m.group("path").strip().strip('"')
            if current == "/dev/null":
                current = None
                continue
            seen.add(current)
            result.setdefault(current, set())
            continue
        h = _HUNK.match(line)
        if h and current is not None:
            start = int(h.group("start"))
            count = 1 if h.group("count") is None else int(h.group("count"))
            if count:
                result[current].update(range(start, start + count))
    # a path the diff never named at all is unknown, not known-empty
    return {p: (result.get(p) if p in seen else None) for p in paths}
