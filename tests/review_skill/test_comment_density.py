from .conftest import _git, commit_all
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


def test_pre_existing_comments_do_not_count_against_the_diff(fixture_repo):
    """Only lines the diff added are measured, so an old comment-heavy file with clean additions passes."""
    mod = fixture_repo / "pkg" / "mod.py"
    mod.write_text("def f():\n" + "    # c\n" * 8 + "    return 1\n")
    commit_all(fixture_repo)
    _git(fixture_repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    mod.write_text(
        mod.read_text()
        + "".join(f"\n\ndef g{n}(x):\n    return x\n" for n in range(10))
    )
    commit_all(fixture_repo)
    out = load("d_comment_density", "checks/d_comment_density.py").run(
        str(fixture_repo), "origin/main"
    )
    assert out.startswith("OK:")


def test_cap_is_overridable_from_the_environment(fixture_repo, monkeypatch):
    monkeypatch.setenv("HELMSMAN_COMMENT_DENSITY_CAP", "0.9")
    body = "def f():\n" + "    # c\n" * 8 + "    return 1\n"
    (fixture_repo / "pkg" / "mod.py").write_text(body)
    commit_all(fixture_repo)
    out = load("d_comment_density", "checks/d_comment_density.py").run(
        str(fixture_repo), "origin/main"
    )
    assert out.startswith("OK:")


def test_tiny_diff_is_not_measured(fixture_repo):
    """Under ten added non-blank lines a single comment would dominate the ratio, so the file is skipped."""
    (fixture_repo / "pkg" / "mod.py").write_text(
        "def existing(x):\n    return x + 1\n\n\ndef added(y):\n    # c\n    return y * 2\n"
    )
    commit_all(fixture_repo)
    out = load("d_comment_density", "checks/d_comment_density.py").run(
        str(fixture_repo), "origin/main"
    )
    assert out.startswith("OK:")


def test_unknown_added_lines_fall_back_to_whole_file_wording(fixture_repo, monkeypatch):
    """When the diff's added lines are unknown the whole file is measured, and the finding must not blame the diff."""
    mod = load("d_comment_density", "checks/d_comment_density.py")
    monkeypatch.setattr(
        mod._diff, "added_lines_by_path", lambda w, b, files: dict.fromkeys(files)
    )
    (fixture_repo / "pkg" / "mod.py").write_text(
        "def f():\n" + "    # c\n" * 8 + "    return 1\n"
    )
    commit_all(fixture_repo)
    out = mod.run(str(fixture_repo), "origin/main")
    assert "of lines are comments" in out and "added" not in out
