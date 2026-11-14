import pytest

from .conftest import CLI_MANIFEST, _git, assert_parity, commit_all
from .test_siblings import BASE_GUARD, TWIN_GUARD

NEW_GUARD = "if is_throttle(exc) and not exhausted(exc):"
COMMON = "    logger.info('starting the configured operation')\n"
MOVED = "def summarise(rows):\n    total = compute_the_total_value(rows)\n    return total\n"
TRIVIAL = "import os\nfrom sys import argv\n\n\ndef f():\n    return (\n        os.sep\n    )\n"


def _budgets(lonely):
    body = "".join(
        f"    value_{i} = configure_component_{i}(settings)\n" for i in range(40)
    )
    files = {f"c{i}.py": f"def f():\n{body}" for i in range(6)}
    files |= {f"u{i}.py": f"def f():\n{body}" for i in range(6)}
    files["a.py"] = "".join(
        f"unique_line_{i} = nothing_else_has_this({i})\n" for i in range(lonely)
    )
    return files, {name: "def f():\n    pass\n" for name in files if name[0] in "ac"}


CASES = {
    "removed_line_survives": (
        {"a.py": BASE_GUARD, "b.py": TWIN_GUARD},
        {"a.py": BASE_GUARD.replace("if is_throttle(exc):", NEW_GUARD)},
    ),
    "changed_at_every_site": (
        {"a.py": BASE_GUARD, "b.py": TWIN_GUARD},
        {
            "a.py": BASE_GUARD.replace("if is_throttle(exc):", NEW_GUARD),
            "b.py": TWIN_GUARD.replace("if is_throttle(exc):", NEW_GUARD),
        },
    ),
    "too_common": (
        {
            **{f"m{i}.py": f"def f{i}():\n{COMMON}    return {i}\n" for i in range(25)},
            "a.py": f"def a():\n{COMMON}    return 0\n",
        },
        {"a.py": "def a():\n    logger.debug('starting the configured operation')\n"},
    ),
    "trivial_lines": ({"a.py": TRIVIAL, "b.py": TRIVIAL}, {"a.py": "def f():\n"}),
    "literal_in_untouched_file": (
        {"a.py": "ACQUIRE_WAIT = 30.0\n", "elsewhere/config.py": "wait = 30.0\n"},
        {"a.py": "ACQUIRE_WAIT = 45.0\n"},
    ),
    "literal_confined_to_changed_files": (
        {"a.py": "ACQUIRE_WAIT = 30.0\n", "b.py": "acquire = 30.0\n"},
        {"a.py": "ACQUIRE_WAIT = 45.0\n", "b.py": "acquire = 45.0\n"},
    ),
    "yaml_twins": (
        {
            "gitops/prod/k.yaml": "patches:\n  - path: service-config.yaml\n",
            "gitops/stg/k.yaml": "patches:\n  - path: service-config.yaml\n",
        },
        {"gitops/prod/k.yaml": "patches: []\n"},
    ),
    "moved_within_file": (
        {
            "a.py": "A = 1\nSHARED_LINE = compute(A, B)\n",
            "b.py": "SHARED_LINE = compute(A, B)\n",
        },
        {"a.py": "SHARED_LINE = compute(A, B)\nA = 1\n"},
    ),
    "moved_across_files": (
        {"pkg/a.py": "import os\n\n\n" + MOVED, "pkg/b.py": "import sys\n"},
        {"pkg/a.py": "import os\n", "pkg/b.py": "import sys\n\n\n" + MOVED},
    ),
    "block_budget": _budgets(0),
    "probe_budget": _budgets(58),
    "skipped_and_binary_files": (
        {
            "a.py": "TOKEN = 'abcdefgh-ijkl'\n",
            "notes.md": "TOKEN = 'abcdefgh-ijkl'\n",
            "node_modules/x.js": "'abcdefgh-ijkl'\n",
            "blob.dat": "\0 'abcdefgh-ijkl'\n",
            "forced.dat": "\0 'abcdefgh-ijkl'\n",
            "hidden.cfg": "'abcdefgh-ijkl'\n",
            ".gitattributes": "forced.dat diff\nhidden.cfg -diff\n",
        },
        {"a.py": "TOKEN = 'zzzzzzzz'\n"},
    ),
    "colon_space_unicode_paths": (
        {
            "a.py": "LIMIT = 12345\n",
            "odd:name.py": "limit = 12345\n",
            "my file.py": "limit = 12345\n",
            "pkg/é.py": "limit = 12345\n",
        },
        {"a.py": "LIMIT = 99999\n"},
    ),
    "form_feed_and_crlf_hits": (
        {
            "a.py": "VALUE = 'distinct-value'\n",
            "b.py": "x = 'distinct-value'\x0cfake:7:tail\r\ny = 1\r\n",
        },
        {"a.py": "VALUE = 'other-value'\n"},
    ),
    "long_line_abbreviated": (
        {"a.py": "x = " + "y" * 120 + "\n", "b.py": "x = " + "y" * 120 + "\n"},
        {"a.py": "x = 1\n"},
    ),
    "unicode_digits": (
        {"a.py": "N = ٣٤٥٦\n", "b.py": "n = ٣٤٥٦\n"},
        {"a.py": "N = 0\n"},
    ),
    "only_deletion": ({"a.py": BASE_GUARD, "b.py": TWIN_GUARD}, {}),
}


def _write(repo, files):
    for name, body in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body.encode())


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    return root


@pytest.mark.parametrize("case", CASES)
def test_rust_port_matches_python(case, helmsman_cli, repo):
    """The Rust check prints what the Python check prints, with its exit code, for every case above."""
    base, change = CASES[case]
    _write(repo, base)
    commit_all(repo, "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    if case == "only_deletion":
        (repo / "a.py").unlink()
    _write(repo, change)
    _git(repo, "add", "-A")
    _git(repo, "commit", "--allow-empty", "-m", case)
    assert_parity(helmsman_cli, "j_siblings", repo)


def test_rust_port_matches_python_on_git_failure(helmsman_cli, repo):
    _write(repo, {"a.py": "x = 1\n"})
    commit_all(repo, "base")
    assert_parity(helmsman_cli, "j_siblings", repo, base="no-such-ref")


def test_rust_port_matches_python_on_bad_budget(helmsman_cli, repo, monkeypatch):
    monkeypatch.setenv("HELMSMAN_PHASE0_CHECK_S", "nope")
    _write(repo, {"a.py": "x = 1\n"})
    commit_all(repo, "base")
    assert_parity(helmsman_cli, "j_siblings", repo, base="HEAD")


def test_rust_port_matches_python_on_own_history(helmsman_cli):
    assert_parity(helmsman_cli, "j_siblings", CLI_MANIFEST.parents[1], base="HEAD~3")
