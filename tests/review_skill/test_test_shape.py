import pytest

from .conftest import _git, commit_all
from .test_phase0 import load


def check():
    return load("l_test_shape", "checks/l_test_shape.py")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    return root


def seed(repo, files):
    for name, body in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    commit_all(repo, "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")


def change(repo, files):
    for name, body in files.items():
        (repo / name).write_text(body)
    commit_all(repo, "change")


def run_on(repo, body, path="tests/test_x.py"):
    seed(repo, {path: "def test_seed():\n    assert 1 == 2 - 1\n"})
    change(repo, {path: body})
    return check().run(str(repo), "origin/main")


def test_a_test_that_asserts_nothing_is_reported(repo):
    """The plainest shape: the body exercises code and pins none of it."""
    out = run_on(
        repo, "def test_thing():\n    result = compute(1)\n    print(result)\n"
    )
    assert "asserts nothing at all" in out


def test_a_raises_context_counts_as_an_assertion(repo):
    """pytest.raises pins behaviour without an assert statement."""
    out = run_on(
        repo,
        "import pytest\n\n\ndef test_thing():\n"
        "    with pytest.raises(ValueError):\n        compute(1)\n",
    )
    assert "asserts nothing at all" not in out


def test_a_mock_assertion_call_counts_as_an_assertion(repo):
    """assert_called_once_with is the assertion, even with no assert statement."""
    out = run_on(
        repo,
        "def test_thing(spy):\n    compute(1)\n    spy.assert_called_once_with(1)\n",
    )
    assert "asserts nothing at all" not in out


def test_an_all_negative_test_is_reported(repo):
    """R2-031's shape: `!= rate_limited` passes for every other wrong code too."""
    out = run_on(
        repo,
        "def test_thing():\n    code = classify(err)\n    assert code != 'rate_limited'\n",
    )
    assert "every assertion is negative" in out


def test_one_positive_assertion_clears_the_negative_shape(repo):
    """A negative assertion beside a positive one is pinning, not merely excluding."""
    out = run_on(
        repo,
        "def test_thing():\n    code = classify(err)\n"
        "    assert code == 'bad_request'\n    assert code != 'rate_limited'\n",
    )
    assert "every assertion is negative" not in out


def test_a_self_comparison_is_reported(repo):
    """`assert X == X` is true whatever the code under test does."""
    out = run_on(
        repo, "def test_thing():\n    assert FLOOR == FLOOR\n    assert other() != 1\n"
    )
    assert "compares a value with itself" in out


def test_ambient_env_reads_are_reported(repo):
    """R1-069: no monkeypatch, so the result depends on the machine running it."""
    out = run_on(
        repo,
        "import os\n\n\ndef test_thing():\n    assert os.getenv('WAIT') == '30'\n",
    )
    assert "reads process env" in out


def test_a_monkeypatched_env_read_is_not_reported(repo):
    """setenv makes the value the test's own, not the machine's."""
    out = run_on(
        repo,
        "import os\n\n\ndef test_thing(monkeypatch):\n"
        "    monkeypatch.setenv('WAIT', '30')\n"
        "    assert os.getenv('WAIT') == '30'\n",
    )
    assert "reads process env" not in out


def test_an_unnamed_caplog_level_is_reported(repo):
    """R2-056: at_level without logger= cannot fail if the module pins its own level."""
    out = run_on(
        repo,
        "import logging\n\n\ndef test_thing(caplog):\n"
        "    with caplog.at_level(logging.INFO):\n        emit()\n"
        "    assert 'restaging' not in caplog.text\n",
    )
    assert "at_level() without a logger=" in out


def test_a_named_caplog_level_is_not_reported(repo):
    """Naming the logger is what makes the capture level actually apply."""
    out = run_on(
        repo,
        "import logging\n\n\ndef test_thing(caplog):\n"
        "    with caplog.at_level(logging.DEBUG, logger='pkg.mod'):\n        emit()\n"
        "    assert 'restaging' in caplog.text\n",
    )
    assert "at_level() without a logger=" not in out


def test_production_files_are_out_of_scope(repo):
    """This check reads tests; h_mutation already owns changed production code."""
    seed(repo, {"pkg/mod.py": "def f():\n    return 1\n"})
    change(repo, {"pkg/mod.py": "def test_like():\n    compute(1)\n"})
    out = check().run(str(repo), "origin/main")
    assert out.startswith("OK:")


def test_findings_are_framed_as_candidates_not_defects(repo):
    """Legitimate absence tests carry these shapes, so the section must not read as a verdict."""
    out = run_on(
        repo, "def test_thing():\n    result = compute(1)\n    print(result)\n"
    )
    assert "CANDIDATE" in out
    assert "not a defect" in out


def test_a_pure_rename_is_not_a_change(repo):
    """A test file moved without edits has no touched test bodies."""
    seed(repo, {"tests/test_x.py": "def test_nothing():\n    pass\n"})
    (repo / "tests" / "test_x.py").rename(repo / "tests" / "test_y.py")
    commit_all(repo, "rename")
    assert check().run(str(repo), "origin/main").startswith("OK:")
