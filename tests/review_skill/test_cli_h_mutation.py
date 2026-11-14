import shutil
import signal
import subprocess
import sys
import time

import pytest

from .conftest import (
    CLI_MANIFEST,
    NEW_GROUP,
    SCRIPTS,
    _git,
    assert_parity,
    commit_all,
    skip_shims_on_windows,
    stop,
)
from .test_phase0 import needs_pytest

GUARD = "def check_guard(n):\n    return n > 10\n"
MUTATED = "git diff --quiet && exit 0\n"

WALK_ORDER = """import re


@policy(x > 1)
def check(a=b < 2, *, k=c >= 3) -> None:
    def inner_verify():
        return {d == 4: e != 5, f <= 6: g > 7}
    if h > 8:
        pass
    elif i < 9:
        pass
    elif j > 10:
        pass
    v = [m for m in n if m > 11]
    w = (p if q > 12 else r)
    x = f"{s > 13} {t:{u > 14}}"
    y = lambda z=aa > 15: z
    return call(bb > 16, kw=cc < 17)


class Validator(Base, meta=dd > 18):
    def mask(self):
        assert ee >= 19, ff <= 20
        return "é" > gg and éé > hh


def unrelated(n):
    return n > 1
"""


def _write(repo, rel, body):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body.encode() if isinstance(body, str) else body)


def _bin(tmp_path, monkeypatch, **tools):
    skip_shims_on_windows()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name in ("git", "sh", "sleep"):
        if not (bin_dir / name).exists():
            (bin_dir / name).symlink_to(shutil.which(name))
    for name, body in tools.items():
        exe = bin_dir / name
        exe.write_text("#!/bin/sh\n" + body)
        exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    return bin_dir


def _commit(repo, files):
    for rel, body in files.items():
        _write(repo, rel, body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "--allow-empty", "-m", "case")


CASES = {
    "only_non_python": ({"README.md": "hi\n"}, {"pytest": "exit 0\n"}),
    "only_tests": (
        {"pkg/test_mod.py": "def test_check():\n    assert 1 > 0\n"},
        {"pytest": "exit 0\n"},
    ),
    "no_guards": ({}, {"pytest": "exit 0\n"}),
    "survived": ({"pkg/mod.py": GUARD}, {"pytest": "exit 0\n"}),
    "killed": ({"pkg/mod.py": GUARD}, {"pytest": MUTATED + "exit 1\n"}),
    "uncovered": ({"pkg/mod.py": GUARD}, {"pytest": MUTATED + "exit 5\n"}),
    "inconclusive": ({"pkg/mod.py": GUARD}, {"pytest": MUTATED + "exit 7\n"}),
    "red_baseline": ({"pkg/mod.py": GUARD}, {"pytest": "exit 1\n"}),
    "pytest_unavailable": ({"pkg/mod.py": GUARD}, {}),
    "spawn_failure_falls_through": (
        {"pkg/mod.py": GUARD, "uv.lock": ""},
        {
            "uv": 'case "$1" in sync) exit 0;; esac\n'
            "echo 'error: Failed to spawn: `pytest`' 1>&2\nexit 2\n",
            "pytest": MUTATED + "exit 3\n",
        },
    ),
    "workspace_member_runs_through_uv_first": (
        {"pkg/mod.py": GUARD, "uv.lock": ""},
        {"uv": 'case "$1" in sync) exit 1;; esac\nexit 0\n'},
    ),
    "syntax_error_skipped": (
        {"pkg/mod.py": "def check_guard(n:\n    return n > 10\n"},
        {"pytest": "exit 0\n"},
    ),
    "no_owning_package": (
        {"loose/mod.py": GUARD, "pkg/mod.py": GUARD},
        {"pytest": MUTATED + "exit 1\n"},
    ),
    "walk_order_cap_and_unicode": (
        {"pkg/a.py": WALK_ORDER, "pkg/b.py": GUARD.replace("10", "99")},
        {"pytest": "exit 0\n"},
    ),
    "crlf_and_bad_utf8": (
        {"pkg/mod.py": b"def verify(n):\r\n    return n != 1  # \xff\r\n"},
        {"pytest": MUTATED + "exit 1\n"},
    ),
}


