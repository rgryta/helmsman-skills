import io
import os
import sys
import tokenize
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import _diff  # noqa: E402

CAP = float(os.environ.get("HELMSMAN_COMMENT_DENSITY_CAP", "0.20"))
MIN_ADDED_LINES = 10


def _ratio(text, added=None):
    comment_lines = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                comment_lines.add(tok.start[0])
    except (tokenize.TokenError, SyntaxError, IndentationError, ValueError):
        return 0.0
    code = {n for n, line in enumerate(text.splitlines(), 1) if line.strip()}
    if added is not None:
        code &= added
        comment_lines &= added
        if len(code) < MIN_ADDED_LINES:
            return 0.0
    return len(comment_lines) / max(len(code), 1)


def run(worktree, base):
    heavy = []
    files = _diff.changed_py_files(worktree, base)
    added = _diff.added_lines_by_path(worktree, base, files)
    for f in files:
        try:
            text = Path(worktree, f).read_text(errors="ignore")
        except OSError:
            continue
        r = _ratio(text, added[f])
        if r > CAP:
            scope = "lines" if added[f] is None else "added lines"
            heavy.append(f"- {f}: {r:.0%} of {scope} are comments (cap {CAP:.0%})")
    if heavy:
        return "FINDINGS:\n" + "\n".join(heavy)
    return "OK: added lines of all changed files within comment cap"


if __name__ == "__main__":
    try:
        print(run(sys.argv[1], sys.argv[2]))
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
    sys.exit(0)
