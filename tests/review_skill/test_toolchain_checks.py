import os
import shutil
import subprocess
import sys
import time

import pytest

from .conftest import _git, commit_all, gone_within, posix_only, skip_shims_on_windows
from .test_phase0 import load


def test_run_cmd_sentinels(tmp_path):
    d = load("_diff", "checks/_diff.py")
    assert d.run_cmd(["definitely-not-a-real-binary-xyz"], str(tmp_path), 5) == (
        -2,
        "MISSING",
    )
    assert d.run_cmd(["sleep", "3"], str(tmp_path), 0.2) == (-1, "TIMEOUT")
    code, out = d.run_cmd(["echo", "hi"], str(tmp_path), 5)
    assert code == 0 and out == "hi"


def test_changed_py_files_and_git_failure(fixture_repo, tmp_path):
    d = load("_diff", "checks/_diff.py")
    assert d.changed_py_files(str(fixture_repo), "origin/main") == ["pkg/mod.py"]
    with pytest.raises(RuntimeError):
        d.changed_py_files(str(fixture_repo), "not-a-ref")


@pytest.mark.skipif(
    not (shutil.which("ruff") or shutil.which("uvx")), reason="no ruff/uvx"
)
def test_ruff_reports_new_violation(fixture_repo):
    (fixture_repo / "pkg" / "mod.py").write_text(
        "import os\n\n\ndef added(y):\n    return y * 2\n"
    )
    commit_all(fixture_repo)
    out = load("a_ruff", "checks/a_ruff.py").run(str(fixture_repo), "origin/main")
    assert out.startswith("FINDINGS:") and "F401" in out


@pytest.mark.skipif(
    not (shutil.which("ruff") or shutil.which("uvx")), reason="no ruff/uvx"
)
def test_ruff_reports_a_file_that_does_not_parse(fixture_repo):
    (fixture_repo / "pkg" / "mod.py").write_text("def added(y:\n    return y\n")
    commit_all(fixture_repo)
    out = load("a_ruff", "checks/a_ruff.py").run(str(fixture_repo), "origin/main")
    assert out.startswith("FINDINGS:") and "invalid-syntax" in out, out


def test_ruff_line_parser_accepts_paths_with_spaces():
    pattern = load("a_ruff", "checks/a_ruff.py")._VIOLATION_LINE
    assert pattern.match("my dir/mod.py:1:8: F401 [*] `os` imported but unused")
    assert not pattern.match("Found 1 error.")


def test_ruff_line_parser_accepts_older_ruff_syntax_errors():
    pattern = load("a_ruff", "checks/a_ruff.py")._VIOLATION_LINE
    assert pattern.match("m.py:1:13: SyntaxError: Expected ')', found newline")


