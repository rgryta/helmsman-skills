import shutil
import subprocess

import pytest

from .conftest import CLI_MANIFEST, _git, assert_parity

needs_ruff = pytest.mark.skipif(
    not (shutil.which("ruff") or shutil.which("uvx")), reason="no ruff/uvx"
)


def _write(repo, rel, body):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


REAL_RUFF_CASES = {
    "unused_import": lambda r: _write(r, "pkg/mod.py", "import os\n"),
    "clean": lambda r: None,
    "syntax_error": lambda r: _write(r, "pkg/mod.py", "def added(y:\n    return y\n"),
    "space_and_unicode_paths": lambda r: (
        _write(r, "pkg/my mod.py", "import os\n"),
        _write(r, "pkg/\u00e9.py", "import sys\n"),
    ),
    "over_80_findings": lambda r: _write(
        r, "pkg/mod.py", "".join(f"import m{n}\n" for n in range(100))
    ),
    "repo_excludes_honored": lambda r: (
        _write(r, "ruff.toml", 'extend-exclude = ["pkg/gen.py"]\n'),
        _write(r, "pkg/gen.py", "import os\n"),
    ),
    "non_python_change_only": lambda r: _write(r, "README.md", "hi\n"),
    "deleted_only": lambda r: (r / "pkg" / "mod.py").unlink(),
}


@needs_ruff
@pytest.mark.parametrize("case", REAL_RUFF_CASES)
def test_rust_port_matches_python_with_real_ruff(case, helmsman_cli, fixture_repo):
    """The Rust check prints what the Python check prints, with its exit code, on real ruff output."""
    REAL_RUFF_CASES[case](fixture_repo)
    _git(fixture_repo, "add", "-A")
    _git(fixture_repo, "commit", "--allow-empty", "-m", case)
    assert_parity(helmsman_cli, "a_ruff", fixture_repo)


def _bin_dir(tmp_path, monkeypatch, **tools):
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
    "broken_output": {
        "ruff": "echo 'ruff failed' 1>&2\necho '  Cause: bad toml' 1>&2\nexit 2\n"
    },
    "older_syntax_error_lines": {
        "ruff": "echo \"m.py:1:13: SyntaxError: Expected ')', found newline\"\n"
        "echo 'Found 1 error.'\nexit 1\n"
    },
    "stdout_empty_falls_back_to_stderr": {
        "ruff": "echo 'pkg/mod.py:1:1: F401 x' 1>&2\nexit 1\n"
    },
    "python_line_breaks_and_bad_utf8": {
        "ruff": "printf 'a.py:1:1: E1 x\\fb.py:2:2: E2 y\\302\\205c.py:3:3: E3 \\377z\\n'\n"
        "exit 1\n"
    },
    "long_unparseable_output": {"ruff": "printf '%0500d\\n' 0\nexit 3\n"},
    "killed_by_signal": {"ruff": "kill -9 $$\n"},
}


@pytest.mark.parametrize("case", SHIM_CASES)
def test_rust_port_matches_python_with_shimmed_tools(
    case, helmsman_cli, fixture_repo, tmp_path, monkeypatch
):
    _bin_dir(tmp_path, monkeypatch, **SHIM_CASES[case])
    assert_parity(helmsman_cli, "a_ruff", fixture_repo)


def test_rust_port_matches_python_on_timeout(
    helmsman_cli, fixture_repo, tmp_path, monkeypatch
):
    _bin_dir(tmp_path, monkeypatch, ruff="exec sleep 10\n")
    monkeypatch.setenv("HELMSMAN_PHASE0_CHECK_S", "21")
    assert_parity(helmsman_cli, "a_ruff", fixture_repo)


@pytest.mark.parametrize("budget", ["nope", "5"])
def test_rust_port_matches_python_on_budget_env(
    budget, helmsman_cli, fixture_repo, tmp_path, monkeypatch
):
    _bin_dir(tmp_path, monkeypatch, ruff="exec sleep 10\n")
    monkeypatch.setenv("HELMSMAN_PHASE0_CHECK_S", budget)
    assert_parity(helmsman_cli, "a_ruff", fixture_repo)


def test_rust_port_matches_python_on_git_failure(helmsman_cli, fixture_repo):
    assert_parity(helmsman_cli, "a_ruff", fixture_repo, base="no-such-ref")


@needs_ruff
def test_rust_port_matches_python_on_own_history(helmsman_cli):
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", "HEAD~1"],
        cwd=CLI_MANIFEST.parents[1],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not head:
        pytest.skip("shallow clone without enough history")
    assert_parity(helmsman_cli, "a_ruff", CLI_MANIFEST.parents[1], base=head)


@pytest.mark.parametrize("uvx", [None, "echo FAKE-UVX-RAN\nexit 1\n"])
def test_rust_port_matches_python_on_non_executable_ruff(
    uvx, helmsman_cli, fixture_repo, tmp_path, monkeypatch
):
    """A ruff on PATH without the execute bit counts as missing, as `shutil.which` does."""
    bin_dir = _bin_dir(tmp_path, monkeypatch, **({"uvx": uvx} if uvx else {}))
    ruff = bin_dir / "ruff"
    ruff.write_text("#!/bin/sh\necho SHOULD-NOT-RUN\nexit 1\n")
    ruff.chmod(0o644)
    assert_parity(helmsman_cli, "a_ruff", fixture_repo)
