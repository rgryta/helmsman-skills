import pytest

from .conftest import _git, assert_parity, commit_all
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


HEAVY = "def f():\n" + "    # c\n" * 8 + "    return 1\n"
LONG_HEAVY = "def f():\n" + "    # c\n    x = 1\n" * 8 + "    return 1\n"


def _write(repo, rel, body):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body.encode() if isinstance(body, str) else body)


def _pre_existing(repo):
    mod = repo / "pkg" / "mod.py"
    mod.write_text(HEAVY)
    commit_all(repo)
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    mod.write_text(
        HEAVY + "".join(f"\n\ndef g{n}(x):\n    return x\n" for n in range(10))
    )


def _rename(repo):
    _git(repo, "mv", "pkg/mod.py", "pkg/moved.py")
    _write(repo, "pkg/moved.py", "def existing(x):\n    return x + 1\n" + LONG_HEAVY)


def _unknown_provenance(repo):
    _write(repo, ".gitattributes", "*.py -diff\n")
    _write(repo, "pkg/mod.py", HEAVY)


PARITY_CASES = {
    "comment_heavy": lambda r: _write(r, "pkg/mod.py", HEAVY),
    "clean": lambda r: None,
    "malformed": lambda r: _write(r, "pkg/mod.py", "def broken(:\n    'unterminated\n"),
    "pre_existing": _pre_existing,
    "tiny_diff": lambda r: _write(
        r,
        "pkg/mod.py",
        "def existing(x):\n    return x + 1\n\n\ndef added(y):\n    # c\n    return y * 2\n",
    ),
    "rename": _rename,
    "unknown_provenance": _unknown_provenance,
    "space_and_unicode_paths": lambda r: (
        _write(r, "pkg/my mod.py", LONG_HEAVY),
        _write(r, "pkg/\u00e9.py", LONG_HEAVY),
    ),
    "crlf_and_invalid_utf8": lambda r: _write(
        r, "pkg/mod.py", LONG_HEAVY.replace("\n", "\r\n").encode() + b"# \xff\r\n"
    ),
    "form_feed_splits_lines": lambda r: _write(
        r, "pkg/mod.py", LONG_HEAVY.replace("x = 1", "x = 1\x0c")
    ),
    "tab_inconsistency": lambda r: _write(
        r, "pkg/mod.py", LONG_HEAVY + "if x:\n\ta\n        b\n"
    ),
    "unclosed_bracket": lambda r: _write(r, "pkg/mod.py", LONG_HEAVY + "f(1,\n"),
    "bad_number": lambda r: _write(r, "pkg/mod.py", LONG_HEAVY + "y = 1_\n"),
    "printable_junk_still_tokenizes": lambda r: _write(
        r, "pkg/mod.py", LONG_HEAVY + "y = $\n"
    ),
}


@pytest.mark.parametrize("case", PARITY_CASES)
def test_rust_port_matches_python(case, helmsman_cli, fixture_repo):
    """The Rust check prints what the Python check prints, with its exit code, for every case above."""
    PARITY_CASES[case](fixture_repo)
    _git(fixture_repo, "add", "-A")
    _git(fixture_repo, "commit", "--allow-empty", "-m", case)
    assert_parity(helmsman_cli, "d_comment_density", fixture_repo)


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("HELMSMAN_COMMENT_DENSITY_CAP", "0.9"),
        ("HELMSMAN_COMMENT_DENSITY_CAP", " 1_0e-2 "),
        ("HELMSMAN_COMMENT_DENSITY_CAP", "abc"),
        ("HELMSMAN_PHASE0_CHECK_S", "nope"),
    ],
)
def test_rust_port_matches_python_env(
    var, value, helmsman_cli, fixture_repo, monkeypatch
):
    monkeypatch.setenv(var, value)
    _write(fixture_repo, "pkg/mod.py", HEAVY)
    commit_all(fixture_repo)
    assert_parity(helmsman_cli, "d_comment_density", fixture_repo)


def test_rust_port_matches_python_on_git_failure(helmsman_cli, fixture_repo):
    assert_parity(helmsman_cli, "d_comment_density", fixture_repo, base="no-such-ref")


def test_unknown_provenance_case_really_measures_the_whole_file(fixture_repo):
    _unknown_provenance(fixture_repo)
    commit_all(fixture_repo)
    out = load("d_comment_density", "checks/d_comment_density.py").run(
        str(fixture_repo), "origin/main"
    )
    assert "of lines are comments" in out
