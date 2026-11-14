import subprocess
from pathlib import Path

import pytest

from .conftest import _git, assert_parity, commit_all

REPO_ROOT = Path(__file__).resolve().parents[2]

NOTHING = "def test_thing():\n    result = compute(1)\n    print(result)\n"
NEGATIVE = (
    "def test_thing():\n    code = classify(err)\n    assert code != 'rate_limited'\n"
)


def _write(repo, rel, body):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body.encode() if isinstance(body, str) else body)


def _test(body, rel="tests/test_x.py"):
    return lambda r: _write(r, rel, body)


def _pre_existing_untouched(repo):
    _write(repo, "tests/test_x.py", NOTHING)
    commit_all(repo)
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _write(repo, "tests/test_x.py", NOTHING + "\n\nHELPER = 1\n")


def _rename(repo):
    _write(repo, "tests/test_x.py", NOTHING)
    commit_all(repo)
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "mv", "tests/test_x.py", "tests/test_y.py")


def _unknown_provenance(repo):
    _write(repo, ".gitattributes", "*.py -diff\n")
    _write(repo, "tests/test_x.py", NOTHING)


def _many_files(repo):
    for n in range(16):
        _write(repo, f"tests/test_{n:02}.py", NOTHING)


PARITY_CASES = {
    "asserts_nothing": _test(NOTHING),
    "raises_context": _test(
        "import pytest\n\n\ndef test_thing():\n"
        "    with pytest.raises(ValueError):\n        compute(1)\n"
    ),
    "mock_assert_call": _test(
        "def test_thing(spy):\n    compute(1)\n    spy.assert_called_once_with(1)\n"
    ),
    "all_negative": _test(NEGATIVE),
    "mixed_negative": _test(
        "def test_thing():\n    code = classify(err)\n"
        "    assert code == 'bad_request'\n    assert code != 'rate_limited'\n"
    ),
    "self_comparison": _test(
        "def test_thing():\n    assert FLOOR == FLOOR\n    assert other() != 1\n"
    ),
    "ambient_env": _test(
        "import os\n\n\ndef test_thing():\n    assert os.getenv('WAIT') == '30'\n"
    ),
    "monkeypatched_env": _test(
        "import os\n\n\ndef test_thing(monkeypatch):\n"
        "    monkeypatch.setenv('WAIT', '30')\n"
        "    assert os.getenv('WAIT') == '30'\n"
    ),
    "unnamed_caplog": _test(
        "import logging\n\n\ndef test_thing(caplog):\n"
        "    with caplog.at_level(logging.INFO):\n        emit()\n"
        "    assert 'restaging' not in caplog.text\n"
    ),
    "named_caplog": _test(
        "import logging\n\n\ndef test_thing(caplog):\n"
        "    with caplog.at_level(logging.DEBUG, logger='pkg.mod'):\n        emit()\n"
        "    assert 'restaging' in caplog.text\n"
    ),
    "caplog_variants": _test(
        "def test_a(caplog):\n    with caplog.at_level(*lvl):\n        assert x\n\n"
        "def test_b(caplog):\n    with caplog.at_level(10, 'pkg'):\n        assert x\n\n"
        "def test_c(caplog):\n    with caplog.at_level(10, **kw):\n        assert x\n"
    ),
    "production_file": _test("def test_like():\n    compute(1)\n", "pkg/mod.py"),
    "clean": lambda r: None,
    "pre_existing_untouched": _pre_existing_untouched,
    "pure_rename": _rename,
    "unknown_provenance": _unknown_provenance,
    "file_cap": _test("".join(f"def test_{n}():\n    pass\n\n\n" for n in range(6))),
    "block_cap": _many_files,
    "walk_order": _test(
        "if a:\n    def test_b():\n        pass\nelif c:\n    def test_d():\n        pass\n"
        "else:\n    def test_e():\n        pass\n\n"
        "class TestK:\n    def test_m(self):\n        pass\n\n"
        "def test_f():\n    def test_g():\n        pass\n"
        "try:\n    pass\nexcept E:\n    def test_h():\n        pass\n"
        "match x:\n    case 1:\n        def test_i():\n            pass\n"
    ),
    "tautology_order": _test(
        "def test_t():\n    if a:\n        assert b == b\n    assert c is c\n"
        "    assert True\n    assert 1 == 0x1\n    assert 'a' 'b' == 'ab'\n"
        "    assert (x) == x\n    assert f(1) == f(2)\n    assert y != y\n"
    ),
    "decorated_and_async": _test(
        "import pytest\n\n\n@pytest.mark.parametrize('a', [os.getenv('X')])\n"
        "# note\n@other\nasync def test_a(a):\n    await go()\n\n\n"
        "@deco\ndef \\\n  test_b(x=check()):\n    go()\n"
    ),
    "env_variants": _test(
        "def test_a():\n    assert os.environ['X']\n\n"
        "def test_b():\n    with patch.dict(os.environ, {}):\n        assert os.environ\n\n"
        "def test_c():\n    assert f'{getenv(\"X\")}'\n\n"
        "def test_d():\n    lambda: os.getenv('Y')\n"
    ),
    "case_folding": _test(
        "def test_a():\n    ExPeCt(1)\n\ndef test_b():\n    ver\u0130fy(1)\n\n"
        "def test_c():\n    \u0131nner()\n\ndef test_d():\n    a\u017f\u017fert_x()\n\n"
        "def te\u017ft_e():\n    pass\n"
    ),
    "nested_assert_in_inner_def": _test(
        "def test_a():\n    def inner():\n        assert 1 != 2\n    inner()\n"
    ),
    "test_helpers_in_tests_dir": _test(NOTHING, "tests/helpers.py"),
    "suffix_test_file": _test(NOTHING, "pkg/mod_test.py"),
    "syntax_error": _test("def test_a(:\n    pass\n"),
    "nul_byte": _test(NOTHING + "x = '\0'\n"),
    "bom": _test("\ufeff" + NOTHING),
    "tab_inconsistency": _test(NOTHING + "if x:\n\ta\n        b\n"),
    "bad_number": _test(NOTHING + "y = 1_\n"),
    "deep_nesting": _test(NOTHING + "y = " + "(" * 201 + ")" * 201 + "\n"),
    "nesting_at_limit": _test(NOTHING + "y = " + "(" * 200 + ")" * 200 + "\n"),
    "fstring_nesting": _test(
        NOTHING + 'y = [f"{' + "(" * 199 + "1" + ")" * 199 + '}"]\n'
    ),
    "u_prefix_kinds": _test(
        'def test_u():\n    assert u"a" == "a"\n    assert u"a" == u"a"\n    assert u"a" "b" == "ab"\n    assert "a" u"b" == "ab"\n    assert u"" "a" == u"a"\n    assert (u"a",) == ("a",)\n    assert u"a" f"{x}b" == f"a{x}b"\n    assert u"a" f"{x}" "b" == u"a" f"{x}b"\n    assert f"b" u"a" == f"ba"\n    assert u"" f"{x}" == f"{x}"\n    assert f"" u"a" f"{x}" == u"a" f"{x}"\n    assert f"{x}" u"a" == f"{x}a"\n    assert f"{x:{u}}" == f"{x:{u}}"\n    assert "" f"{x}" "" == f"{x}"\n'
    ),
    "crlf_and_invalid_utf8": _test(
        NOTHING.replace("\n", "\r\n").encode() + b"# \xff\r\n"
    ),
    "form_feed": _test("\x0c" + NOTHING),
    "space_and_unicode_paths": lambda r: (
        _write(r, "tests/test my.py", NOTHING),
        _write(r, "tests/test_\u00e9.py", NOTHING),
    ),
}


