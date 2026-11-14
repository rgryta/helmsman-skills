import os
import shlex
import shutil
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import _diff  # noqa: E402

SYNC_TIMEOUT_S = 150
SPAWN_FAILED = "Failed to spawn"
RUNNER_ENV = "HELMSMAN_PYTEST_RUNNER"


def ensure_project_env(worktree, timeout=SYNC_TIMEOUT_S):
    """Return (interpreter, trusted, note) for the worktree's OWN python env.

    A review worktree is created fresh, so nothing is installed in it and every
    import in the diff reads as unresolved. A sibling checkout's env is not a
    substitute: first-party imports would resolve against that checkout's
    sources, so a module the diff adds reads as missing and a signature the diff
    changes type-checks against the other branch's copy.
    """
    root = Path(worktree)
    python = Path(
        root,
        os.environ.get("UV_PROJECT_ENVIRONMENT", ".venv"),
        "Scripts/python.exe" if os.name == "nt" else "bin/python",
    )
    synced = None
    if not (root / "uv.lock").exists():
        note = "no worktree env, no uv.lock"
    elif shutil.which("uv") is None:
        note = "no worktree env, uv unavailable"
    else:
        code, out = _diff.run_cmd(
            ["uv", "sync", "--frozen"], cwd=str(root), timeout=timeout
        )
        synced = code == 0
        note = (
            "worktree .venv, uv sync clean"
            if synced
            else f"uv sync failed: {' '.join(out.split())[:160]}"
        )
    if not python.exists():
        return None, False, note
    if synced is None:
        return str(python), True, "worktree .venv, no lockfile to verify it against"
    return str(python), synced, note


def runner_unavailable(code, out):
    """uv saying it could not start the tool is a missing runner, not a test result."""
    return code in (-2, -3) or (code == 2 and SPAWN_FAILED in out)


def _dist_name(pkg):
    try:
        with open(Path(pkg) / "pyproject.toml", "rb") as fh:
            return tomllib.load(fh).get("project", {}).get("name")
    except (OSError, tomllib.TOMLDecodeError):
        return None


def pytest_commands(worktree, pkg, python=None):
    """Runners in the order to try them; the first one that spawns decides.

    A workspace shares one env, and only --package makes a member's own sources
    importable. A member's closure does not carry the root's dev group, so a
    runner that is declared only there fails to spawn under --package and the
    root-level runners after it have to be tried, never reported as a failed run.
    """
    root = Path(worktree)
    workspace = (root / "uv.lock").exists() and Path(pkg) != root
    dist = _dist_name(pkg) if workspace else None
    cmds = [["uv", "run", "--no-sync", "--package", dist, "pytest"]] if dist else []
    pytest = "pytest.exe" if os.name == "nt" else "pytest"
    pinned = Path(python).with_name(pytest) if python else None
    if pinned and pinned.exists():
        cmds.append([str(pinned)])
    return cmds + [["pytest"], ["uv", "run", "--no-sync", "pytest"]]


def project_runner_commands(worktree, pkg):
    """The project's own pytest runner, tried before plain pytest, in the same envs.

    HELMSMAN_PYTEST_RUNNER names what to run where ``pytest`` would be run,
    taking pytest's arguments last (e.g. a parallel harness's
    ``python -m <runner> run --``). Unset, a project keeps plain pytest.
    """
    runner = shlex.split(os.environ.get(RUNNER_ENV, ""))
    if not runner:
        return []
    root = Path(worktree)
    workspace = (root / "uv.lock").exists() and Path(pkg) != root
    dist = _dist_name(pkg) if workspace else None
    cmds = [["uv", "run", "--no-sync", "--package", dist, *runner]] if dist else []
    if (root / "uv.lock").exists():
        cmds.append(["uv", "run", "--no-sync", *runner])
    return cmds + [runner]