@posix_only
def test_uvx_fallback_fires_when_primary_missing(fixture_repo, tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git_shim = bin_dir / "git"
    git_shim.write_text("#!/bin/sh\necho pkg/mod.py\n")
    git_shim.chmod(0o755)
    sh_real = shutil.which("sh")
    if sh_real:
        (bin_dir / "sh").symlink_to(sh_real)
    fake = bin_dir / "uvx"
    fake.write_text("#!/bin/sh\necho FAKE-UVX-RAN\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    out = load("a_ruff", "checks/a_ruff.py").run(str(fixture_repo), "origin/main")
    assert "FAKE-UVX-RAN" in out


@posix_only
def test_ruff_broken_output_skips_not_findings(fixture_repo, tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git_shim = bin_dir / "git"
    git_shim.write_text("#!/bin/sh\necho pkg/mod.py\n")
    git_shim.chmod(0o755)
    sh_real = shutil.which("sh")
    if sh_real:
        (bin_dir / "sh").symlink_to(sh_real)
    fake_ruff = bin_dir / "ruff"
    fake_ruff.write_text(
        "#!/bin/sh\n"
        "echo 'ruff failed' 1>&2\n"
        "echo '  Cause: Failed to parse pyproject.toml' 1>&2\n"
        "exit 2\n"
    )
    fake_ruff.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    out = load("a_ruff", "checks/a_ruff.py").run(str(fixture_repo), "origin/main")
    assert out.startswith("SKIPPED:")
    assert "ruff failed" in out


@posix_only
def test_pyright_no_error_lines_skips_empty_findings(
    fixture_repo, tmp_path, monkeypatch
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git_shim = bin_dir / "git"
    git_shim.write_text("#!/bin/sh\necho pkg/mod.py\n")
    git_shim.chmod(0o755)
    sh_real = shutil.which("sh")
    if sh_real:
        (bin_dir / "sh").symlink_to(sh_real)
    fake_pyright = bin_dir / "pyright"
    fake_pyright.write_text(
        "#!/bin/sh\necho pkg/mod.py:1:1 - warning: Some warning only\nexit 1\n"
    )
    fake_pyright.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    out = load("b_pyright", "checks/b_pyright.py").run(str(fixture_repo), "origin/main")
    assert out.startswith("SKIPPED:")


@pytest.mark.skipif(
    not (shutil.which("pytest") or shutil.which("uv")), reason="no pytest and no uv"
)
def test_pytest_check_green_package(fixture_repo):
    (fixture_repo / "pkg" / "test_mod.py").write_text(
        "from pkg.mod import added\n\n\ndef test_added():\n    assert added(2) == 4\n"
    )
    commit_all(fixture_repo)
    out = load("c_pytest", "checks/c_pytest.py").run(str(fixture_repo), "origin/main")
    assert out.startswith("OK:")


@pytest.mark.skipif(
    not (shutil.which("pytest") or shutil.which("uv")), reason="no pytest and no uv"
)
def test_pytest_says_so_when_no_test_maps_to_the_change(fixture_repo):
    """An unverified change must not read as a green tick."""
    out = load("c_pytest", "checks/c_pytest.py").run(str(fixture_repo), "origin/main")
    assert out.startswith("SKIPPED:")
    assert "no test file maps" in out
    assert "pkg/mod.py" in out


@posix_only
def test_pytest_infra_failure_is_skipped_not_fail(fixture_repo, tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git_shim = bin_dir / "git"
    git_shim.write_text("#!/bin/sh\necho pkg/mod.py\n")
    git_shim.chmod(0o755)
    sh_real = shutil.which("sh")
    if sh_real:
        (bin_dir / "sh").symlink_to(sh_real)
    fake_pytest = bin_dir / "pytest"
    fake_pytest.write_text(
        "#!/bin/sh\n"
        "echo 'docker.errors.DockerException: Error while fetching server API version'\n"
        "exit 1\n"
    )
    fake_pytest.chmod(0o755)
    (fixture_repo / "pkg" / "test_mod.py").write_text("def test_x():\n    pass\n")
    monkeypatch.setenv("PATH", str(bin_dir))
    out = load("c_pytest", "checks/c_pytest.py").run(str(fixture_repo), "origin/main")
    assert out.startswith("SKIPPED: 1 of 1 package(s) did not run")
    assert "test runner infra unavailable: 1), none ran" in out
    assert "SKIPPED (test runner infra unavailable" in out


@posix_only
def test_pytest_bare_connection_refused_is_not_runner_infra(
    fixture_repo, tmp_path, monkeypatch
):
    """A refusal the code under test raised must not qualify for the infra-only carve-out."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git_shim = bin_dir / "git"
    git_shim.write_text("#!/bin/sh\necho pkg/mod.py\n")
    git_shim.chmod(0o755)
    sh_real = shutil.which("sh")
    if sh_real:
        (bin_dir / "sh").symlink_to(sh_real)
    fake_pytest = bin_dir / "pytest"
    fake_pytest.write_text(
        "#!/bin/sh\n"
        "echo 'ConnectionRefusedError: [Errno 61] Connection refused'\n"
        "exit 1\n"
    )
    fake_pytest.chmod(0o755)
    (fixture_repo / "pkg" / "test_mod.py").write_text("def test_x():\n    pass\n")
    monkeypatch.setenv("PATH", str(bin_dir))
    out = load("c_pytest", "checks/c_pytest.py").run(str(fixture_repo), "origin/main")
    assert out.startswith("SKIPPED: 1 of 1 package(s) did not run")
    assert "SKIPPED (connection refused, infra or code unverified" in out
    assert "test runner infra unavailable" not in out


@posix_only
def test_pytest_hypothesis_health_check_is_load_not_a_failure(
    fixture_repo, tmp_path, monkeypatch
):
    """A too-slow health check under machine load is a skipped run, never a FAIL."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git_shim = bin_dir / "git"
    git_shim.write_text("#!/bin/sh\necho pkg/mod.py\n")
    git_shim.chmod(0o755)
    sh_real = shutil.which("sh")
    if sh_real:
        (bin_dir / "sh").symlink_to(sh_real)
    fake_pytest = bin_dir / "pytest"
    fake_pytest.write_text(
        "#!/bin/sh\n"
        "echo 'hypothesis.errors.FailedHealthCheck: Data generation is extremely slow'\n"
        "echo 'add HealthCheck.too_slow to the suppress_health_check settings'\n"
        "exit 1\n"
    )
    fake_pytest.chmod(0o755)
    (fixture_repo / "pkg" / "test_mod.py").write_text("def test_x():\n    pass\n")
    monkeypatch.setenv("PATH", str(bin_dir))
    out = load("c_pytest", "checks/c_pytest.py").run(str(fixture_repo), "origin/main")
    assert out.startswith("SKIPPED: 1 of 1 package(s) did not run")
    assert "SKIPPED (load-induced health check, infra not code" in out
    assert "FAIL" not in out


@posix_only
def test_pytest_app_deadline_exceeded_stays_a_failure(
    fixture_repo, tmp_path, monkeypatch
):
    """An app-level DeadlineExceeded is a real failure, never load-induced."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git_shim = bin_dir / "git"
    git_shim.write_text("#!/bin/sh\necho pkg/mod.py\n")
    git_shim.chmod(0o755)
    sh_real = shutil.which("sh")
    if sh_real:
        (bin_dir / "sh").symlink_to(sh_real)
    fake_pytest = bin_dir / "pytest"
    fake_pytest.write_text(
        "#!/bin/sh\n"
        "echo 'google.api_core.exceptions.DeadlineExceeded: 504 Deadline Exceeded'\n"
        "exit 1\n"
    )
    fake_pytest.chmod(0o755)
    (fixture_repo / "pkg" / "test_mod.py").write_text("def test_x():\n    pass\n")
    monkeypatch.setenv("PATH", str(bin_dir))
    out = load("c_pytest", "checks/c_pytest.py").run(str(fixture_repo), "origin/main")
    assert "load-induced" not in out
    assert "FAIL" in out


@pytest.mark.skipif(
    not (shutil.which("pytest") or shutil.which("uv")), reason="no pytest and no uv"
)
def test_packages_finds_pyproject_at_git_root(tmp_path):
    repo = tmp_path / "repo3"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "pyproject.toml").write_text('[project]\nname = "root"\nversion = "0"\n')
    (repo / "conftest.py").write_text(
        "import sys\nfrom pathlib import Path\n\n"
        "sys.path.insert(0, str(Path(__file__).parent / 'src'))\n"
    )
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "src" / "pkg" / "__init__.py").write_text("")
    (repo / "src" / "pkg" / "mod.py").write_text("def existing(x):\n    return x + 1\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_mod.py").write_text(
        "from pkg.mod import existing\n\n\ndef test_existing():\n    assert existing(1) == 2\n"
    )
    commit_all(repo, "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / "src" / "pkg" / "mod.py").write_text(
        "def existing(x):\n    return x + 1\n\n\ndef added(y):\n    return y * 2\n"
    )
    commit_all(repo, "change")
    out = load("c_pytest", "checks/c_pytest.py").run(str(repo), "origin/main")
    assert "no owning package" not in out
    assert out.startswith("OK:")


def _import_repo(tmp_path, before, after):
    root = tmp_path / "repo"
    root.mkdir()
    g = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(
        [*g, "init", "-b", "main"], cwd=root, check=True, capture_output=True
    )
    (root / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0"\n')
    (root / "m.py").write_text(before)
    subprocess.run([*g, "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run([*g, "commit", "-m", "b"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        [*g, "update-ref", "refs/remotes/origin/main", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    (root / "m.py").write_text(after)
    subprocess.run([*g, "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run([*g, "commit", "-m", "c"], cwd=root, check=True, capture_output=True)
    return root


def test_a_deferred_import_the_diff_adds_is_reported(tmp_path):
    """House style is module level; a new one inside a function is worth justifying."""
    repo = _import_repo(
        tmp_path,
        "def f():\n    return 1\n",
        "def f():\n    import json\n\n    return json\n",
    )
    out = load("f_import_closure", "checks/f_import_closure.py").run(
        str(repo), "origin/main"
    )
    assert "Deferred imports" in out
    assert "imports `json`" in out


def test_a_deferred_import_in_a_nested_function_is_listed_once(tmp_path):
    repo = _import_repo(
        tmp_path,
        "def f():\n    return 1\n",
        "def f():\n    def inner():\n        import json\n\n        return json\n\n    return inner\n",
    )
    out = load("f_import_closure", "checks/f_import_closure.py").run(
        str(repo), "origin/main"
    )
    assert out.count("imports `json`") == 1, out
    assert "in `inner` imports `json`" in out


def test_a_pre_existing_deferred_import_is_not_reported(tmp_path):
    """A repo can carry many deliberate ones; only what this diff adds is in scope."""
    body = "def f():\n    import json\n\n    return json\n"
    repo = _import_repo(tmp_path, body, body + "\n\ndef g():\n    return 2\n")
    out = load("f_import_closure", "checks/f_import_closure.py").run(
        str(repo), "origin/main"
    )
    assert "Deferred imports" not in out


def test_a_module_level_import_the_diff_adds_is_not_reported(tmp_path):
    """The rule is about placement, not about adding imports at all."""
    repo = _import_repo(
        tmp_path,
        "def f():\n    return 1\n",
        "import json\n\n\ndef f():\n    return json\n",
    )
    out = load("f_import_closure", "checks/f_import_closure.py").run(
        str(repo), "origin/main"
    )
    assert "Deferred imports" not in out


def test_deferred_imports_are_framed_as_candidates(tmp_path):
    """Cycle-breaking and post-patch imports are legitimate, so this is not a verdict."""
    repo = _import_repo(
        tmp_path,
        "def f():\n    return 1\n",
        "def f():\n    import json\n\n    return json\n",
    )
    out = load("f_import_closure", "checks/f_import_closure.py").run(
        str(repo), "origin/main"
    )
    assert "CANDIDATE" in out
    assert "breaks a cycle" in out


def _shim(bin_dir, name, body):
    skip_shims_on_windows()
    p = bin_dir / name
    p.write_text(body)
    p.chmod(0o755)
    return p


def _isolated_bin(tmp_path, monkeypatch):
    skip_shims_on_windows()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _shim(bin_dir, "git", "#!/bin/sh\necho pkg/mod.py\n")
    sh_real = shutil.which("sh")
    if sh_real:
        (bin_dir / "sh").symlink_to(sh_real)
    monkeypatch.setenv("PATH", str(bin_dir))
    return bin_dir


def _fake_venv(repo, with_pyright=None):
    skip_shims_on_windows()
    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    _shim(venv_bin, "python", "#!/bin/sh\nexit 0\n")
    if with_pyright:
        _shim(venv_bin, "pyright", with_pyright)
    return venv_bin


def test_pyright_runs_the_worktree_pinned_binary_against_its_own_interpreter(
    fixture_repo, tmp_path, monkeypatch
):
    """The worktree's own env is what pyright must resolve imports against."""
    _isolated_bin(tmp_path, monkeypatch)
    argv = tmp_path / "argv.txt"
    _fake_venv(fixture_repo, f'#!/bin/sh\necho "$@" > {argv}\nexit 0\n')
    out = load("b_pyright", "checks/b_pyright.py").run(str(fixture_repo), "origin/main")
    assert out.startswith("OK:")
    assert f"--pythonpath {fixture_repo}/.venv/bin/python" in argv.read_text()


def test_pyright_env_bootstrap_never_rewrites_the_lockfile(
    fixture_repo, tmp_path, monkeypatch
):
    """Syncing a reviewed worktree must not mutate the branch's uv.lock."""
    bin_dir = _isolated_bin(tmp_path, monkeypatch)
    (fixture_repo / "uv.lock").write_text("")
    argv = tmp_path / "uv-argv.txt"
    _shim(bin_dir, "uv", f'#!/bin/sh\necho "$@" > {argv}\nexit 0\n')
    _fake_venv(fixture_repo)
    _shim(bin_dir, "pyright", "#!/bin/sh\nexit 0\n")
    load("b_pyright", "checks/b_pyright.py").run(str(fixture_repo), "origin/main")
    assert argv.read_text().split() == ["sync", "--frozen"]


def test_pyright_import_errors_are_findings_once_the_env_is_real(
    fixture_repo, tmp_path, monkeypatch
):
    """A synced env makes an unresolved import a defect, not environment noise."""
    bin_dir = _isolated_bin(tmp_path, monkeypatch)
    (fixture_repo / "uv.lock").write_text("")
    _shim(bin_dir, "uv", "#!/bin/sh\nexit 0\n")
    _fake_venv(fixture_repo)
    _shim(
        bin_dir,
        "pyright",
        "#!/bin/sh\n"
        "echo 'pkg/mod.py:1:8 - error: Import \"yaml\" could not be resolved'\n"
        "exit 1\n",
    )
    out = load("b_pyright", "checks/b_pyright.py").run(str(fixture_repo), "origin/main")
    assert out.startswith("FINDINGS:") and 'Import "yaml"' in out


def test_pyright_drops_import_noise_when_the_sync_failed(
    fixture_repo, tmp_path, monkeypatch
):
    """An unverified env still reports real errors, without its import noise."""
    bin_dir = _isolated_bin(tmp_path, monkeypatch)
    (fixture_repo / "uv.lock").write_text("")
    _shim(bin_dir, "uv", "#!/bin/sh\necho 'error: no solution found' 1>&2\nexit 1\n")
    _fake_venv(fixture_repo)
    _shim(
        bin_dir,
        "pyright",
        "#!/bin/sh\n"
        "echo 'pkg/mod.py:1:8 - error: Import \"yaml\" could not be resolved'\n"
        "echo 'pkg/mod.py:9:5 - error: Cannot assign to type \"int\"'\n"
        "exit 1\n",
    )
    out = load("b_pyright", "checks/b_pyright.py").run(str(fixture_repo), "origin/main")
    assert out.startswith("FINDINGS:")
    assert "Cannot assign" in out and 'Import "yaml"' not in out
    assert "dropped 1 import-resolution line(s)" in out
    assert "uv sync failed" in out


def test_pyright_skips_when_there_is_no_env_to_build(
    fixture_repo, tmp_path, monkeypatch
):
    """Without a lockfile there is nothing to sync, so the skip names that."""
    bin_dir = _isolated_bin(tmp_path, monkeypatch)
    _shim(
        bin_dir,
        "pyright",
        "#!/bin/sh\n"
        "echo 'pkg/mod.py:1:8 - error: Import \"yaml\" could not be resolved'\n"
        "exit 1\n",
    )
    out = load("b_pyright", "checks/b_pyright.py").run(str(fixture_repo), "origin/main")
    assert out.startswith("SKIPPED:") and "no uv.lock" in out


def test_pytest_runs_only_the_tests_named_for_the_change(
    fixture_repo, tmp_path, monkeypatch
):
    """The whole point: a monorepo package suite never fits one check's budget."""
    bin_dir = _isolated_bin(tmp_path, monkeypatch)
    argv = tmp_path / "pytest-argv.txt"
    _shim(bin_dir, "pytest", f'#!/bin/sh\npwd > {argv}\necho "$@" >> {argv}\nexit 0\n')
    (fixture_repo / "pkg" / "test_mod.py").write_text("def test_x():\n    pass\n")
    (fixture_repo / "pkg" / "test_unrelated.py").write_text("def test_y():\n    pass\n")
    out = load("c_pytest", "checks/c_pytest.py").run(str(fixture_repo), "origin/main")
    assert out.startswith("OK:")
    cwd, args = argv.read_text().splitlines()
    assert cwd == str(fixture_repo), cwd
    assert "pkg/test_mod.py" in args
    assert "test_unrelated" not in args


def test_pytest_ignores_same_named_tests_inside_installed_packages(tmp_path):
    """A venv or node_modules copy of test_<stem>.py is not this repo's test."""
    (tmp_path / "test_mod.py").write_text("")
    for vendored in (".venv/lib/site-packages/dep", "node_modules/dep", "build/lib"):
        (tmp_path / vendored).mkdir(parents=True)
        (tmp_path / vendored / "test_mod.py").write_text("")
    found = load("c_pytest", "checks/c_pytest.py")._mapped_tests(tmp_path, "pkg/mod.py")
    assert found == {tmp_path / "test_mod.py"}


def test_pytest_selects_a_changed_test_file_itself(fixture_repo, tmp_path, monkeypatch):
    bin_dir = _isolated_bin(tmp_path, monkeypatch)
    _shim(bin_dir, "git", "#!/bin/sh\necho pkg/test_thing.py\n")
    argv = tmp_path / "pytest-argv.txt"
    _shim(bin_dir, "pytest", f'#!/bin/sh\necho "$@" > {argv}\nexit 0\n')
    (fixture_repo / "pkg" / "test_thing.py").write_text("def test_z():\n    pass\n")
    out = load("c_pytest", "checks/c_pytest.py").run(str(fixture_repo), "origin/main")
    assert out.startswith("OK:")
    assert "test_thing.py" in argv.read_text()


def test_pytest_stops_starting_packages_once_the_budget_is_spent(
    fixture_repo, tmp_path, monkeypatch
):
    """Phase 0 kills the check at its own deadline, so the check must yield first."""
    bin_dir = _isolated_bin(tmp_path, monkeypatch)
    _shim(bin_dir, "pytest", "#!/bin/sh\nexit 0\n")
    (fixture_repo / "pkg" / "test_mod.py").write_text("def test_x():\n    pass\n")
    mod = load("c_pytest", "checks/c_pytest.py")
    monkeypatch.setattr(mod, "BUDGET_S", -1)
    out = mod.run(str(fixture_repo), "origin/main")
    assert out.startswith("SKIPPED: 1 of 1 package(s) did not run")
    assert "budget spent before it ran" in out


def _second_package(fixture_repo, bin_dir):
    pkg2 = fixture_repo / "pkg2"
    pkg2.mkdir()
    (pkg2 / "__init__.py").write_text("")
    (pkg2 / "pyproject.toml").write_text('[project]\nname = "pkg2"\nversion = "0"\n')
    (pkg2 / "mod.py").write_text("X = 1\n")
    (pkg2 / "test_mod.py").write_text("def test_x():\n    pass\n")
    (fixture_repo / "pkg" / "test_mod.py").write_text("def test_x():\n    pass\n")
    _shim(bin_dir, "git", "#!/bin/sh\necho pkg/mod.py\necho pkg2/mod.py\n")


def test_pytest_header_is_skipped_when_one_package_never_ran(
    fixture_repo, tmp_path, monkeypatch
):
    """A package that collected nothing is a ceiling on the whole section, not a pass."""
    bin_dir = _isolated_bin(tmp_path, monkeypatch)
    _second_package(fixture_repo, bin_dir)
    _shim(bin_dir, "pytest", '#!/bin/sh\ncase "$1" in pkg2/*) exit 5;; esac\nexit 0\n')
    out = load("c_pytest", "checks/c_pytest.py").run(str(fixture_repo), "origin/main")
    assert out.startswith(
        "SKIPPED: 1 of 2 package(s) did not run "
        "(selection collected no tests: 1), the 1 that ran passed -"
    )
    assert "pkg (1 test file(s)): OK" in out
    assert "pkg2 (1 test file(s)): SKIPPED (selection collected no tests)" in out


def test_pytest_header_is_skipped_when_a_package_timed_out(
    fixture_repo, tmp_path, monkeypatch
):
    bin_dir = _isolated_bin(tmp_path, monkeypatch)
    _shim(bin_dir, "pytest", "#!/bin/sh\nexit 0\n")
    (fixture_repo / "pkg" / "test_mod.py").write_text("def test_x():\n    pass\n")
    mod = load("c_pytest", "checks/c_pytest.py")
    monkeypatch.setattr(mod._diff, "run_cmd", lambda *a, **k: (-1, ""))
    out = mod.run(str(fixture_repo), "origin/main")
    assert out.startswith(
        "SKIPPED: 1 of 1 package(s) did not run (timed out: 1), none ran -"
    )
    assert "SKIPPED (timed out after" in out


def test_pytest_failure_outranks_a_skipped_sibling(fixture_repo, tmp_path, monkeypatch):
    bin_dir = _isolated_bin(tmp_path, monkeypatch)
    _second_package(fixture_repo, bin_dir)
    _shim(bin_dir, "pytest", '#!/bin/sh\ncase "$1" in pkg2/*) exit 5;; esac\nexit 1\n')
    out = load("c_pytest", "checks/c_pytest.py").run(str(fixture_repo), "origin/main")
    assert out.startswith("FINDINGS:")
    assert "FAIL (exit 1)" in out and "SKIPPED (selection collected no tests)" in out


def test_pytest_gate_scope_widens_to_the_directories_of_the_matched_tests(
    fixture_repo, tmp_path, monkeypatch
):
    """A sibling test broken in the same directory is what a gate must catch."""
    bin_dir = _isolated_bin(tmp_path, monkeypatch)
    argv = tmp_path / "pytest-argv.txt"
    _shim(bin_dir, "pytest", f'#!/bin/sh\necho "$@" > {argv}\nexit 0\n')
    (fixture_repo / "pkg" / "test_mod.py").write_text("def test_x():\n    pass\n")
    monkeypatch.setenv("HELMSMAN_PYTEST_SCOPE", "gate")
    out = load("c_pytest", "checks/c_pytest.py").run(str(fixture_repo), "origin/main")
    assert out.startswith("OK:") and "gate scope" in out
    assert argv.read_text().split() == ["pkg", "-q", "-x"]


def test_pytest_names_the_workspace_member_it_runs(fixture_repo, tmp_path, monkeypatch):
    """One shared workspace env, synced from the root first so the root's own
    dev group (where a monorepo declares pytest) is present before --package
    narrows the run to the member whose sources have to be importable."""
    bin_dir = _isolated_bin(tmp_path, monkeypatch)
    argv = tmp_path / "uv-argv.txt"
    _shim(bin_dir, "uv", f'#!/bin/sh\necho "$@" >> {argv}\nexit 0\n')
    (fixture_repo / "pkg" / "test_mod.py").write_text("def test_x():\n    pass\n")
    (fixture_repo / "uv.lock").write_text("")
    load("c_pytest", "checks/c_pytest.py").run(str(fixture_repo), "origin/main")
    calls = [line.split() for line in argv.read_text().splitlines()]
    assert calls[0] == ["sync", "--frozen"]
    assert calls[1][:5] == ["run", "--no-sync", "--package", "pkg", "pytest"]


_SPAWN_FAIL = (
    "echo 'error: Failed to spawn: `pytest`' 1>&2\n"
    "echo '  Caused by: No such file or directory (os error 2)' 1>&2\n"
    "exit 2\n"
)


def test_pytest_falls_through_when_the_member_runner_cannot_spawn(
    fixture_repo, tmp_path, monkeypatch
):
    """A member's closure without pytest is uv failing to start the runner, not
    the tests failing: the root-level runner runs next and its verdict stands."""
    bin_dir = _isolated_bin(tmp_path, monkeypatch)
    argv = tmp_path / "uv-argv.txt"
    _shim(
        bin_dir,
        "uv",
        f'#!/bin/sh\necho "$@" >> {argv}\n'
        f'case "$*" in *--package*) {_SPAWN_FAIL};; esac\nexit 0\n',
    )
    (fixture_repo / "pkg" / "test_mod.py").write_text("def test_x():\n    pass\n")
    (fixture_repo / "uv.lock").write_text("")
    out = load("c_pytest", "checks/c_pytest.py").run(str(fixture_repo), "origin/main")
    assert out.startswith("OK:"), out
    assert "FAIL" not in out
    calls = [line.split() for line in argv.read_text().splitlines()]
    assert calls[-2][:5] == ["run", "--no-sync", "--package", "pkg", "pytest"]
    assert calls[-1][:3] == ["run", "--no-sync", "pytest"]


def test_pytest_a_runner_that_never_spawns_is_a_skip_that_says_why(
    fixture_repo, tmp_path, monkeypatch
):
    bin_dir = _isolated_bin(tmp_path, monkeypatch)
    _shim(bin_dir, "uv", f'#!/bin/sh\ncase "$1" in sync) exit 0;; esac\n{_SPAWN_FAIL}')
    (fixture_repo / "pkg" / "test_mod.py").write_text("def test_x():\n    pass\n")
    (fixture_repo / "uv.lock").write_text("")
    out = load("c_pytest", "checks/c_pytest.py").run(str(fixture_repo), "origin/main")
    assert out.startswith("SKIPPED: pytest unavailable"), out
    assert "Failed to spawn" in out and "FAIL (" not in out


def test_pytest_prefers_the_synced_worktree_binary_over_a_path_one(
    fixture_repo, tmp_path, monkeypatch
):
    """A pytest on PATH belongs to some other env; the worktree's own runner
    resolves the diff's imports against the code under review."""
    bin_dir = _isolated_bin(tmp_path, monkeypatch)
    argv = tmp_path / "which.txt"
    _shim(bin_dir, "pytest", f"#!/bin/sh\necho path > {argv}\nexit 0\n")
    _fake_venv(fixture_repo)
    _shim(
        fixture_repo / ".venv" / "bin",
        "pytest",
        f"#!/bin/sh\necho venv > {argv}\nexit 0\n",
    )
    (fixture_repo / "pkg" / "test_mod.py").write_text("def test_x():\n    pass\n")
    out = load("c_pytest", "checks/c_pytest.py").run(str(fixture_repo), "origin/main")
    assert out.startswith("OK:"), out
    assert argv.read_text().strip() == "venv"


def test_pytest_packages_over_the_cap_count_as_skipped(
    fixture_repo, tmp_path, monkeypatch
):
    """A package the cap left out never ran, so it is a ceiling like any other skip."""
    bin_dir = _isolated_bin(tmp_path, monkeypatch)
    _second_package(fixture_repo, bin_dir)
    _shim(bin_dir, "pytest", "#!/bin/sh\nexit 0\n")
    mod = load("c_pytest", "checks/c_pytest.py")
    monkeypatch.setattr(mod, "MAX_PKGS", 1)
    out = mod.run(str(fixture_repo), "origin/main")
    assert out.startswith(
        "SKIPPED: 1 of 2 package(s) did not run "
        "(over the package cap: 1), the 1 that ran passed -"
    )
    assert "pkg2: SKIPPED (over the package cap of 1)" in out
    assert "NOTE:" not in out


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _slow_sync(mod, monkeypatch, sync_s):
    clock = _Clock()
    monkeypatch.setattr(mod.time, "monotonic", clock)

    def sync(worktree, timeout):
        clock.now += sync_s
        return None, False, "slow sync"

    monkeypatch.setattr(mod._env, "ensure_project_env", sync)
    timeouts = []

    def run_cmd(args, cwd, timeout):
        timeouts.append(timeout)
        return 0, "1 passed"

    monkeypatch.setattr(mod._diff, "run_cmd", run_cmd)
    return timeouts


def test_pytest_gets_only_what_the_sync_left_under_the_phase0_cap(
    fixture_repo, tmp_path, monkeypatch
):
    """Phase 0 kills the whole check at its cap, so a slow sync shrinks the run
    instead of pushing it past the kill and losing every package's result."""
    _isolated_bin(tmp_path, monkeypatch)
    (fixture_repo / "pkg" / "test_mod.py").write_text("def test_x():\n    pass\n")
    mod = load("c_pytest", "checks/c_pytest.py")
    timeouts = _slow_sync(mod, monkeypatch, sync_s=150)
    out = mod.run(str(fixture_repo), "origin/main")
    assert out.startswith("OK:"), out
    assert timeouts == [mod.BUDGET_S - 150]
    assert mod.BUDGET_S < 240


def test_pytest_reports_instead_of_starting_a_run_the_cap_would_kill(
    fixture_repo, tmp_path, monkeypatch
):
    _isolated_bin(tmp_path, monkeypatch)
    (fixture_repo / "pkg" / "test_mod.py").write_text("def test_x():\n    pass\n")
    mod = load("c_pytest", "checks/c_pytest.py")
    timeouts = _slow_sync(mod, monkeypatch, sync_s=mod.BUDGET_S - 5)
    out = mod.run(str(fixture_repo), "origin/main")
    assert timeouts == []
    assert "budget spent before it ran" in out


def test_pytest_budget_follows_the_phase0_cap(monkeypatch):
    monkeypatch.setenv("HELMSMAN_PHASE0_CHECK_S", "270")
    assert load("c_pytest", "checks/c_pytest.py").BUDGET_S == 250


def test_pytest_runs_the_project_runner_first_and_says_so(
    fixture_repo, tmp_path, monkeypatch
):
    bin_dir = _isolated_bin(tmp_path, monkeypatch)
    argv = tmp_path / "runner-argv.txt"
    _shim(bin_dir, "fastrun", f'#!/bin/sh\necho "$@" > {argv}\nexit 0\n')
    _shim(bin_dir, "pytest", "#!/bin/sh\nexit 1\n")
    (fixture_repo / "pkg" / "test_mod.py").write_text("def test_x():\n    pass\n")
    monkeypatch.setenv("HELMSMAN_PYTEST_RUNNER", "fastrun --workers 4 --")
    out = load("c_pytest", "checks/c_pytest.py").run(str(fixture_repo), "origin/main")
    assert out.startswith("OK:"), out
    assert "via HELMSMAN_PYTEST_RUNNER" in out
    assert argv.read_text().split() == [
        "--workers",
        "4",
        "--",
        "pkg/test_mod.py",
        "-q",
        "-x",
    ]


def test_pytest_project_runner_runs_inside_the_workspace_member_env(
    fixture_repo, tmp_path, monkeypatch
):
    bin_dir = _isolated_bin(tmp_path, monkeypatch)
    argv = tmp_path / "uv-argv.txt"
    _shim(bin_dir, "uv", f'#!/bin/sh\necho "$@" >> {argv}\nexit 0\n')
    (fixture_repo / "pkg" / "test_mod.py").write_text("def test_x():\n    pass\n")
    (fixture_repo / "uv.lock").write_text("")
    monkeypatch.setenv("HELMSMAN_PYTEST_RUNNER", "python -m fastrun run --")
    load("c_pytest", "checks/c_pytest.py").run(str(fixture_repo), "origin/main")
    calls = [line.split() for line in argv.read_text().splitlines()]
    assert calls[1][:9] == [
        "run",
        "--no-sync",
        "--package",
        "pkg",
        "python",
        "-m",
        "fastrun",
        "run",
        "--",
    ]


def test_pytest_falls_back_to_pytest_when_the_project_runner_is_missing(
    fixture_repo, tmp_path, monkeypatch
):
    bin_dir = _isolated_bin(tmp_path, monkeypatch)
    _shim(bin_dir, "pytest", "#!/bin/sh\nexit 0\n")
    (fixture_repo / "pkg" / "test_mod.py").write_text("def test_x():\n    pass\n")
    monkeypatch.setenv("HELMSMAN_PYTEST_RUNNER", "not-installed-runner --")
    out = load("c_pytest", "checks/c_pytest.py").run(str(fixture_repo), "origin/main")
    assert out.startswith("OK:"), out
    assert "via HELMSMAN_PYTEST_RUNNER" not in out


def test_run_cmd_timeout_kills_the_runners_own_children(tmp_path, monkeypatch):
    """A parallel runner's workers must not keep burning CPU after its timeout."""
    sleep_real = shutil.which("sleep")
    bin_dir = _isolated_bin(tmp_path, monkeypatch)
    (bin_dir / "sleep").symlink_to(sleep_real)
    pid_file = tmp_path / "child.pid"
    _shim(bin_dir, "spawner", f"#!/bin/sh\nsleep 30 &\necho $! > {pid_file}\nwait\n")
    d = load("_diff", "checks/_diff.py")
    assert d.run_cmd(["spawner"], str(tmp_path), 3) == (-1, "TIMEOUT")
    child = int(pid_file.read_text())
    assert _gone_within(child, seconds=5)


def _gone_within(pid, seconds):
    """A killed orphan stays a zombie until init reaps it."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_run_cmd_timeout_kills_the_tools_whole_process_tree(tmp_path):
    """A timed-out tool's own children die with it: a process group on POSIX, taskkill /T on Windows."""
    pid_file = tmp_path / "grandchild.pid"
    sleeper = "import time; time.sleep(60)"
    spawner = (
        "import pathlib, subprocess, sys, time\n"
        f"p = subprocess.Popen([sys.executable, '-c', {sleeper!r}])\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid))\n"
        "time.sleep(60)\n"
    )
    d = load("_diff", "checks/_diff.py")
    assert d.run_cmd([sys.executable, "-c", spawner], str(tmp_path), 5) == (
        -1,
        "TIMEOUT",
    )
    assert gone_within(int(pid_file.read_text()), seconds=5)
