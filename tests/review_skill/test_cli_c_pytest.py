import shutil
import subprocess
import sys

import pytest

from .conftest import (
    CLI_MANIFEST,
    SCRIPTS,
    _git,
    assert_parity,
    commit_all,
    posix_only,
    skip_shims_on_windows,
)

ECHO_OK = '#!/bin/sh\nfor i in 1 2 3 4 5 6 7 8; do echo "line $i $*"; done\nexit 0\n'
SPAWN_FAIL = (
    "echo 'error: Failed to spawn: `pytest`' 1>&2\n"
    "echo '  Caused by: No such file or directory (os error 2)' 1>&2\n"
    "exit 2\n"
)


def _write(repo, rel, body=""):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def _bin(tmp_path, monkeypatch, **tools):
    """A PATH holding only git, sh and the given shims, so no real pytest or uv can run."""
    skip_shims_on_windows()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name in ("git", "sh", "sleep", "kill"):
        real = shutil.which(name)
        if real and not (bin_dir / name).exists():
            (bin_dir / name).symlink_to(real)
    for name, body in tools.items():
        exe = bin_dir / name
        exe.write_text(body)
        exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    return bin_dir


def _test_mod(r):
    _write(r, "pkg/test_mod.py", "def test_x():\n    pass\n")


def _pkg(r, name, files=("mod.py", "test_mod.py")):
    _write(r, f"{name}/pyproject.toml", f'[project]\nname = "{name}"\nversion = "0"\n')
    for f in files:
        _write(r, f"{name}/{f}", f"# {name}\n")


def _shim_pytest(body):
    return {"pytest": "#!/bin/sh\n" + body}


SHIMMED_CASES = {
    "no_test_maps": (lambda r: None, _shim_pytest("exit 0\n")),
    "green_with_long_output": (_test_mod, {"pytest": ECHO_OK}),
    "failure": (_test_mod, _shim_pytest("echo 'E   assert 1 == 2'\nexit 1\n")),
    "collected_nothing": (_test_mod, _shim_pytest("exit 5\n")),
    "docker_infra": (
        _test_mod,
        _shim_pytest("echo 'docker.errors.DockerException: x'\nexit 1\n"),
    ),
    "health_check_load": (
        _test_mod,
        _shim_pytest("echo 'add HealthCheck.too_slow'\nexit 1\n"),
    ),
    "connection_refused": (
        _test_mod,
        _shim_pytest("echo 'ConnectionRefusedError: Connection refused'\nexit 1\n"),
    ),
    "killed_by_signal": (_test_mod, _shim_pytest("kill -9 $$\n")),
    "stderr_only_output": (_test_mod, _shim_pytest("echo oops 1>&2\nexit 1\n")),
    "pytest_missing": (_test_mod, {}),
    "changed_test_file_itself": (
        lambda r: _write(r, "pkg/sub/test_thing.py", "def test_z():\n    pass\n"),
        {"pytest": ECHO_OK},
    ),
    "underscore_test_suffix": (
        lambda r: _write(r, "pkg/deep/mod_test.py", "# t\n"),
        {"pytest": ECHO_OK},
    ),
    "vendored_and_symlinked_tests_ignored": (
        lambda r: (
            [
                _write(r, f"pkg/{d}/test_mod.py")
                for d in (".venv/lib", "node_modules/x", "build", "dist", ".tox")
            ],
            _write(r, "outside/test_mod.py"),
            (r / "pkg" / "linked").symlink_to(r / "outside"),
            _write(r, "pkg/other.py", "# o\n"),
        ),
        {"pytest": ECHO_OK},
    ),
    "glob_chars_in_module_name": (
        lambda r: (
            _write(r, "pkg/m[o]d.py", "# g\n"),
            _write(r, "pkg/test_mod.py"),
            _write(r, "pkg/test_m[o]d.py"),
        ),
        {"pytest": ECHO_OK},
    ),
    "two_packages_ordered_by_touch": (
        lambda r: (_pkg(r, "aaa"), _test_mod(r), _write(r, "pkg/extra.py", "# e\n")),
        _shim_pytest('case "$1" in aaa/*) exit 5;; esac\necho ran "$@"\nexit 0\n'),
    ),
    "failure_outranks_skip": (
        lambda r: (_pkg(r, "aaa"), _test_mod(r)),
        _shim_pytest('case "$1" in aaa/*) exit 5;; esac\nexit 1\n'),
    ),
    "over_the_package_cap": (
        lambda r: [_pkg(r, f"p{n}") for n in range(9)],
        {"pytest": ECHO_OK},
    ),
    "many_unmapped": (
        lambda r: (
            [_write(r, f"pkg/u{n:02}.py", "# u\n") for n in range(11)],
            _test_mod(r),
        ),
        {"pytest": ECHO_OK},
    ),
    "root_package": (
        lambda r: (
            _write(r, "pyproject.toml", '[project]\nname = "root"\nversion = "0"\n'),
            _write(r, "src/app/thing.py", "# t\n"),
            _write(r, "tests/test_thing.py"),
        ),
        {"pytest": ECHO_OK},
    ),
    "unicode_and_space_paths": (
        lambda r: (
            _write(r, "pkg/my mod.py", "# s\n"),
            _write(r, "pkg/test_my mod.py"),
            _write(r, "pkg/é.py", "# u\n"),
            _write(r, "pkg/test_é.py"),
        ),
        {"pytest": ECHO_OK},
    ),
    "no_owning_package": (
        lambda r: ((r / "pkg" / "pyproject.toml").unlink(), _test_mod(r)),
        {"pytest": ECHO_OK},
    ),
}


