import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import _git, assert_parity, commit_all

REPO_ROOT = Path(__file__).resolve().parents[2]

ONE_SIDED = "def call(timeout):\n    return run(min(30, timeout))\n"

BFS_ORDER = """\
def outer(a, b, *rest, c, **kw):
    x = g(h(k(min(a, 1))))
    y = min(a, 2)
    g(k=min(b, 1),
      j=0)
    g(max(b, 2))

    def inner(z=min(a, 5)):
        return max(z, 0)

    return [min(c, i) for i in range(min(c, 3)) if min(c, 9)]


def later(p, /, q):
    if p:
        pass
    elif q:
        min(p, 1)
    else:
        min(q, 4)
    return f"{min(p, 2):{max(q, 7)}}", {min(q, 1): 1, 2: g(min(q, 3))}


class Holder:
    async def method(self, v):
        match v:
            case [x] if min(v, 1):
                pass
        return lambda w=min(v, 2): w
"""

DECORATED = """\
def factory(n):
    @deco(max(n, 1))
    def wrapped(m: max(n, 3) = 0) -> min(m, 2):
        return min(m, 5)

    class Inner(Base, meta=min(n, 4)):
        pass

    return wrapped
"""


def _write(repo, rel, body):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body.encode() if isinstance(body, str) else body)


def _pre_existing(repo):
    mod = repo / "pkg" / "mod.py"
    mod.write_text("def call(timeout):\n    return min(30, timeout)\n")
    commit_all(repo)
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    mod.write_text(
        "def call(timeout):\n    return min(30, timeout)  # noqa\n\n\nX = 1\n"
    )


def _deleted(repo):
    (repo / "pkg" / "mod.py").unlink()
    _write(repo, "pkg/other.txt", "x\n")


def _many(repo):
    _write(
        repo,
        "pkg/mod.py",
        "".join(f"def f{n}(a):\n    return min(a, {n})\n\n\n" for n in range(15)),
    )


PARITY_CASES = {
    "one_sided": lambda r: _write(r, "pkg/mod.py", ONE_SIDED),
    "both_sides": lambda r: _write(
        r, "pkg/mod.py", "def call(limit):\n    return max(1, min(limit, 100))\n"
    ),
    "not_a_parameter": lambda r: _write(
        r, "pkg/mod.py", "def call(rows):\n    return min(10, len(rows))\n"
    ),
    "pre_existing": _pre_existing,
    "unparseable": lambda r: _write(r, "pkg/mod.py", "def call(timeout:\n"),
    "unknown_provenance": lambda r: (
        _write(r, ".gitattributes", "*.py -diff\n"),
        _write(r, "pkg/mod.py", ONE_SIDED),
    ),
    "clean": lambda r: None,
    "deleted": _deleted,
    "bfs_order": lambda r: _write(r, "pkg/mod.py", BFS_ORDER),
    "decorators_defaults_annotations": lambda r: _write(r, "pkg/mod.py", DECORATED),
    "more_than_shown": _many,
    "rename_and_space_and_unicode_paths": lambda r: (
        _git(r, "mv", "pkg/mod.py", "pkg/moved.py"),
        _write(r, "pkg/moved.py", "def existing(x):\n    return x + 1\n" + ONE_SIDED),
        _write(r, "pkg/my mod.py", ONE_SIDED),
        _write(r, "pkg/\u00e9.py", ONE_SIDED),
    ),
    "nfkc_identifiers": lambda r: _write(
        r, "pkg/mod.py", "def \ufb01t(\ufb01le):\n    return min(file, 1)\n"
    ),
    "bom": lambda r: _write(r, "pkg/mod.py", "\ufeff" + ONE_SIDED),
    "inner_bom": lambda r: _write(r, "pkg/mod.py", ONE_SIDED + "x = 1\ufeff\n"),
    "nul_byte": lambda r: _write(r, "pkg/mod.py", ONE_SIDED + "\0\n"),
    "crlf_and_invalid_utf8_comment": lambda r: _write(
        r, "pkg/mod.py", b"# \xff\r\n" + ONE_SIDED.replace("\n", "\r\n").encode()
    ),
    "invalid_utf8_in_code": lambda r: _write(
        r, "pkg/mod.py", b"def call(t\xff):\n    return min(t, 1)\n"
    ),
    "parenthesized_and_continued_calls": lambda r: _write(
        r,
        "pkg/mod.py",
        "def a(x):\n    return (\n        min\n    )(x, 1)\n\n\n"
        "def b(y):\n    return \\\n        max(y, 1)\n\n\n"
        "def c(z):\n    return min(\n        z,\n        1)\n",
    ),
    "only_comment_mentions": lambda r: _write(
        r, "pkg/mod.py", "def call(t):\n    # min max\n    return t\n"
    ),
}


@pytest.mark.parametrize("case", PARITY_CASES)
def test_rust_port_matches_python(case, helmsman_cli, fixture_repo):
    """The Rust check prints what the Python check prints, with its exit code, for every case above."""
    PARITY_CASES[case](fixture_repo)
    _git(fixture_repo, "add", "-A")
    _git(fixture_repo, "commit", "--allow-empty", "-m", case)
    assert_parity(helmsman_cli, "m_bounds", fixture_repo)


def test_rust_port_matches_python_with_no_python_changes(helmsman_cli, fixture_repo):
    assert_parity(helmsman_cli, "m_bounds", fixture_repo, base="HEAD")


def test_rust_port_matches_python_on_git_failure(helmsman_cli, fixture_repo):
    assert_parity(helmsman_cli, "m_bounds", fixture_repo, base="no-such-ref")


def test_rust_port_matches_python_on_bad_budget(
    helmsman_cli, fixture_repo, monkeypatch
):
    monkeypatch.setenv("HELMSMAN_PHASE0_CHECK_S", "nope")
    assert_parity(helmsman_cli, "m_bounds", fixture_repo)


def test_bfs_case_really_reports_rows(fixture_repo):
    """Guards the parity cases above against comparing two empty outputs."""
    _write(fixture_repo, "pkg/mod.py", BFS_ORDER)
    commit_all(fixture_repo)
    out = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "plugin/skills/review/scripts/checks/m_bounds.py"),
            str(fixture_repo),
            "origin/main",
        ],
        capture_output=True,
        text=True,
    ).stdout
    assert out.startswith("Bound facts:") and out.count("### ") >= 5


def test_rust_port_matches_python_on_own_history(helmsman_cli):
    if subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--verify", "-q", "HEAD~1"],
        capture_output=True,
    ).returncode:
        pytest.skip("no parent commit in this checkout")
    assert_parity(helmsman_cli, "m_bounds", REPO_ROOT, base="HEAD~1")
