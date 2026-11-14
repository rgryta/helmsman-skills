import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import _diff  # noqa: E402
import _env  # noqa: E402


def _is_import_error(line):
    return "could not be resolved" in line or "reportMissingImports" in line


def run(worktree, base):
    files = _diff.changed_py_files(worktree, base)
    if not files:
        return "OK: no python changes"
    started = time.monotonic()
    python, trusted, env = _env.ensure_project_env(worktree)
    flags = ["--level", "error"]
    tools = []
    if python:
        flags = ["--pythonpath", python, *flags]
        pinned = Path(python).with_name("pyright.exe" if os.name == "nt" else "pyright")
        if pinned.exists():
            tools.append([str(pinned)])
    tools += [["pyright"], ["uvx", "pyright"]]
    for tool in tools:
        left = max(1, _diff.CHECK_BUDGET_S - (time.monotonic() - started))
        code, out = _diff.run_cmd([*tool, *flags, *files], cwd=worktree, timeout=left)
        if code == -2:
            continue
        if code in (-1, -3):
            return f"SKIPPED: pyright {out} (env: {env})"
        if code == 0:
            return f"OK: pyright clean on {len(files)} changed files (env: {env})"
        lines = [line.strip() for line in out.splitlines() if " - error" in line]
        if not lines:
            return f"SKIPPED: pyright exited {code} with no parsed error lines (env: {env})"
        dropped = 0
        if not trusted:
            real = [line for line in lines if not _is_import_error(line)]
            if not real:
                return (
                    f"SKIPPED: pyright saw import-resolution errors only (env: {env})"
                )
            dropped, lines = len(lines) - len(real), real
        noise = (
            f", dropped {dropped} import-resolution line(s) as env noise"
            if dropped
            else ""
        )
        return f"FINDINGS: pyright (env: {env}{noise})\n" + "\n".join(
            f"- {line}" for line in lines[:60]
        )
    return "SKIPPED: pyright unavailable"


if __name__ == "__main__":
    try:
        print(run(sys.argv[1], sys.argv[2]))
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
    sys.exit(0)
