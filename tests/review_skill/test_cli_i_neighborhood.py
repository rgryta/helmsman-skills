import subprocess
from pathlib import Path

import pytest

from .conftest import _git, assert_parity, commit_all
from .test_neighborhood import (
    ADJACENT,
    ADJACENT_CHANGED,
    BASE,
    NEW_FUNCTION,
    REBOUND,
    REBOUND_WITH_GATE,
    TWO_FUNCTIONS,
    TWO_FUNCTIONS_GUARDED,
    WITH_GATE,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

DECORATED = """import functools


@functools.cache
@register(
    name="x",
)
async def handler(req):
    audit(req)
    result = await engine(req)
    return result
"""

NESTED = """class Service:
    def outer(self, req):
        prepare(req)

        def inner(x):
            step(x)
            return finish(x)

        return inner(req)
"""

MANY_CALLS = (
    "def busy(x):\n"
    + "".join(f"    c{i}(x)\n" for i in range(12))
    + "    x = x\n"
    + "".join(f"    d{i}(x)\n" for i in range(12))
)

MANY_BINDINGS = (
    "def bound(x):\n"
    + "".join(f"    x = shape{i}(x)\n" for i in range(9))
    + "    use(x)\n"
    + "    return x\n"
)


def _modify(body, needle, insert):
    return body.replace(needle, needle + insert, 1)


def _write(repo, rel, body):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body.encode() if isinstance(body, str) else body)


def _two(first, second, rel="pkg/svc.py"):
    return (lambda r: _write(r, rel, first), lambda r: _write(r, rel, second))


def _callers(r):
    _write(r, "pkg/svc.py", BASE)
    _write(r, "pkg/use.py", "from .svc import handler\n\nhandler(1)\nhandler(2)\n")
    _write(r, "pkg/other.py", "def handler(x):\n    return x\n")
    _write(r, "pkg/notes.txt", "handler(3)\n")
    _write(r, "pkg/blob.py", b"handler(4)\0\n")


def _many_callers(n):
    def setup(r):
        _write(r, "pkg/svc.py", BASE)
        _write(r, "pkg/use.py", "".join(f"handler({i})\n" for i in range(n)))

    return setup


def _many_hunks(r):
    funcs = "".join(
        f"\n\ndef f{i}(x):\n    a{i}(x)\n    b{i}(x)\n    return x\n" for i in range(16)
    )
    _write(r, "pkg/a.py", funcs)
    _write(r, "pkg/b.py", funcs)


def _many_hunks_changed(r):
    funcs = "".join(
        f"\n\ndef f{i}(x):\n    a{i}(x)\n    gate(x)\n    b{i}(x)\n    return x\n"
        for i in range(16)
    )
    _write(r, "pkg/a.py", funcs)
    _write(r, "pkg/b.py", funcs)


MALFORMED = {
    "syntax": "def broken(:\n",
    "tab_error": "def f(x):\n\tpass\n        pass\n",
    "unindent": "def f(x):\n        a(x)\n    b(x)\n",
    "unexpected_indent": "def f(x):\n    a(x)\n        b(x)\n",
    "expected_block": "def f(x):\nreturn x\n",
    "null_byte": "def f(x):\n    a(x)\0\n",
    "bom": "﻿def f(x):\n    a(x)\n    b(x)\n",
    "leading_zero": "def f(x):\n    a(x)\n    return 0777\n",
    "unterminated": "def f(x):\n    a(x)\n    return 'abc\n",
    "assign_to_call": "def f(x):\n    a(x)\n    f() = 1\n",
}

BINARY_CASES = {
    "invalid_utf8": b"def f(x):\n    a(x)\n    b(x)  # \xff\n",
    "truncated_utf8": b"def f(x):\n    a(x)\n    b(x)  # \xe2\x82",
    "bad_continuation": b"def f(x):\n    a(x)\n    b(x)  # \xe2\x82x\n",
    "crlf": BASE.replace("audit(req)\n", "audit(req)\nif_ok(req)\n")
    .replace("\n", "\r\n")
    .encode(),
}

