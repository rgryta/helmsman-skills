import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import _diff  # noqa: E402

_VULTURE_LINE = re.compile(
    r"^(?P<path>\S+):(?P<line>\d+): (?P<kind>unused \S+) '(?P<name>[^']+)' \(\d+% confidence\)$"
)
_SCOPE_NOTE = "scope: changed files only, unused imports excluded (see a_ruff F401), orphans elsewhere not visible"


def _drop_import_hits(lines):
    kept = []
    for line in lines:
        m = _VULTURE_LINE.match(line)
        if m and m.group("kind") == "unused import":
            continue
        kept.append(line)
    return kept


def run(worktree, base):
    files = _diff.changed_py_files(worktree, base)
    if not files:
        return "OK: no python changes"
    for tool in (["vulture"], ["uvx", "vulture"]):
        code, out = _diff.run_cmd(
            [*tool, "--min-confidence", "80", *files], cwd=worktree, timeout=180
        )
        if code == -2:
            continue
        if code in (-1, -3):
            return f"SKIPPED: vulture {out}"
        if code == 0:
            return f"OK: no high-confidence dead code in changed files ({_SCOPE_NOTE})"
        lines = [line for line in out.splitlines() if _VULTURE_LINE.match(line)]
        if not lines:
            return f"SKIPPED: vulture exited {code} with no parseable output ({out.strip()[:200]})"
        lines = _drop_import_hits(lines)
        if not lines:
            return f"OK: no high-confidence dead code in changed files ({_SCOPE_NOTE})"
        return f"FINDINGS: ({_SCOPE_NOTE})\n" + "\n".join(
            f"- {line}" for line in lines[:40]
        )
    return "SKIPPED: vulture unavailable"


if __name__ == "__main__":
    try:
        print(run(sys.argv[1], sys.argv[2]))
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
    sys.exit(0)
