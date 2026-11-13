import shutil

import pytest

from .conftest import _git, commit_all
from .test_phase0 import load


def test_import_closure_flags_genuinely_undeclared_import(fixture_repo):
    (fixture_repo / "pkg" / "mod.py").write_text(
        "import pytest\n\n\ndef f():\n    return 1\n"
    )
    commit_all(fixture_repo)
    out = load("f_import_closure", "checks/f_import_closure.py").run(
        str(fixture_repo), "origin/main"
    )
    assert out.startswith("FINDINGS:")
    assert "`pytest` imported in pkg but not declared in its pyproject.toml" in out


def test_import_closure_unmappable_import_is_unverified_not_finding(fixture_repo):
    (fixture_repo / "pkg" / "mod.py").write_text(
        "import zzz_totally_unknown_mod_9999\n\n\ndef f():\n    return 1\n"
    )
    commit_all(fixture_repo)
    out = load("f_import_closure", "checks/f_import_closure.py").run(
        str(fixture_repo), "origin/main"
    )
    assert out.startswith("SKIPPED: 1 import(s) could not be verified")
    assert "could not confidently map" in out
    assert "not declared in its pyproject.toml" not in out
    assert "interpreter running Phase 0, not the reviewed repo's environment" in out
    assert "reported UNVERIFIED rather than as a finding" in out


def test_import_closure_ignores_stdlib_and_local(fixture_repo):
    (fixture_repo / "pkg" / "mod.py").write_text(
        "import os, json\nfrom pkg import mod2\n\n\ndef f():\n    return 1\n"
    )
    (fixture_repo / "pkg" / "mod2.py").write_text("X = 1\n")
    commit_all(fixture_repo)
    out = load("f_import_closure", "checks/f_import_closure.py").run(
        str(fixture_repo), "origin/main"
    )
    assert out == "OK: no third-party imports in changed files"


def test_import_closure_ok_when_declared_in_dependencies(fixture_repo):
    (fixture_repo / "pkg" / "pyproject.toml").write_text(
        '[project]\nname = "pkg"\nversion = "0"\ndependencies = ["somepkg>=1.0"]\n'
    )
    (fixture_repo / "pkg" / "mod.py").write_text(
        "import somepkg\n\n\ndef f():\n    return 1\n"
    )
    commit_all(fixture_repo)
    out = load("f_import_closure", "checks/f_import_closure.py").run(
        str(fixture_repo), "origin/main"
    )
    assert out.startswith("OK: 1 third-party import")


def test_import_closure_ok_when_declared_in_optional_dependencies(fixture_repo):
    (fixture_repo / "pkg" / "pyproject.toml").write_text(
        '[project]\nname = "pkg"\nversion = "0"\n'
        '[project.optional-dependencies]\ndev = ["somepkg>=1.0"]\n'
    )
    (fixture_repo / "pkg" / "mod.py").write_text(
        "import somepkg\n\n\ndef f():\n    return 1\n"
    )
    commit_all(fixture_repo)
    out = load("f_import_closure", "checks/f_import_closure.py").run(
        str(fixture_repo), "origin/main"
    )
    assert out.startswith("OK:")


def test_import_closure_ok_when_declared_in_dependency_groups(fixture_repo):
    (fixture_repo / "pkg" / "pyproject.toml").write_text(
        '[project]\nname = "pkg"\nversion = "0"\n'
        '[dependency-groups]\ntest = ["somepkg>=1.0"]\n'
    )
    (fixture_repo / "pkg" / "mod.py").write_text(
        "import somepkg\n\n\ndef f():\n    return 1\n"
    )
    commit_all(fixture_repo)
    out = load("f_import_closure", "checks/f_import_closure.py").run(
        str(fixture_repo), "origin/main"
    )
    assert out.startswith("OK:")


def test_import_closure_no_owning_package_is_skipped(tmp_path):
    repo = tmp_path / "repo_no_pkg"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "conftest.py").write_text("")
    (repo / "loose").mkdir()
    (repo / "loose" / "mod.py").write_text("def existing(x):\n    return x + 1\n")
    commit_all(repo, "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / "loose" / "mod.py").write_text(
        "import somepkg\n\n\ndef existing(x):\n    return x + 1\n"
    )
    commit_all(repo, "change")
    out = load("f_import_closure", "checks/f_import_closure.py").run(
        str(repo), "origin/main"
    )
    assert out == "SKIPPED: no owning package (pyproject.toml) found for changed files"