PARITY_CASES = {
    "insertion": _two(BASE, WITH_GATE),
    "rebound": _two(REBOUND, REBOUND_WITH_GATE),
    "two_functions": _two(TWO_FUNCTIONS, TWO_FUNCTIONS_GUARDED),
    "adjacent": _two(ADJACENT, ADJACENT_CHANGED),
    "test_file": _two(BASE, WITH_GATE, rel="tests/test_svc.py"),
    "conftest_file": _two(BASE, WITH_GATE, rel="pkg/conftest.py"),
    "test_dir": _two(BASE, WITH_GATE, rel="test/svc.py"),
    "not_a_test_dir": _two(BASE, WITH_GATE, rel="testing/svc.py"),
    "new_function": _two(BASE, NEW_FUNCTION),
    "no_python": (
        lambda r: _write(r, "README.md", "a\n"),
        lambda r: _write(r, "README.md", "b\n"),
    ),
    "pure_rename": (
        lambda r: _write(r, "pkg/svc.py", WITH_GATE),
        lambda r: (r / "pkg" / "svc.py").rename(r / "pkg" / "moved.py"),
    ),
    "deleted": (
        lambda r: _write(r, "pkg/svc.py", BASE),
        lambda r: (r / "pkg" / "svc.py").unlink(),
    ),
    "decorated_async": _two(
        DECORATED, _modify(DECORATED, "audit(req)\n", "    check(req)\n")
    ),
    "decorator_changed": _two(DECORATED, DECORATED.replace('name="x"', 'name="y"')),
    "nested": _two(NESTED, _modify(NESTED, "step(x)\n", "            guard(x)\n")),
    "module_level": _two(
        "setup()\nx = load()\n", "setup()\nx = load()\nvalidate(x)\nrun(x)\n"
    ),
    "many_calls": _two(MANY_CALLS, MANY_CALLS.replace("    x = x\n", "    y = x\n")),
    "many_bindings": _two(
        MANY_BINDINGS, MANY_BINDINGS.replace("    use(x)\n", "    use(x, x)\n")
    ),
    "callers": (_callers, lambda r: _write(r, "pkg/svc.py", WITH_GATE)),
    "many_callers": (
        _many_callers(8),
        lambda r: _write(r, "pkg/svc.py", WITH_GATE),
    ),
    "too_common_caller": (
        _many_callers(30),
        lambda r: _write(r, "pkg/svc.py", WITH_GATE),
    ),
    "many_hunks": (_many_hunks, _many_hunks_changed),
    **{
        f"malformed_{k}": _two(BASE, v)
        for k, v in {**MALFORMED, **BINARY_CASES}.items()
    },
    "skipped_and_ranked": (
        lambda r: (_write(r, "pkg/svc.py", BASE), _write(r, "pkg/bad.py", "x = 1\n")),
        lambda r: (
            _write(r, "pkg/svc.py", WITH_GATE),
            _write(r, "pkg/bad.py", "def broken(:\n"),
        ),
    ),
}


def _repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    return root


@pytest.mark.parametrize("case", PARITY_CASES)
def test_rust_port_matches_python(case, helmsman_cli, tmp_path):
    """The Rust check prints what the Python check prints, with its exit code, for every case above."""
    repo = _repo(tmp_path)
    first, second = PARITY_CASES[case]
    first(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "--allow-empty", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    second(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "--allow-empty", "-m", case)
    assert_parity(helmsman_cli, "i_neighborhood", repo)


def test_rust_port_matches_python_on_git_failure(helmsman_cli, tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "pkg/svc.py", BASE)
    commit_all(repo)
    assert_parity(helmsman_cli, "i_neighborhood", repo, base="no-such-ref")


def test_rust_port_matches_python_on_bad_budget(helmsman_cli, tmp_path, monkeypatch):
    monkeypatch.setenv("HELMSMAN_PHASE0_CHECK_S", "nope")
    repo = _repo(tmp_path)
    _write(repo, "pkg/svc.py", BASE)
    commit_all(repo)
    assert_parity(helmsman_cli, "i_neighborhood", repo, base="HEAD")


def test_rust_port_matches_python_on_own_history(helmsman_cli):
    """Smoke test over two real commits of this repository."""
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    ).stdout.strip()
    base = subprocess.run(
        ["git", "rev-list", "--max-count=1", "--skip=8", head],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not base:
        pytest.skip("shallow clone without enough history")
    assert_parity(helmsman_cli, "i_neighborhood", REPO_ROOT, base=base)
