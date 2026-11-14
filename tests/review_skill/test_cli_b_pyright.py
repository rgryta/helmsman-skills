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

needs_pyright = pytest.mark.skipif(
    not (shutil.which("pyright") or shutil.which("uvx")), reason="no pyright/uvx"
)


def _write(repo, rel, body):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


REAL_PYRIGHT_CASES = {
    "type_error": lambda r: _write(r, "pkg/mod.py", "x: int = 'a'\n"),
    "clean": lambda r: None,
    "unresolved_import_only": lambda r: _write(
        r, "pkg/mod.py", "import helmsman_no_such_module\n"
    ),
    "import_noise_plus_real_error": lambda r: _write(
        r, "pkg/mod.py", "import helmsman_no_such_module\nx: int = 'a'\n"
    ),
    "space_and_unicode_paths": lambda r: (
        _write(r, "pkg/my mod.py", "x: int = 'a'\n"),
        _write(r, "pkg/é.py", "y: str = 1\n"),
    ),
    "non_python_change_only": lambda r: _write(r, "README.md", "hi\n"),
    "deleted_only": lambda r: (r / "pkg" / "mod.py").unlink(),
}


@needs_pyright
@pytest.mark.parametrize("case", REAL_PYRIGHT_CASES)
def test_rust_port_matches_python_with_real_pyright(case, helmsman_cli, fixture_repo):
    """The Rust check prints what the Python check prints, with its exit code, on real pyright output."""
    REAL_PYRIGHT_CASES[case](fixture_repo)
    _git(fixture_repo, "add", "-A")
    _git(fixture_repo, "commit", "--allow-empty", "-m", case)
    assert_parity(helmsman_cli, "b_pyright", fixture_repo)


def _shim(path, body):
    skip_shims_on_windows()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def _bin_dir(tmp_path, monkeypatch, **tools):
    skip_shims_on_windows()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("git", "sh", "sleep"):
        real = shutil.which(name)
        if real:
            (bin_dir / name).symlink_to(real)
    for name, body in tools.items():
        _shim(bin_dir / name, body)
    monkeypatch.setenv("PATH", str(bin_dir))
    return bin_dir


IMPORT_AND_REAL = (
    "echo '  pkg/mod.py:1:8 - error: Import \"yaml\" could not be resolved'\n"
    "echo 'pkg/mod.py:2:1 - warning: unused'\n"
    "echo 'pkg/mod.py:9:5 - error: Cannot assign (reportMissingImports-free)'\n"
    "echo '1 error, 1 warning'\nexit 1\n"
)

SHIM_CASES = {
    "unavailable": ({}, {}),
    "uvx_fallback": ({"uvx": 'echo "$@" 1>&2\nexit 0\n'}, {}),
    "warnings_only": ({"pyright": "echo 'm.py:1:1 - warning: w'\nexit 1\n"}, {}),
    "import_noise_dropped_without_env": ({"pyright": IMPORT_AND_REAL}, {}),
    "import_errors_only_without_env": (
        {"pyright": "echo 'm.py:1:1 - error: x reportMissingImports'\nexit 1\n"},
        {},
    ),
    "over_60_findings": (
        {
            "pyright": 'i=0\nwhile [ $i -lt 90 ]; do echo "m.py:$i:1 - error: e"; '
            "i=$((i+1)); done\nexit 1\n"
        },
        {},
    ),
    "python_line_breaks_and_bad_utf8": (
        {
            "pyright": "printf 'a.py:1:1 - error: x\\fb.py:2:2 - error: y\\302\\205"
            "c.py:3:3 - error: \\377z\\n'\nexit 1\n"
        },
        {},
    ),
    "killed_by_signal": ({"pyright": "kill -9 $$\n"}, {}),
    "pinned_binary_with_own_interpreter": (
        {"pyright": "echo SHOULD-NOT-RUN\nexit 1\n"},
        {".venv/bin/python": "exit 0\n", ".venv/bin/pyright": 'echo "$@"\nexit 0\n'},
    ),
    "venv_without_pinned_pyright": (
        {"pyright": 'echo "a - error: $@"\nexit 1\n'},
        {".venv/bin/python": "exit 0\n"},
    ),
    "synced_env_keeps_import_errors": (
        {"uv": "exit 0\n", "pyright": IMPORT_AND_REAL},
        {"uv.lock": None, ".venv/bin/python": "exit 0\n"},
    ),
    "failed_sync_drops_import_noise": (
        {
            "uv": "echo 'error: no   solution\n found' 1>&2\nexit 1\n",
            "pyright": IMPORT_AND_REAL,
        },
        {"uv.lock": None, ".venv/bin/python": "exit 0\n"},
    ),
    "lockfile_without_uv": ({"pyright": IMPORT_AND_REAL}, {"uv.lock": None}),
    "synced_but_no_venv": (
        {"uv": "exit 0\n", "pyright": IMPORT_AND_REAL},
        {"uv.lock": None},
    ),
}