@pytest.mark.parametrize("case", SHIMMED_CASES)
def test_rust_port_matches_python(
    case, helmsman_cli, fixture_repo, tmp_path, monkeypatch
):
    """The Rust check prints what the Python check prints, with its exit code, for every case above."""
    setup, tools = SHIMMED_CASES[case]
    setup(fixture_repo)
    _git(fixture_repo, "add", "-A")
    _git(fixture_repo, "commit", "--allow-empty", "-m", case)
    _bin(tmp_path, monkeypatch, **tools)
    assert_parity(helmsman_cli, "c_pytest", fixture_repo)


@pytest.mark.parametrize("scope", ["gate", "diff", "GATE"])
def test_rust_port_matches_python_on_scope(
    scope, helmsman_cli, fixture_repo, tmp_path, monkeypatch
):
    """Gate scope widens to the outermost directories of the matched tests."""
    for rel in ("pkg/test_mod.py", "pkg/sub/test_mod.py", "pkg/z/y/mod_test.py"):
        _write(fixture_repo, rel)
    _write(fixture_repo, "test_mod.py")
    commit_all(fixture_repo)
    _bin(tmp_path, monkeypatch, pytest=ECHO_OK)
    monkeypatch.setenv("HELMSMAN_PYTEST_SCOPE", scope)
    assert_parity(helmsman_cli, "c_pytest", fixture_repo)


RUNNER_CASES = {
    "runner_first": ("fastrun --workers 4 --", {"fastrun": ECHO_OK}),
    "runner_missing": ("not-installed-runner --", {"pytest": ECHO_OK}),
    "runner_is_pytest": ("pytest", {"pytest": ECHO_OK}),
    "runner_quoted": ("fastrun 'a b' \"c\\\"d\" ''", {"fastrun": ECHO_OK}),
    "unclosed_quote": ("fastrun 'a", {"fastrun": ECHO_OK}),
    "dangling_escape": ("fastrun \\", {"fastrun": ECHO_OK}),
    "blank": ("   ", {"pytest": ECHO_OK}),
}


@pytest.mark.parametrize("case", RUNNER_CASES)
def test_rust_port_matches_python_on_project_runner(
    case, helmsman_cli, fixture_repo, tmp_path, monkeypatch
):
    runner, tools = RUNNER_CASES[case]
    _test_mod(fixture_repo)
    commit_all(fixture_repo)
    _bin(tmp_path, monkeypatch, **tools)
    monkeypatch.setenv("HELMSMAN_PYTEST_RUNNER", runner)
    assert_parity(helmsman_cli, "c_pytest", fixture_repo)


UV_RECORD = '#!/bin/sh\necho "uv $*"\ncase "$1" in sync) exit 0;; esac\n'
WORKSPACE_CASES = {
    "member_runs": ({}, UV_RECORD + "exit 0\n", None),
    "member_spawn_fail_falls_through": (
        {},
        UV_RECORD + f'case "$*" in *--package*) {SPAWN_FAIL};; esac\nexit 0\n',
        None,
    ),
    "never_spawns": ({}, UV_RECORD + SPAWN_FAIL, None),
    "sync_fails": ({}, "#!/bin/sh\necho 'error:  lock\n  stale' 1>&2\nexit 1\n", None),
    "runner_in_member": (
        {"HELMSMAN_PYTEST_RUNNER": "python -m fastrun run --"},
        UV_RECORD + "exit 0\n",
        None,
    ),
    "name_is_int": ({}, UV_RECORD + "exit 0\n", "[project]\nname = 5\n"),
    "name_is_empty": ({}, UV_RECORD + "exit 0\n", '[project]\nname = ""\n'),
    "name_missing": ({}, UV_RECORD + "exit 0\n", "[project]\nversion = '1'\n"),
    "project_is_int": ({}, UV_RECORD + "exit 0\n", "project = 1\n"),
    "invalid_toml": ({}, UV_RECORD + "exit 0\n", "[project\n"),
}


