import re
import subprocess
import sys
from pathlib import Path

REF = re.compile(
    r"(?P<path>(?:[\w.-]+/)*[\w.-]+\.[A-Za-z]\w*)"
    r"(?:[:#]L?(?P<start>\d+)(?:\s*[-–]\s*L?(?P<end>\d+))?)?"
)


def _line_count(path):
    try:
        with path.open("rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return None


BLOB_URL = re.compile(r"https?://github\.com/[^/\s]+/[^/\s]+/blob/[^/\s]+/")


def _tracked(root):
    try:
        r = subprocess.run(
            ["git", "ls-files"], cwd=root, capture_output=True, text=True, timeout=60
        )
    except OSError:
        return []
    return r.stdout.splitlines() if r.returncode == 0 else []


def resolve(root, path, tracked):
    """The worktree-relative file a citation names, or (None, why)."""
    if (root / path).is_file():
        return path, None
    if path.startswith(("a/", "b/")) and (root / path[2:]).is_file():
        return path[2:], None
    bare = path[2:] if path.startswith(("a/", "b/")) else path
    hits = [f for f in tracked if f == bare or f.endswith("/" + bare)]
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        return None, f"ambiguous: suffix of {len(hits)} tracked files"
    return None, "no such file in the worktree"


def collect(text):
    text = BLOB_URL.sub("", text)
    refs = []
    for m in REF.finditer(text):
        if m.group("start") is None:
            continue
        end = m.group("end")
        refs.append(
            (
                m.group("path"),
                int(m.group("start")),
                int(end) if end else int(m.group("start")),
            )
        )
    return sorted(set(refs))


def check(worktree, refs, tracked):
    bad = []
    root = Path(worktree)
    counts = {}
    for path, start, end in refs:
        if path not in counts:
            real, why = resolve(root, path, tracked)
            counts[path] = (_line_count(root / real), why) if real else (None, why)
        n, why = counts[path]
        if n is None:
            bad.append((path, start, end, why or "no such file in the worktree"))
        elif start < 1 or end < start:
            bad.append((path, start, end, "nonsensical line range"))
        elif end > n:
            bad.append((path, start, end, f"file has only {n} lines"))
    return bad


def main(argv):
    if len(argv) < 2:
        print(
            "usage: verify_citations.py <worktree> <findings-file> ...", file=sys.stderr
        )
        return 2
    worktree, files = argv[0], argv[1:]
    tracked = _tracked(worktree)
    exts = {Path(t).suffix for t in tracked}
    refs = []
    unreadable = []
    for f in files:
        try:
            found = [
                r
                for r in collect(Path(f).read_text())
                # a bare `host.com:8080` is not a file unless the repo tracks such files
                if "/" in r[0] or Path(r[0]).suffix in exts
            ]
        except OSError as e:
            unreadable.append(f"{f} ({type(e).__name__})")
            continue
        if not found:
            print(
                f"WARNING: {f} cites no file:line - any findings in it are unanchored"
            )
        refs.extend(found)
    for f in unreadable:
        print(f"UNREADABLE: {f}")
    refs = sorted(set(refs))
    bad = check(worktree, refs, tracked)
    for path, start, end, why in bad:
        span = f"{start}" if start == end else f"{start}-{end}"
        print(f"BAD {path}:{span} - {why}")
    print(f"CHECKED: {len(refs)} citation(s)")
    print(f"BAD_CITATIONS: {len(bad)}" if bad else "ALL_CITATIONS_RESOLVE")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
