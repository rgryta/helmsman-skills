import re
import sys
from pathlib import Path

REF = re.compile(
    r"(?P<path>(?:[\w.-]+/)+[\w.-]+\.\w+)"
    r"(?:[:#]L?(?P<start>\d+)(?:\s*[-–]\s*L?(?P<end>\d+))?)?"
)


def _line_count(path):
    try:
        with path.open("rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return None


def collect(text):
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


def check(worktree, refs):
    bad = []
    root = Path(worktree)
    counts = {}
    for path, start, end in refs:
        if path not in counts:
            counts[path] = _line_count(root / path)
        n = counts[path]
        if n is None:
            bad.append((path, start, end, "no such file in the worktree"))
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
    refs = []
    unreadable = []
    for f in files:
        try:
            refs.extend(collect(Path(f).read_text()))
        except OSError as e:
            unreadable.append(f"{f} ({type(e).__name__})")
    for f in unreadable:
        print(f"UNREADABLE: {f}")
    refs = sorted(set(refs))
    bad = check(worktree, refs)
    for path, start, end, why in bad:
        span = f"{start}" if start == end else f"{start}-{end}"
        print(f"BAD {path}:{span} - {why}")
    print(f"CHECKED: {len(refs)} citation(s)")
    print(f"BAD_CITATIONS: {len(bad)}" if bad else "ALL_CITATIONS_RESOLVE")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
