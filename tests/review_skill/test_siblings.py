import pytest

from .conftest import _git, commit_all
from .test_phase0 import load

BASE_GUARD = """def handle_400(exc):
    if is_throttle(exc):
        return retry_after(exc)
    return classify(exc)
"""

TWIN_GUARD = """def handle_429(exc):
    if is_throttle(exc):
        return retry_after(exc)
    return classify(exc)
"""


def check():
    return load("j_siblings", "checks/j_siblings.py")


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


def test_a_removed_line_that_survives_elsewhere_is_reported(repo):
    """The corpus's largest class: one site fixed, its twin left carrying the old shape."""
    seed(repo, {"a.py": BASE_GUARD, "b.py": TWIN_GUARD})
    change(
        repo,
        {
            "a.py": BASE_GUARD.replace(
                "if is_throttle(exc):", "if is_throttle(exc) and not exhausted(exc):"
            )
        },
    )
    out = check().run(str(repo), "origin/main")
    assert "b.py" in out
    assert "if is_throttle(exc):" in out


def test_a_shape_changed_at_every_site_is_not_reported(repo):
    """Fixing both twins in one diff leaves nothing surviving, so nothing is flagged."""
    seed(repo, {"a.py": BASE_GUARD, "b.py": TWIN_GUARD})
    new = "if is_throttle(exc) and not exhausted(exc):"
    change(
        repo,
        {
            "a.py": BASE_GUARD.replace("if is_throttle(exc):", new),
            "b.py": TWIN_GUARD.replace("if is_throttle(exc):", new),
        },
    )
    out = check().run(str(repo), "origin/main")
    assert out.startswith("OK:")


def test_a_line_common_across_the_tree_is_not_reported(repo):
    """A boilerplate line has too many hits to attribute, so it is dropped, not listed."""
    common = "    logger.info('starting the configured operation')\n"
    files = {f"m{i}.py": f"def f{i}():\n{common}    return {i}\n" for i in range(25)}
    files["a.py"] = f"def a():\n{common}    return 0\n"
    seed(repo, files)
    change(
        repo,
        {
            "a.py": "def a():\n    logger.debug('starting the configured operation')\n    return 0\n"
        },
    )
    out = check().run(str(repo), "origin/main")
    assert "starting the configured operation" not in out


def test_trivial_lines_are_never_probed(repo):
    """Closers, short lines and imports are not distinctive enough to be a twin signal."""
    body = "import os\nfrom sys import argv\n\n\ndef f():\n    return (\n        os.sep\n    )\n"
    seed(repo, {"a.py": body, "b.py": body})
    change(repo, {"a.py": "def f():\n    return os.sep\n"})
    out = check().run(str(repo), "origin/main")
    assert "from sys import argv" not in out
    assert "`)`" not in out


def test_a_literal_living_in_an_untouched_file_is_reported(repo):
    """The 'third of four copies of the 30s default' shape: one copy edited, others not."""
    seed(
        repo,
        {
            "a.py": "ACQUIRE_WAIT = 30.0\n",
            "elsewhere/config.py": "acquire_wait: float = 30.0\n",
        },
    )
    change(repo, {"a.py": "ACQUIRE_WAIT = 45.0\n"})
    out = check().run(str(repo), "origin/main")
    assert "elsewhere/config.py" in out
    assert "30.0" in out


def test_a_literal_confined_to_changed_files_is_not_reported(repo):
    """Both copies moved together, so there is no untouched sibling to warn about."""
    seed(repo, {"a.py": "ACQUIRE_WAIT = 30.0\n", "b.py": "acquire = 30.0\n"})
    change(repo, {"a.py": "ACQUIRE_WAIT = 45.0\n", "b.py": "acquire = 45.0\n"})
    out = check().run(str(repo), "origin/main")
    assert "b.py" not in out


def test_findings_are_framed_as_candidates_not_defects(repo):
    """A FACTS section read as a finding list would inflate every review it feeds."""
    seed(repo, {"a.py": BASE_GUARD, "b.py": TWIN_GUARD})
    change(
        repo,
        {
            "a.py": BASE_GUARD.replace(
                "if is_throttle(exc):", "if is_throttle(exc) and fresh(exc):"
            )
        },
    )
    out = check().run(str(repo), "origin/main")
    assert "CANDIDATE" in out
    assert "not a defect" in out


def test_non_python_files_are_matched_too(repo):
    """Twins in the corpus live in gitops yaml and frontend tsx, not only python."""
    seed(
        repo,
        {
            "gitops/prod/kustomization.yaml": "patches:\n  - path: service-config.yaml\n",
            "gitops/stg/kustomization.yaml": "patches:\n  - path: service-config.yaml\n",
        },
    )
    change(repo, {"gitops/prod/kustomization.yaml": "patches: []\n"})
    out = check().run(str(repo), "origin/main")
    assert "gitops/stg/kustomization.yaml" in out


def test_a_line_moved_within_a_file_is_not_a_twin(repo):
    """A moved line survives by definition, so reporting it would be pure noise."""
    body = "A = 1\nB = 2\nSHARED_CONSTANT_LINE = compute(A, B)\nC = 3\n"
    seed(repo, {"a.py": body, "b.py": body})
    change(
        repo, {"a.py": "SHARED_CONSTANT_LINE = compute(A, B)\nA = 1\nB = 2\nC = 3\n"}
    )
    out = check().run(str(repo), "origin/main")
    assert "SHARED_CONSTANT_LINE = compute(A, B)" not in out


def test_a_function_moved_to_another_file_is_not_a_twin(repo):
    """A line the diff removes from one file and adds to another was moved, not left behind."""
    moved = "def summarise(rows):\n    total = compute_the_total_value(rows)\n    return total\n"
    seed(repo, {"pkg/a.py": "import os\n\n\n" + moved, "pkg/b.py": "import sys\n"})
    change(repo, {"pkg/a.py": "import os\n", "pkg/b.py": "import sys\n\n\n" + moved})
    out = check().run(str(repo), "origin/main")
    assert "a line this diff removed still exists elsewhere" not in out


def test_a_code_fragment_between_two_strings_is_not_read_as_a_literal():
    """Quote pairing: the span between one string's close and the next one's open is code."""
    line = '    spec = load("_diff", Path(__file__).parent / "_diff.py")'
    assert check()._literals([line]) == ["_diff.py"]


def test_a_two_digit_version_is_not_a_distinctive_literal():
    """`2.0` matches every version string in the tree; `30.0` is a real duplicated default."""
    m = check()
    assert m._literals(["mcp>=2.0"]) == []
    assert "30.0" in m._literals(["ACQUIRE_WAIT = 30.0"])


def test_prose_files_are_out_of_scope():
    """Plan and doc prose repeats itself by nature, which is not a code twin."""
    assert check().SKIP_PATH.search("docs/plans/2026-08-03-fold-review.md")