@pytest.mark.parametrize("case", PARITY_CASES)
def test_rust_port_matches_python(case, helmsman_cli, fixture_repo):
    """The Rust check prints what the Python check prints, with its exit code, for every case above."""
    PARITY_CASES[case](fixture_repo)
    _git(fixture_repo, "add", "-A")
    _git(fixture_repo, "commit", "--allow-empty", "-m", case)
    assert_parity(helmsman_cli, "l_test_shape", fixture_repo)


def test_rust_port_matches_python_on_bad_budget(
    helmsman_cli, fixture_repo, monkeypatch
):
    monkeypatch.setenv("HELMSMAN_PHASE0_CHECK_S", "nope")
    assert_parity(helmsman_cli, "l_test_shape", fixture_repo)


def test_rust_port_matches_python_on_git_failure(helmsman_cli, fixture_repo):
    assert_parity(helmsman_cli, "l_test_shape", fixture_repo, base="no-such-ref")


def test_rust_port_matches_python_on_own_history(helmsman_cli):
    """Smoke test on two real commits of this repository."""
    first = subprocess.run(
        ["git", "rev-list", "--max-parents=0", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        encoding="utf-8",
        check=True,
    ).stdout.split()[0]
    assert_parity(helmsman_cli, "l_test_shape", REPO_ROOT, base=first)