def test_import_closure_finds_pyproject_at_git_root(tmp_path):
    repo = tmp_path / "repo_root_pkg"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "root"\nversion = "0"\ndependencies = ["somepkg>=1.0"]\n'
    )
    (repo / "conftest.py").write_text("")
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "src" / "pkg" / "__init__.py").write_text("")
    (repo / "src" / "pkg" / "mod.py").write_text("def existing(x):\n    return x + 1\n")
    commit_all(repo, "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / "src" / "pkg" / "mod.py").write_text(
        "import somepkg\n\n\ndef existing(x):\n    return x + 1\n"
    )
    commit_all(repo, "change")
    out = load("f_import_closure", "checks/f_import_closure.py").run(
        str(repo), "origin/main"
    )
    assert "no owning package" not in out
    assert out.startswith("OK:")


def test_dead_code_check_reports_status(fixture_repo):
    out = load("e_dead_code", "checks/e_dead_code.py").run(
        str(fixture_repo), "origin/main"
    )
    assert out.startswith(("OK:", "FINDINGS:", "SKIPPED:"))


def test_dead_code_broken_tool_output_skips_not_findings(
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
    fake_vulture = bin_dir / "vulture"
    fake_vulture.write_text(
        "#!/bin/sh\necho 'Traceback (most recent call last):'\necho 'ValueError: boom'\nexit 1\n"
    )
    fake_vulture.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    out = load("e_dead_code", "checks/e_dead_code.py").run(
        str(fixture_repo), "origin/main"
    )
    assert out.startswith("SKIPPED:")
    assert "boom" in out


@pytest.mark.skipif(
    not (shutil.which("vulture") or shutil.which("uvx")), reason="no vulture/uvx"
)
def test_dead_code_drops_unused_import_hits_entirely(fixture_repo):
    (fixture_repo / "pkg" / "mod.py").write_text(
        "import os\n\n\ndef f():\n    return 1\n"
    )
    commit_all(fixture_repo)
    out = load("e_dead_code", "checks/e_dead_code.py").run(
        str(fixture_repo), "origin/main"
    )
    assert "unused import '" not in out
    assert out.startswith("OK:")


@pytest.mark.skipif(
    not (shutil.which("vulture") or shutil.which("uvx")), reason="no vulture/uvx"
)
def test_dead_code_still_reports_unused_parameter_hits(fixture_repo):
    (fixture_repo / "pkg" / "mod.py").write_text("def f(unused_param):\n    return 1\n")
    commit_all(fixture_repo)
    out = load("e_dead_code", "checks/e_dead_code.py").run(
        str(fixture_repo), "origin/main"
    )
    assert out.startswith("FINDINGS:")
    assert "unused_param" in out


def test_import_closure_treats_a_sibling_module_as_local(fixture_repo):
    """A module sitting next to the importing file resolves from that directory."""
    (fixture_repo / "pkg" / "helper.py").write_text("VALUE = 1\n")
    (fixture_repo / "pkg" / "mod.py").write_text(
        "import helper\n\n\ndef f():\n    return helper.VALUE\n"
    )
    commit_all(fixture_repo)
    out = load("f_import_closure", "checks/f_import_closure.py").run(
        str(fixture_repo), "origin/main"
    )
    assert out.startswith("OK:") and "helper" not in out


def test_import_closure_maps_imports_with_the_reviewed_repos_env(fixture_repo):
    """A dependency installed only in the repo's own venv resolves, instead of reading as unverified."""
    (fixture_repo / "pkg" / "pyproject.toml").write_text(
        '[project]\nname = "pkg"\nversion = "0"\ndependencies = ["zzz-dist"]\n'
    )
    (fixture_repo / "pkg" / "mod.py").write_text(
        "import zzz_only_in_repo_env\n\n\ndef f():\n    return 1\n"
    )
    commit_all(fixture_repo)
    venv_bin = fixture_repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    python = venv_bin / "python"
    python.write_text('#!/bin/sh\necho \'{"zzz_only_in_repo_env": ["zzz-dist"]}\'\n')
    python.chmod(0o755)
    out = load("f_import_closure", "checks/f_import_closure.py").run(
        str(fixture_repo), "origin/main"
    )
    assert out.startswith("OK: 1 third-party import"), out
    assert "the reviewed repo's env" in out


def test_import_closure_ignores_an_unsynced_repo_env(fixture_repo, monkeypatch):
    """A repo venv whose sync is not verified is not trusted for import mapping."""
    (fixture_repo / "pkg" / "mod.py").write_text(
        "import zzz_only_in_repo_env\n\n\ndef f():\n    return 1\n"
    )
    commit_all(fixture_repo)
    venv_bin = fixture_repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    python = venv_bin / "python"
    python.write_text('#!/bin/sh\necho \'{"zzz_only_in_repo_env": ["zzz-dist"]}\'\n')
    python.chmod(0o755)
    mod = load("f_import_closure", "checks/f_import_closure.py")
    monkeypatch.setattr(
        mod._env,
        "ensure_project_env",
        lambda wt: (str(python), False, "uv sync failed"),
    )
    out = mod.run(str(fixture_repo), "origin/main")
    assert "interpreter running Phase 0" in out, out
