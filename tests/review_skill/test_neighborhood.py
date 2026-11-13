import pytest

from .conftest import commit_all
from .test_phase0 import load

BASE = """def other():
    req = build_alt()
    return req


def handler(req):
    audit(req)
    result = engine(req)
    return result
"""

WITH_GATE = """def other():
    req = build_alt()
    return req


def handler(req):
    audit(req)
    if blocked(req):
        return refuse()
    result = engine(req)
    return result
"""

NEW_FUNCTION = (
    WITH_GATE
    + """

def freshly_added(a):
    step_one(a)
    step_two(a)
    return step_three(a)
"""
)


def check():
    return load("i_neighborhood", "checks/i_neighborhood.py")


@pytest.fixture
def repo(tmp_path):
    import subprocess

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "init", "-b", "main"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return root


def commit_two(repo, first, second, path="pkg/svc.py"):
    import subprocess

    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(first)
    commit_all(repo, "base")
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "update-ref",
            "refs/remotes/origin/main",
            "HEAD",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    target.write_text(second)
    commit_all(repo, "change")
    return base


def test_insertion_reports_what_runs_before_and_after_it(repo):
    """An inserted guard names the calls it precedes and follows, so ungated work is visible."""
    base = commit_two(repo, BASE, WITH_GATE)
    out = check().run(str(repo), base)
    assert "audit@" in out
    assert "engine@" in out
    assert "runs BEFORE the change" in out
    assert "runs AFTER the change" in out


REBOUND = """def handler(req):
    req = normalise(req)
    audit(req)
    result = engine(req)
    return result
"""

REBOUND_WITH_GATE = """def handler(req):
    req = normalise(req)
    audit(req)
    if blocked(req):
        return refuse()
    result = engine(req)
    return result
"""

TWO_FUNCTIONS = """def load(path):
    out = open(path).read()
    return out


def save(out):
    write(out)
"""

TWO_FUNCTIONS_GUARDED = """def load(path):
    out = open(path).read()
    return out


def save(out):
    if not out:
        return
    write(out)
"""


def test_other_bindings_of_a_read_name_are_reported(repo):
    """A name the changed lines read, bound elsewhere in the same function, is flagged as another shape."""
    base = commit_two(repo, REBOUND, REBOUND_WITH_GATE)
    out = check().run(str(repo), base)
    assert "other bindings of names the changed lines read" in out
    assert "req@2" in out


def test_code_in_other_functions_is_not_reported_around_the_change(repo):
    """Calls and bindings from a different function never appear as running around, or reaching, the change."""
    base = commit_two(repo, TWO_FUNCTIONS, TWO_FUNCTIONS_GUARDED)
    out = check().run(str(repo), base)
    assert "write@" in out
    assert "open@" not in out
    assert "read@" not in out
    assert "out@2" not in out


ADJACENT = """def load(path):
    data = open(path).read()
    return data


def save(out):
    write(out)
    flush(out)
    notify(out)
"""

ADJACENT_CHANGED = """def load(path):
    data = open(path).read()
    log(data)
    return data


def save(out):
    if not out:
        return
    write(out)
    flush(out)
    notify(out)
"""


def test_nearby_changes_in_two_functions_are_reported_separately(repo):
    """Changed lines close together but in different functions are never merged into one hunk."""
    base = commit_two(repo, ADJACENT, ADJACENT_CHANGED)
    out = check().run(str(repo), base)
    save_block = next(b for b in out.split("\n\n") if "in `save`" in b)
    assert "write@" in save_block
    assert "notify@" in save_block
    assert "open@" not in save_block


def test_test_files_are_excluded(repo):
    """Changed test files produce no boundary facts, keeping the section to production code."""
    base = commit_two(repo, BASE, WITH_GATE, path="tests/test_svc.py")
    out = check().run(str(repo), base)
    assert "no non-test python changes" in out


def test_a_wholly_new_function_ranks_below_an_insertion(repo):
    """An insertion into existing code outranks a brand-new function, which has no execution neighbours."""
    base = commit_two(repo, BASE, NEW_FUNCTION)
    out = check().run(str(repo), base)
    headers = [ln for ln in out.splitlines() if ln.startswith("### ")]
    assert headers
    assert "handler" in headers[0], headers


def test_unparseable_changed_file_degrades_without_crashing(repo):
    """A changed file that will not parse is reported SKIPPED rather than aborting the check."""
    base = commit_two(repo, BASE, "def broken(:\n")
    out = check().run(str(repo), base)
    assert "SKIPPED: unparseable" in out


def test_no_python_changes_is_reported_ok(repo):
    """A diff touching no python files says so instead of emitting an empty section."""
    import subprocess

    (repo / "pkg").mkdir()
    (repo / "pkg" / "svc.py").write_text(BASE)
    (repo / "README.md").write_text("before\n")
    commit_all(repo, "base")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    (repo / "README.md").write_text("after\n")
    commit_all(repo, "docs only")
    out = check().run(str(repo), base)
    assert out.startswith("OK:")


def test_a_pure_rename_is_not_a_change(repo):
    """A file moved without edits has no added lines, so nothing around it is mapped."""
    base = commit_two(repo, BASE, WITH_GATE)
    (repo / "pkg" / "svc.py").rename(repo / "pkg" / "moved.py")
    commit_all(repo, "rename")
    assert check().run(str(repo), "HEAD~1").startswith("OK:")
    assert "audit@" in check().run(str(repo), base)
