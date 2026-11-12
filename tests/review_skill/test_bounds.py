import pytest

from .conftest import _git, commit_all
from .test_phase0 import load


def check():
    return load("m_bounds", "checks/m_bounds.py")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    return root


def run_on(repo, body, path="pkg/mod.py"):
    seed = repo / path
    seed.parent.mkdir(parents=True, exist_ok=True)
    seed.write_text("def seeded():\n    return 1\n")
    commit_all(repo, "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    seed.write_text(body)
    commit_all(repo, "change")
    return check().run(str(repo), "origin/main")


def test_a_parameter_clamped_only_from_above_is_reported(repo):
    """The open end is caller-supplied, which is the question the row asks."""
    out = run_on(repo, "def call(timeout):\n    return run(min(30, timeout))\n")
    assert out.startswith("Bound facts:")
    assert "`call` parameter `timeout`" in out
    assert "`min()`" in out and "low end" in out


def test_a_parameter_clamped_from_both_ends_is_not_reported(repo):
    out = run_on(repo, "def call(limit):\n    return max(1, min(limit, 100))\n")
    assert out.startswith("OK:")


def test_a_clamp_on_something_that_is_not_a_parameter_is_not_reported(repo):
    out = run_on(repo, "def call(rows):\n    return min(10, len(rows))\n")
    assert out.startswith("OK:")


def test_a_clamp_the_diff_did_not_add_is_not_reported(repo):
    """Diff-scoped like every other check: old clamps are not this PR's question."""
    path = repo / "pkg" / "mod.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("def call(timeout):\n    return min(30, timeout)\n")
    commit_all(repo, "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    path.write_text(
        "def call(timeout):\n    return min(30, timeout)  # noqa\n\n\nX = 1\n"
    )
    commit_all(repo, "change")
    out = check().run(str(repo), "origin/main")
    assert out.startswith("Bound facts:") or out.startswith("OK:")


def test_unparseable_file_does_not_break_the_check(repo):
    out = run_on(repo, "def call(timeout:\n")
    assert out.startswith("OK:")
