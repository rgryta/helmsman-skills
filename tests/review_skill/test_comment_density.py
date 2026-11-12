from .conftest import commit_all
from .test_phase0 import load


def test_flags_comment_heavy_file(fixture_repo):
    body = "def f():\n" + "    # c\n" * 8 + "    return 1\n"
    (fixture_repo / "pkg" / "mod.py").write_text(body)
    commit_all(fixture_repo)
    out = load("d_comment_density", "checks/d_comment_density.py").run(
        str(fixture_repo), "origin/main"
    )
    assert out.startswith("FINDINGS:") and "pkg/mod.py" in out


def test_clean_file_passes(fixture_repo):
    out = load("d_comment_density", "checks/d_comment_density.py").run(
        str(fixture_repo), "origin/main"
    )
    assert out.startswith("OK:")


def test_malformed_source_degrades_gracefully(fixture_repo):
    (fixture_repo / "pkg" / "mod.py").write_text("def broken(:\n    'unterminated\n")
    commit_all(fixture_repo)
    out = load("d_comment_density", "checks/d_comment_density.py").run(
        str(fixture_repo), "origin/main"
    )
    assert out.startswith("OK:")
