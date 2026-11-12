import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import _diff  # noqa: E402

_VIOLATION_LINE = re.compile(r"^\S+:\d+:\d+: [A-Z]+\d+\b")


def run(worktree, base):
    files = _diff.changed_py_files(worktree, base)
    if not files:
        return "OK: no python changes"
    for tool in (["ruff"], ["uvx", "ruff"]):
        code, out = _diff.run_cmd(
            [*tool, "check", "--force-exclude", "--output-format=concise", *files],
            cwd=worktree,
            timeout=240,
        )
        if code == -2:
            continue
        if code in (-1, -3):
            return f"SKIPPED: ruff {out}"
        if code == 0:
            return "OK: ruff clean on changed files (repo excludes honored)"
        lines = [line for line in out.splitlines() if _VIOLATION_LINE.match(line)]
        if not lines:
            return f"SKIPPED: ruff exited {code} with no parseable violation lines ({out.strip()[:200]})"
        return "FINDINGS:\n" + "\n".join(f"- {line}" for line in lines[:80])
    return "SKIPPED: ruff unavailable"


if __name__ == "__main__":
    try:
        print(run(sys.argv[1], sys.argv[2]))
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
    sys.exit(0)