@pytest.mark.parametrize("case", SHIM_CASES)
def test_rust_port_matches_python_with_shimmed_tools(
    case, helmsman_cli, fixture_repo, tmp_path, monkeypatch
):
    tools, repo_files = SHIM_CASES[case]
    _bin_dir(tmp_path, monkeypatch, **tools)
    for rel, body in repo_files.items():
        if body is None:
            (fixture_repo / rel).write_text("")
        else:
            _shim(fixture_repo / rel, body)
    assert_parity(helmsman_cli, "b_pyright", fixture_repo)


def test_rust_port_matches_python_on_custom_venv_dir(
    helmsman_cli, fixture_repo, tmp_path, monkeypatch
):
    _bin_dir(tmp_path, monkeypatch, pyright="echo SHOULD-NOT-RUN\nexit 1\n")
    _shim(fixture_repo / "env" / "bin" / "python", "exit 0\n")
    _shim(fixture_repo / "env" / "bin" / "pyright", 'echo "$@"\nexit 0\n')
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "env")
    assert_parity(helmsman_cli, "b_pyright", fixture_repo)


@posix_only
def test_rust_port_matches_python_on_non_executable_pinned_pyright(
    helmsman_cli, fixture_repo, tmp_path, monkeypatch
):
    """A pinned pyright without the execute bit counts as missing, and PATH's pyright runs."""
    _bin_dir(tmp_path, monkeypatch, pyright='echo "$@"\nexit 0\n')
    _shim(fixture_repo / ".venv" / "bin" / "python", "exit 0\n")
    pinned = fixture_repo / ".venv" / "bin" / "pyright"
    pinned.write_text("#!/bin/sh\necho SHOULD-NOT-RUN\nexit 1\n")
    pinned.chmod(0o644)
    assert_parity(helmsman_cli, "b_pyright", fixture_repo)


def test_rust_port_matches_python_on_timeout(
    helmsman_cli, fixture_repo, tmp_path, monkeypatch
):
    _bin_dir(tmp_path, monkeypatch, pyright="exec sleep 10\n")
    monkeypatch.setenv("HELMSMAN_PHASE0_CHECK_S", "22")
    assert_parity(helmsman_cli, "b_pyright", fixture_repo)


@pytest.mark.parametrize("budget", ["nope", "5"])
def test_rust_port_matches_python_on_budget_env(
    budget, helmsman_cli, fixture_repo, tmp_path, monkeypatch
):
    _bin_dir(tmp_path, monkeypatch, pyright="exec sleep 10\n")
    monkeypatch.setenv("HELMSMAN_PHASE0_CHECK_S", budget)
    assert_parity(helmsman_cli, "b_pyright", fixture_repo)


def test_rust_port_matches_python_on_git_failure(helmsman_cli, fixture_repo):
    assert_parity(helmsman_cli, "b_pyright", fixture_repo, base="no-such-ref")


@needs_pyright
def test_rust_port_matches_python_on_own_history(helmsman_cli):
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", "HEAD~1"],
        cwd=CLI_MANIFEST.parents[1],
        capture_output=True,
        encoding="utf-8",
    ).stdout.strip()
    if not head:
        pytest.skip("shallow clone without enough history")
    assert_parity(helmsman_cli, "b_pyright", CLI_MANIFEST.parents[1], base=head)
