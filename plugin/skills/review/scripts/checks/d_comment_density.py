import io
import sys
import tokenize
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import _diff  # noqa: E402

CAP = 0.20


def _ratio(text):
    comment_lines = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                comment_lines.add(tok.start[0])
    except (tokenize.TokenError, SyntaxError, IndentationError, ValueError):
        return 0.0
    code = [line for line in text.splitlines() if line.strip()]
    return len(comment_lines) / max(len(code), 1)


def run(worktree, base):
    heavy = []
    for f in _diff.changed_py_files(worktree, base):
        try:
            text = Path(worktree, f).read_text(errors="ignore")
        except OSError:
            continue
        r = _ratio(text)
        if r > CAP:
            heavy.append(f"- {f}: {r:.0%} comments (cap {CAP:.0%})")
    if heavy:
        return "FINDINGS:\n" + "\n".join(heavy)
    return "OK: all changed files within comment cap"


if __name__ == "__main__":
    try:
        print(run(sys.argv[1], sys.argv[2]))
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
    sys.exit(0)
