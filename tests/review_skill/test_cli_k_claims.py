import os
import subprocess

import pytest

from .conftest import _git, assert_parity, commit_all
from .test_claims import GATE_AFTER, GATE_BEFORE

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    return root


def _write(repo, files):
    for name, body in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body.encode() if isinstance(body, str) else body)


def _seed_change(before, after):
    def apply(repo):
        _write(repo, before)
        commit_all(repo, "base")
        _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        _write(repo, after)

    return apply


NESTED = '''import os


@decorator(
    "x",
)
def outer(a, *rest, key=None, **kw):
    """Calls inner_helper and os.path.join; MISSING_FLAG is stale."""
    # routes through shared_router unless SOME_LIMIT trips the fallback
    if a:
        def in_if():
            """Uses deep_one_name here."""
            return a
    elif rest:
        def in_elif():
            """Uses deep_two_name here."""
            return rest
    else:
        def in_else():
            """Uses deep_three_name here."""
            return kw
    return os.path.join(f"{a=} inner_helper {key:{width}>10}", "x" "y")


class Holder:
    """Holder of holder_state, see `Holder.method` and `missing.thing()`."""

    async def method(self, value):
        """Awaits remote_fetch then stores value; no_op, e.g. etc."""
        try:
            pass
        except ValueError:
            def handler_fn():
                """Mentions handler_only_name."""
        return value
'''

ELSEWHERE = '''def stated(labels):
    """Both selectors sit in one entry; _validated_peer enforces the pairing."""
    # _validated_peer appears in this comment, which is prose and not a definition
    return {"podSelector": labels}


def _validated_peer(peer):
    """_validated_peer is named here too."""
    return set(peer) <= {"podSelector"}
'''

MANY = "".join(
    f'def f{n}(x):\n    """Calls helper_number_{n} and CONST_{n}_X."""\n    return x\n\n\n'
    for n in range(15)
)