@pytest.mark.parametrize("case", CASES)
def test_rust_port_matches_python(
    case, helmsman_cli, fixture_repo, tmp_path, monkeypatch
):
    """The Rust check prints what the Python check prints, and leaves the sources as they were."""
    files, tools = CASES[case]
    _commit(fixture_repo, files)
    _bin(tmp_path, monkeypatch, **tools)
    assert_parity(helmsman_cli, "h_mutation", fixture_repo)
    _git(fixture_repo, "diff", "--exit-code")


@pytest.mark.parametrize(
    "env",
    [
        {"HELMSMAN_PHASE0_CHECK_S": "30"},
        {"HELMSMAN_PHASE0_CHECK_S": "nope"},
        {"HELMSMAN_PYTEST_RUNNER": "'unclosed"},
        {"HELMSMAN_PYTEST_RUNNER": "sh -c 'exit 0' --"},
    ],
)
def test_rust_port_matches_python_on_env_knobs(
    env, helmsman_cli, fixture_repo, tmp_path, monkeypatch
):
    _commit(fixture_repo, {"pkg/mod.py": GUARD})
    _bin(tmp_path, monkeypatch, pytest=MUTATED + "exit 1\n")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert_parity(helmsman_cli, "h_mutation", fixture_repo)


def test_rust_port_matches_python_on_git_failure(helmsman_cli, fixture_repo):
    assert_parity(helmsman_cli, "h_mutation", fixture_repo, base="no-such-ref")


def _stopped_run(argv, repo, sig):
    target = repo / "pkg" / "mod.py"
    original = target.read_bytes()
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 20
    while target.read_bytes() == original:
        assert proc.poll() is None and time.monotonic() < deadline, "no mutant seen"
        time.sleep(0.05)
    proc.send_signal(sig)
    out, _ = proc.communicate(timeout=20)
    assert target.read_bytes() == original
    return out, proc.returncode


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_rust_port_matches_python_when_stopped_mid_mutant(
    sig, helmsman_cli, fixture_repo, tmp_path, monkeypatch
):
    """A stop during a mutant run restores the file and reports the partial result the same way."""
    _commit(fixture_repo, {"pkg/mod.py": GUARD.replace("> 10", "> 10 and n < 20")})
    _bin(tmp_path, monkeypatch, pytest=MUTATED + "exec sleep 30\n")
    check = ["h_mutation", str(fixture_repo), "origin/main"]
    py = _stopped_run(
        [sys.executable, str(SCRIPTS / "checks" / f"{check[0]}.py"), *check[1:]],
        fixture_repo,
        sig,
    )
    rs = _stopped_run([str(helmsman_cli), "check", *check], fixture_repo, sig)
    assert rs == py
    assert "stopped early" in py[0]


def test_rust_port_matches_python_on_own_history(helmsman_cli, tmp_path, monkeypatch):
    root = CLI_MANIFEST.parents[1]
    base = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", "HEAD~1"],
        cwd=root,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not base:
        pytest.skip("shallow clone without enough history")
    _bin(tmp_path, monkeypatch, pytest="exit 0\n")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(tmp_path / "no-venv"))
    assert_parity(helmsman_cli, "h_mutation", root, base=base)


@needs_pytest
def test_rust_check_restores_the_mutant_when_stopped(helmsman_cli, fixture_repo):
    """SIGTERM, or a console break on Windows, mid-mutant leaves the source as committed."""
    mod_path = fixture_repo / "pkg" / "mod.py"
    mod_path.write_text(GUARD)
    (fixture_repo / "pkg" / "test_mod.py").write_text(
        "import time\n\nfrom pkg.mod import check_guard\n\n\n"
        "def test_guard():\n    if check_guard(5):\n        time.sleep(30)\n"
        "    assert check_guard(50)\n"
    )
    commit_all(fixture_repo)
    original = mod_path.read_bytes()
    proc = subprocess.Popen(
        [str(helmsman_cli), "check", "h_mutation", str(fixture_repo), "origin/main"],
        stdout=subprocess.PIPE,
        text=True,
        **NEW_GROUP,
    )
    try:
        deadline = time.monotonic() + 30
        while mod_path.read_bytes() == original:
            assert proc.poll() is None and time.monotonic() < deadline, "no mutant seen"
            time.sleep(0.05)
        stop(proc)
        out, _ = proc.communicate(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert mod_path.read_bytes() == original
    assert "stopped early" in out