@pytest.mark.parametrize("case", WORKSPACE_CASES)
def test_rust_port_matches_python_in_a_workspace(
    case, helmsman_cli, fixture_repo, tmp_path, monkeypatch
):
    env, uv, pyproject = WORKSPACE_CASES[case]
    _test_mod(fixture_repo)
    _write(fixture_repo, "uv.lock")
    if pyproject is not None:
        _write(fixture_repo, "pkg/pyproject.toml", pyproject)
    commit_all(fixture_repo)
    _bin(tmp_path, monkeypatch, uv=uv)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert_parity(helmsman_cli, "c_pytest", fixture_repo)


def test_rust_port_matches_python_with_a_workspace_name_and_no_uv(
    helmsman_cli, fixture_repo, tmp_path, monkeypatch
):
    """A non-string member name only raises once uv is there to be spawned."""
    _test_mod(fixture_repo)
    _write(fixture_repo, "uv.lock")
    _write(fixture_repo, "pkg/pyproject.toml", "[project]\nname = [1]\n")
    commit_all(fixture_repo)
    _bin(tmp_path, monkeypatch, pytest=ECHO_OK)
    assert_parity(helmsman_cli, "c_pytest", fixture_repo)


@posix_only
def test_rust_port_matches_python_with_the_pinned_venv_pytest(
    helmsman_cli, fixture_repo, tmp_path, monkeypatch
):
    _test_mod(fixture_repo)
    commit_all(fixture_repo)
    venv_bin = fixture_repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    for name, body in (("python", "#!/bin/sh\nexit 0\n"), ("pytest", ECHO_OK)):
        (venv_bin / name).write_text(body)
        (venv_bin / name).chmod(0o755)
    _bin(tmp_path, monkeypatch, pytest="#!/bin/sh\necho path\nexit 1\n")
    assert_parity(helmsman_cli, "c_pytest", fixture_repo)


@posix_only
@pytest.mark.parametrize("budget", ["nope", "30", "45"])
def test_rust_port_matches_python_on_budget(
    budget, helmsman_cli, fixture_repo, tmp_path, monkeypatch
):
    """30 leaves no run budget at all; 45 starts one run that the shim outlives."""
    _test_mod(fixture_repo)
    commit_all(fixture_repo)
    _bin(tmp_path, monkeypatch, pytest="#!/bin/sh\nexec sleep 60\n")
    monkeypatch.setenv("HELMSMAN_PHASE0_CHECK_S", budget)
    assert_parity(helmsman_cli, "c_pytest", fixture_repo)


@pytest.mark.parametrize("base", ["no-such-ref", "HEAD"])
def test_rust_port_matches_python_on_git_edges(base, helmsman_cli, fixture_repo):
    assert_parity(helmsman_cli, "c_pytest", fixture_repo, base=base)


def test_rust_port_matches_python_on_a_relative_worktree(
    helmsman_cli, fixture_repo, tmp_path, monkeypatch
):
    _test_mod(fixture_repo)
    _write(fixture_repo, "pyproject.toml", '[project]\nname = "r"\n')
    _write(fixture_repo, "lone.py", "# l\n")
    _write(fixture_repo, "test_lone.py")
    commit_all(fixture_repo)
    _bin(tmp_path, monkeypatch, pytest=ECHO_OK)
    monkeypatch.setenv("HELMSMAN_PYTEST_SCOPE", "gate")
    script = str(SCRIPTS / "checks" / "c_pytest.py")
    for wt in (".", "./", "../repo"):
        cwd = fixture_repo if wt != "../repo" else fixture_repo / "pkg"
        py = subprocess.run(
            [sys.executable, script, wt, "origin/main"],
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
        )
        rs = subprocess.run(
            [str(helmsman_cli), "check", "c_pytest", wt, "origin/main"],
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
        )
        assert (rs.stdout, rs.returncode) == (py.stdout, py.returncode), wt


def test_rust_port_matches_python_on_own_history(helmsman_cli, tmp_path, monkeypatch):
    """Smoke on real history with a recording pytest, so no real suite runs."""
    repo = CLI_MANIFEST.parents[1]
    base = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", "HEAD~5"],
        cwd=repo,
        capture_output=True,
        encoding="utf-8",
    ).stdout.strip()
    if not base:
        pytest.skip("shallow clone without enough history")
    _bin(tmp_path, monkeypatch, pytest=ECHO_OK)
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(tmp_path / "no-venv"))
    assert_parity(helmsman_cli, "c_pytest", repo, base=base)
