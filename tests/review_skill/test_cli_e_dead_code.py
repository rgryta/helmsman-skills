import shutil
import subprocess

import pytest

from .conftest import (
    CLI_MANIFEST,
    _git,
    assert_parity,
    posix_only,
    skip_shims_on_windows,
)

needs_vulture = pytest.mark.skipif(
    not (shutil.which("vulture") or shutil.which("uvx")), reason="no vulture/uvx"
)


def _write(repo, rel, body):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


REAL_VULTURE_CASES = {
    "unused_param": lambda r: _write(
        r, "pkg/mod.py", "def f(unused_param):\n    return 1\n"
    ),
    "only_unused_import": lambda r: _write(
        r, "pkg/mod.py", "import os\n\n\ndef f():\n    return 1\n"
    ),
    "clean": lambda r: None,
    "syntax_error": lambda r: _write(r, "pkg/mod.py", "def added(y:\n    return y\n"),
    "space_and_unicode_paths": lambda r: (
        _write(r, "pkg/my mod.py", "def f(a):\n    return 1\n"),
        _write(r, "pkg/\u00e9.py", "def g(b):\n    return 1\n"),
    ),
    "over_40_findings": lambda r: _write(
        r,
        "pkg/mod.py",
        "".join(f"def f{n}(p{n}):\n    return 1\n\n\n" for n in range(60)),
    ),
    "non_python_change_only": lambda r: _write(r, "README.md", "hi\n"),
    "deleted_only": lambda r: (r / "pkg" / "mod.py").unlink(),
}


@needs_vulture
@pytest.mark.parametrize("case", REAL_VULTURE_CASES)
def test_rust_port_matches_python_with_real_vulture(case, helmsman_cli, fixture_repo):
    """The Rust check prints what the Python check prints, with its exit code, on real vulture output."""
    REAL_VULTURE_CASES[case](fixture_repo)
    _git(fixture_repo, "add", "-A")
    _git(fixture_repo, "commit", "--allow-empty", "-m", case)
    assert_parity(helmsman_cli, "e_dead_code", fixture_repo)


def _bin_dir(tmp_path, monkeypatch, **tools):
    skip_shims_on_windows()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("git", "sh", "sleep"):
        real = shutil.which(name)
        if real:
            (bin_dir / name).symlink_to(real)
    for name, body in tools.items():
        exe = bin_dir / name
        exe.write_text("#!/bin/sh\n" + body)
        exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    return bin_dir


SHIM_CASES = {
    "uvx_fallback": {"uvx": "echo FAKE-UVX-RAN\nexit 1\n"},
    "unavailable": {},
    "clean_exit": {"vulture": "exit 0\n"},
    "broken_output": {
        "vulture": "echo 'Traceback (most recent call last):'\necho 'ValueError: boom'\nexit 1\n"
    },
    "imports_only": {
        "vulture": "echo \"pkg/mod.py:1: unused import 'os' (90% confidence)\"\nexit 3\n"
    },
    "mixed_hits_and_noise": {
        "vulture": "echo \"pkg/mod.py:1: unused import 'os' (90% confidence)\"\n"
        "echo \"my dir/m.py:4: unused function 'f' (60% confidence)\"\n"
        "echo \"m.py:5: unused variable 'x' (100% confidence) trailing\"\n"
        "echo 'noise'\nexit 3\n"
    },
    "stdout_empty_falls_back_to_stderr": {
        "vulture": "echo \"pkg/mod.py:1: unused variable 'x' (100% confidence)\" 1>&2\nexit 3\n"
    },
    "python_line_breaks_and_bad_utf8": {
        "vulture": "printf \"a.py:1: unused variable 'x' (100%% confidence)\\fb.py:2: unused "
        "function 'g' (60%% confidence)\\302\\205c.py:3: unused \\377 'z' (60%% confidence)\\n\"\n"
        "exit 3\n"
    },
    "long_unparseable_output": {"vulture": "printf '%0500d\\n' 0\nexit 3\n"},
    "killed_by_signal": {"vulture": "kill -9 $$\n"},
}


@pytest.mark.parametrize("case", SHIM_CASES)
def test_rust_port_matches_python_with_shimmed_tools(
    case, helmsman_cli, fixture_repo, tmp_path, monkeypatch
):
    _bin_dir(tmp_path, monkeypatch, **SHIM_CASES[case])
    assert_parity(helmsman_cli, "e_dead_code", fixture_repo)


@pytest.mark.parametrize("budget", ["nope", "5"])
def test_rust_port_matches_python_on_budget_env(
    budget, helmsman_cli, fixture_repo, tmp_path, monkeypatch
):
    """The tool timeout stays fixed; only a malformed budget env var changes the outcome."""
    _bin_dir(tmp_path, monkeypatch, vulture="exit 0\n")
    monkeypatch.setenv("HELMSMAN_PHASE0_CHECK_S", budget)
    assert_parity(helmsman_cli, "e_dead_code", fixture_repo)


def test_rust_port_matches_python_on_git_failure(helmsman_cli, fixture_repo):
    assert_parity(helmsman_cli, "e_dead_code", fixture_repo, base="no-such-ref")


@needs_vulture
def test_rust_port_matches_python_on_own_history(helmsman_cli):
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", "HEAD~1"],
        cwd=CLI_MANIFEST.parents[1],
        capture_output=True,
        encoding="utf-8",
    ).stdout.strip()
    if not head:
        pytest.skip("shallow clone without enough history")
    assert_parity(helmsman_cli, "e_dead_code", CLI_MANIFEST.parents[1], base=head)


@posix_only
@pytest.mark.parametrize("uvx", [None, "echo FAKE-UVX-RAN\nexit 1\n"])
def test_rust_port_matches_python_on_non_executable_vulture(
    uvx, helmsman_cli, fixture_repo, tmp_path, monkeypatch
):
    """A vulture on PATH without the execute bit counts as missing, as `shutil.which` does."""
    bin_dir = _bin_dir(tmp_path, monkeypatch, **({"uvx": uvx} if uvx else {}))
    vulture = bin_dir / "vulture"
    vulture.write_text("#!/bin/sh\necho SHOULD-NOT-RUN\nexit 1\n")
    vulture.chmod(0o644)
    assert_parity(helmsman_cli, "e_dead_code", fixture_repo)