PARITY_CASES = {
    "gate_names_another_provider": _seed_change(
        {"a.py": GATE_BEFORE, "other.py": "LLAMA_CPP = 'llama'\n"},
        {"a.py": GATE_AFTER},
    ),
    "all_resolve": _seed_change(
        {"a.py": GATE_BEFORE},
        {
            "a.py": 'def compaction_gate(provider):\n    """Returns True only for AWS_BEDROCK."""\n    return provider == AWS_BEDROCK\n'
        },
    ),
    "elsewhere_in_same_file": _seed_change(
        {"a.py": "def stated(labels):\n    return labels\n"}, {"a.py": ELSEWHERE}
    ),
    "nowhere_on_continuation_line": _seed_change(
        {"a.py": "def f(x):\n    return x\n"},
        {
            "a.py": 'def f(x):\n    """Handle the deprecated path.\n\n    Mirrors legacy_scope_resolver, which owns the same fallback.\n    """\n    return x\n'
        },
    ),
    "untouched_docstring": _seed_change(
        {
            "a.py": 'def old(x):\n    """Delegates to vanished_helper."""\n    return x\n\n\ndef g(y):\n    return y\n'
        },
        {
            "a.py": 'def old(x):\n    """Delegates to vanished_helper."""\n    return x\n\n\ndef g(y):\n    return y + 1\n'
        },
    ),
    "pure_rename": lambda r: (
        _seed_change({"a.py": GATE_AFTER, "other.py": "LLAMA_CPP = 1\n"}, {})(r),
        _git(r, "mv", "a.py", "moved.py"),
    ),
    "nested_decorated_async_elif_except": _seed_change(
        {"pkg/mod.py": "x = 1\n", "pkg/other.py": "def inner_helper():\n    pass\n"},
        {"pkg/mod.py": NESTED},
    ),
    "block_cap": _seed_change({"m.py": "x = 1\n"}, {"m.py": MANY}),
    "syntax_error_skipped": _seed_change(
        {"a.py": "x = 1\n"},
        {"a.py": 'def f(x):\n    """Names broken_helper."""\n    return (x\n'},
    ),
    "bom_skipped": _seed_change(
        {"a.py": "x = 1\n"},
        {"a.py": "\ufeff" + 'def f(x):\n    """Names bom_helper."""\n    return x\n'},
    ),
    "crlf_and_invalid_utf8": _seed_change(
        {"a.py": "x = 1\n", "b.py": b"crlf_helper = 1\r\n# \xff crlf_helper\r\n"},
        {"a.py": b'def f(x):\r\n    """Names crlf_helper \xff."""\r\n    return x\r\n'},
    ),
    "form_feed_shifts_comment_lines": _seed_change(
        {"a.py": "x = 1\n"},
        {
            "a.py": "def f(x):\n    y = 1\x0c\n    # comment names ff_helper_name here\n    return x\n"
        },
    ),
    "binary_and_attributes": _seed_change(
        {
            "a.py": "x = 1\n",
            "bin.dat": b"\x00 bin_helper_name\n",
            "txt.dat": b"\x00 attr_text_name\n",
            "nodiff.txt": "attr_text_name nodiff\n",
            ".gitattributes": "txt.dat diff\nnodiff.txt -diff\n",
        },
        {
            "a.py": 'def f(x):\n    """Names bin_helper_name and attr_text_name."""\n    return x\n'
        },
    ),
    "space_and_unicode_paths": _seed_change(
        {
            "a.py": "x = 1\n",
            "my dir/hit file.py": "spaced_name = 1\n",
            "pkg/é.py": "spaced_name = 2\nunicode_name = 1\n",
        },
        {
            "a.py": 'def f(x):\n    """Names spaced_name and unicode_name."""\n    return x\n',
            "my dir/new one.py": 'def g(x):\n    """Names spaced_name."""\n    return x\n',
        },
    ),
    "prose_lines_filtered_in_other_files": _seed_change(
        {
            "a.py": "x = 1\n",
            "b.py": '# filtered_name in a comment\n    """filtered_name docstring"""\nfiltered_name = 1\n',
        },
        {"a.py": 'def f(x):\n    """Names filtered_name."""\n    return x\n'},
    ),
    "many_hits_truncated": _seed_change(
        {
            "a.py": "x = 1\n",
            **{f"h{n}.py": f"hit_name_many = {n}\n" for n in range(6)},
        },
        {"a.py": 'def f(x):\n    """Names hit_name_many."""\n    return x\n'},
    ),
    "symlink_not_searched": lambda r: (
        _seed_change(
            {"a.py": "x = 1\n", "real.txt": "linked_name\n"},
            {"a.py": 'def f(x):\n    """Names linked_name."""\n    return x\n'},
        )(r),
        os.symlink("real.txt", r / "link.txt"),
    ),
    "string_literal_resolves": _seed_change(
        {"a.py": "def t():\n    assert True\n"},
        {
            "a.py": 'def t():\n    """Pins that await_layers is named."""\n    assert "await_layers.py <outdir>" in BLOCK\n'
        },
    ),
    "no_python_files": _seed_change({"a.txt": "x\n"}, {"a.txt": "y\n"}),
}


@pytest.mark.parametrize("case", PARITY_CASES)
def test_rust_port_matches_python(case, helmsman_cli, repo):
    """The Rust check prints what the Python check prints, with its exit code, for every case above."""
    PARITY_CASES[case](repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "--allow-empty", "-m", case)
    assert_parity(helmsman_cli, "k_claims", repo)


def test_rust_port_matches_python_on_git_failure(helmsman_cli, repo):
    _seed_change({"a.py": "x = 1\n"}, {})(repo)
    assert_parity(helmsman_cli, "k_claims", repo, base="no-such-ref")


def test_rust_port_matches_python_on_bad_budget(helmsman_cli, repo, monkeypatch):
    monkeypatch.setenv("HELMSMAN_PHASE0_CHECK_S", "nope")
    _seed_change({"a.py": "x = 1\n"}, {})(repo)
    assert_parity(helmsman_cli, "k_claims", repo)


def test_rust_port_matches_python_on_own_history(helmsman_cli):
    """Smoke test on real commits: this repository's last few commits."""
    head = subprocess.run(
        ["git", "rev-parse", "HEAD~3"],
        cwd=REPO_ROOT,
        capture_output=True,
        encoding="utf-8",
    )
    if head.returncode != 0:
        pytest.skip("shallow clone without enough history")
    assert_parity(helmsman_cli, "k_claims", REPO_ROOT, base=head.stdout.strip())
