import os
import subprocess
from pathlib import Path

import pytest

from .conftest import _git, assert_parity, commit_all, posix_only, skip_shims_on_windows

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = '[project]\nname = "pkg"\nversion = "0"\n'


def _write(repo, rel, body):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body.encode() if isinstance(body, str) else body)


def _mod(body):
    return lambda r: _write(r, "pkg/mod.py", body)


def _deps(extra, body="import somepkg\n"):
    return lambda r: (
        _write(r, "pkg/pyproject.toml", PYPROJECT + extra),
        _write(r, "pkg/mod.py", body),
    )


def _venv_python(repo, script):
    skip_shims_on_windows()
    _write(repo, ".venv/bin/python", script)
    (repo / ".venv" / "bin" / "python").chmod(0o755)


def _repo_env_maps(repo):
    _venv_python(
        repo, '#!/bin/sh\necho \'{"yaml": ["PyYAML"], "bs4": ["beautifulsoup4"]}\'\n'
    )
    _deps('dependencies = ["pyyaml"]\n', "import yaml\nimport bs4\n")(repo)


def _repo_env_broken(repo):
    _venv_python(repo, "#!/bin/sh\necho not json\n")
    _write(repo, "pkg/mod.py", "import pytest\n")


def _no_owning_package(repo):
    (repo / "pkg" / "pyproject.toml").unlink()
    _write(repo, "pkg/mod.py", "import somepkg\n")


def _root_package(repo):
    _write(repo, "pyproject.toml", PYPROJECT + 'dependencies = ["somepkg"]\n')
    _write(repo, "src/app/__init__.py", "")
    _write(repo, "src/app/mod.py", "import somepkg\nimport other_unknown_9\n")


def _two_packages(repo):
    _write(repo, "zeta/pyproject.toml", PYPROJECT)
    _write(repo, "zeta/z.py", "import pytest\nimport aaa_unknown\n")
    _write(repo, "loose/l.py", "import orphan_unknown\n")
    _write(repo, "pkg/mod.py", "import pytest\n")


DEFERRED = "def f():\n    import json\n\n    return json\n"
NESTED = (
    "class C:\n    def m(self):\n        def inner():\n            if x:\n                pass\n"
    "            elif y:\n                import json\n            try:\n                pass\n"
    "            except E:\n                from . import a, b\n"
    "            match v:\n                case 1:\n                    import os.path\n"
    "        import sys; import re\n\n\nasync def g():\n    with x:\n"
    "        from .sib import y\n    for i in z:\n        pass\n    else:\n"
    "        import zzz_unknown\n"
)

PARITY_CASES = {
    "clean": lambda r: None,
    "undeclared": _mod("import pytest\n\n\ndef f():\n    return 1\n"),
    "unmappable": _mod("import zzz_totally_unknown_mod_9999\n"),
    "stdlib_and_local": lambda r: (
        _write(r, "pkg/mod.py", "import os, json\nfrom pkg import mod2\n"),
        _write(r, "pkg/mod2.py", "X = 1\n"),
    ),
    "sibling_module": lambda r: (
        _write(r, "pkg/sub/a.py", "import b\nimport c.d\n"),
        _write(r, "pkg/sub/b.py", ""),
        _write(r, "pkg/sub/c.py", ""),
    ),
    "declared": _deps('dependencies = ["somepkg>=1.0"]\n'),
    "declared_normalized": _deps(
        'dependencies = ["  Some_Pkg[extra]>=1"]\n', "import some_pkg\n"
    ),
    "optional": _deps('[project.optional-dependencies]\ndev = ["somepkg>=1.0"]\n'),
    "dependency_groups": _deps(
        '[dependency-groups]\ntest = ["somepkg>=1.0", {include-group = "x"}]\n'
    ),
    "dynamic": _deps('dynamic = ["dependencies"]\n'),
    "invalid_toml": _deps("dependencies = [\n"),
    "no_project_table": lambda r: (
        _write(r, "pkg/pyproject.toml", "[tool.x]\ny = 1\n"),
        _write(r, "pkg/mod.py", "import somepkg\n"),
    ),
    "no_owning_package": _no_owning_package,
    "root_package": _root_package,
    "two_packages_and_orphans": _two_packages,
    "over_twenty": _mod("".join(f"import unknown_mod_{n:02}\n" for n in range(23))),
    "relative_and_dotted": _mod(
        "from . import x\nfrom .y import z\nimport a.b.c\nfrom d.e import f\n"
    ),
    "deferred": _mod(DEFERRED),
    "deferred_nested": _mod(NESTED),
    "deferred_and_undeclared": _mod("import pytest\n\n\n" + DEFERRED),
    "syntax_error": _mod("import pytest\ndef broken(:\n"),
    "bom": _mod("\ufeffimport pytest\n"),
    "tokenizer_rejects": _mod("import pytest\nif x:\n\ta\n        b\n"),
    "crlf_and_invalid_utf8": _mod(b"import pytest\r\n# \xff\r\n" + DEFERRED.encode()),
    "repo_env_maps": _repo_env_maps,
    "repo_env_broken": _repo_env_broken,
    "space_and_unicode_paths": lambda r: (
        _write(r, "pkg/my mod.py", "import pytest\n"),
        _write(r, "pkg/\u00e9.py", DEFERRED),
    ),
}


def _raw_pyproject(body):
    return lambda r: (
        _write(r, "pkg/pyproject.toml", body),
        _write(r, "pkg/mod.py", "import somepkg\n"),
    )


PYPROJECT_ERROR_CASES = {
    "invalid_start_byte": b'[project]\nname = "\xff"\n',
    "invalid_continuation": b'[project]\nname = "\xe2\x82("\n',
    "surrogate_lead": b"# \xed\xa0\x80\n[project]\n",
    "unexpected_end": b"[project]\n# \xf0\x9f\x98",
    "project_is_int": "project = 1\n",
    "project_is_date": "project = 2024-01-01\n",
    "project_is_datetime": "project = 2024-01-01T00:00:00Z\n",
    "dynamic_is_int": PYPROJECT + "dynamic = 1\n",
    "dynamic_is_time": PYPROJECT + "dynamic = 07:32:00\n",
    "dynamic_is_falsy": PYPROJECT + 'dynamic = 0\ndependencies = ["somepkg"]\n',
    "dynamic_substring": PYPROJECT + 'dynamic = "xdependenciesx"\n',
    "dynamic_table": PYPROJECT + "dynamic = {dependencies = 1}\n",
    "dependencies_is_str": PYPROJECT + 'dependencies = "somepkg"\n',
    "dependencies_is_table": PYPROJECT + "dependencies = {somepkg = 1}\n",
    "dependencies_is_int": PYPROJECT + "dependencies = 5\n",
    "dependencies_mixed_types": PYPROJECT + 'dependencies = [1, "somepkg", [2]]\n',
    "optional_is_list": PYPROJECT + 'optional-dependencies = ["somepkg"]\n',
    "optional_group_is_str": PYPROJECT + 'optional-dependencies = {dev = "s"}\n',
    "optional_group_is_float": PYPROJECT + "optional-dependencies = {dev = 1.5}\n",
    "groups_is_int": "dependency-groups = 1\n" + PYPROJECT,
    "group_is_bool": "dependency-groups = {test = true}\n" + PYPROJECT,
}


@pytest.mark.parametrize("case", PYPROJECT_ERROR_CASES)
def test_rust_port_matches_python_on_odd_pyprojects(case, helmsman_cli, fixture_repo):
    """Undecodable or wrongly typed pyproject fields print the same ERROR or verdict as Python."""
    _raw_pyproject(PYPROJECT_ERROR_CASES[case])(fixture_repo)
    commit_all(fixture_repo)
    assert_parity(helmsman_cli, "f_import_closure", fixture_repo)


@pytest.mark.parametrize("case", PARITY_CASES)
def test_rust_port_matches_python(case, helmsman_cli, fixture_repo):
    """The Rust check prints what the Python check prints, with its exit code, for every case above."""
    PARITY_CASES[case](fixture_repo)
    _git(fixture_repo, "add", "-A")
    _git(fixture_repo, "commit", "--allow-empty", "-m", case)
    assert_parity(helmsman_cli, "f_import_closure", fixture_repo)


def test_pre_existing_deferred_import_matches(helmsman_cli, fixture_repo):
    _write(fixture_repo, "pkg/mod.py", DEFERRED)
    commit_all(fixture_repo)
    _git(fixture_repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _write(fixture_repo, "pkg/mod.py", DEFERRED + "\n\ndef g():\n    return 2\n")
    commit_all(fixture_repo)
    assert_parity(helmsman_cli, "f_import_closure", fixture_repo)


@posix_only
def test_failed_sync_distrusts_the_repo_env(
    helmsman_cli, fixture_repo, tmp_path, monkeypatch
):
    """A lockfile whose sync fails leaves the venv untrusted, so both fall back to the interpreter."""
    bin_dir = tmp_path / "bin"
    _write(bin_dir, "uv", "#!/bin/sh\necho 'error:   lock   out of date' >&2\nexit 1\n")
    (bin_dir / "uv").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    _write(fixture_repo, "uv.lock", "")
    _repo_env_maps(fixture_repo)
    commit_all(fixture_repo)
    assert_parity(helmsman_cli, "f_import_closure", fixture_repo)


@pytest.mark.parametrize("base", ["no-such-ref", "HEAD"])
def test_rust_port_matches_python_on_git_edges(base, helmsman_cli, fixture_repo):
    assert_parity(helmsman_cli, "f_import_closure", fixture_repo, base=base)


def test_rust_port_matches_python_on_bad_budget(
    helmsman_cli, fixture_repo, monkeypatch
):
    monkeypatch.setenv("HELMSMAN_PHASE0_CHECK_S", "nope")
    assert_parity(helmsman_cli, "f_import_closure", fixture_repo)


def test_rust_port_matches_python_on_this_repos_history(helmsman_cli):
    """Smoke: the last commit that touched Python in this repo, checked by both."""
    r = subprocess.run(
        ["git", "log", "-1", "--format=%H", "--", "*.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    head = r.stdout.strip()
    parent = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", f"{head}^"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0 or not head or parent.returncode != 0:
        pytest.skip("no two commits of Python history in this checkout")
    assert_parity(
        helmsman_cli, "f_import_closure", REPO_ROOT, base=parent.stdout.strip()
    )
