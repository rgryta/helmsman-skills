import pytest

from .conftest import _git, commit_all
from .test_phase0 import load


@pytest.fixture(autouse=True)
def _worktree(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")


def test_in_range_citations_all_resolve(tmp_path, capsys):
    """A citation whose file exists and whose line is within it resolves clean."""
    vc = load("verify_citations", "verify_citations.py")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("a\nb\nc\nd\ne\n")
    findings = tmp_path / "REVIEW-7-hunter.md"
    findings.write_text("see pkg/mod.py:3 for the bug\n")
    assert vc.main([str(tmp_path), str(findings)]) == 0
    out = capsys.readouterr().out
    assert "CHECKED: 1 citation(s)" in out
    assert out.strip().endswith("ALL_CITATIONS_RESOLVE")


def test_line_past_end_of_file_is_reported_bad(tmp_path, capsys):
    """A line number beyond the file's length is a fabricated position, not a finding."""
    vc = load("verify_citations", "verify_citations.py")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("a\nb\nc\n")
    findings = tmp_path / "REVIEW-7-hunter.md"
    findings.write_text("the guard at pkg/mod.py:990-1032 is wrong\n")
    assert vc.main([str(tmp_path), str(findings)]) == 0
    out = capsys.readouterr().out
    assert "BAD pkg/mod.py:990-1032 - file has only 3 lines" in out
    assert "BAD_CITATIONS: 1" in out


def test_missing_file_is_reported_bad(tmp_path, capsys):
    """A citation naming a file absent from the worktree is reported, not ignored."""
    vc = load("verify_citations", "verify_citations.py")
    findings = tmp_path / "REVIEW-7-project.md"
    findings.write_text("broken in pkg/ghost.py:12\n")
    assert vc.main([str(tmp_path), str(findings)]) == 0
    out = capsys.readouterr().out
    assert "BAD pkg/ghost.py:12 - no such file in the worktree" in out


def test_markdown_link_anchors_are_parsed(tmp_path, capsys):
    """Citations written as markdown line anchors are checked like colon form."""
    vc = load("verify_citations", "verify_citations.py")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("a\nb\n")
    findings = tmp_path / "REVIEW-7-landmine.md"
    findings.write_text("[here](pkg/mod.py#L400)\n")
    assert vc.main([str(tmp_path), str(findings)]) == 0
    assert "BAD pkg/mod.py:400" in capsys.readouterr().out


def test_unreadable_findings_file_does_not_abort_the_check(tmp_path, capsys):
    """An unreadable findings file is named and the remaining files still get checked."""
    vc = load("verify_citations", "verify_citations.py")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("a\n")
    good = tmp_path / "REVIEW-7-hunter.md"
    good.write_text("pkg/mod.py:1\n")
    assert vc.main([str(tmp_path), str(tmp_path / "gone.md"), str(good)]) == 0
    out = capsys.readouterr().out
    assert "UNREADABLE:" in out
    assert "ALL_CITATIONS_RESOLVE" in out


def test_prose_without_line_numbers_is_not_a_citation(tmp_path, capsys):
    """A bare filename with no line number is not checked, so prose does not create noise."""
    vc = load("verify_citations", "verify_citations.py")
    findings = tmp_path / "REVIEW-7-hunter.md"
    findings.write_text("we looked at pkg/ghost.py and it was fine\n")
    assert vc.main([str(tmp_path), str(findings)]) == 0
    assert "CHECKED: 0 citation(s)" in capsys.readouterr().out


def test_usage_error_without_arguments():
    """Too few arguments exits 2 rather than raising."""
    vc = load("verify_citations", "verify_citations.py")
    assert vc.main([]) == 2


def test_top_level_file_citation_is_checked(tmp_path, capsys):
    """A citation of a file in the repo root, with no directory, is still checked."""
    vc = load("verify_citations", "verify_citations.py")
    _git(tmp_path, "init", "-b", "main")
    (tmp_path / "setup.py").write_text("a\n")
    commit_all(tmp_path)
    findings = tmp_path / "REVIEW-7-hunter.md"
    findings.write_text("see setup.py:10\n")
    assert vc.main([str(tmp_path), str(findings)]) == 0
    assert "BAD setup.py:10 - file has only 1 lines" in capsys.readouterr().out


def test_blob_urls_and_diff_prefixes_resolve(tmp_path, capsys):
    """GitHub blob URLs and a/ b/ diff prefixes name the same worktree file."""
    vc = load("verify_citations", "verify_citations.py")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("a\nb\nc\n")
    findings = tmp_path / "REVIEW-7-hunter.md"
    findings.write_text(
        "https://github.com/o/r/blob/abc123/pkg/mod.py#L2-L3\n"
        "a/pkg/mod.py:1 and b/pkg/mod.py:3\n"
    )
    assert vc.main([str(tmp_path), str(findings)]) == 0
    out = capsys.readouterr().out
    assert "BAD" not in out
    assert "ALL_CITATIONS_RESOLVE" in out


def test_shortened_path_resolves_only_when_unique(tmp_path, capsys):
    """A path suffix resolves against the one tracked file it names; two matches are ambiguous."""
    vc = load("verify_citations", "verify_citations.py")
    _git(tmp_path, "init", "-b", "main")
    for d in ("src/pkg", "src/other"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "src/pkg/mod.py").write_text("a\nb\n")
    (tmp_path / "src/pkg/util.py").write_text("a\n")
    (tmp_path / "src/other/util.py").write_text("a\n")
    commit_all(tmp_path)
    findings = tmp_path / "REVIEW-7-hunter.md"
    findings.write_text("pkg/mod.py:2 and mod.py:5 and util.py:1\n")
    assert vc.main([str(tmp_path), str(findings)]) == 0
    out = capsys.readouterr().out
    assert "BAD pkg/mod.py" not in out
    assert "BAD mod.py:5 - file has only 2 lines" in out
    assert "BAD util.py:1 - ambiguous: suffix of 2 tracked files" in out


def test_layer_file_without_citations_is_warned(tmp_path, capsys):
    """A layer file citing nothing is called out rather than passing silently."""
    vc = load("verify_citations", "verify_citations.py")
    findings = tmp_path / "REVIEW-7-hunter.md"
    findings.write_text("looks fine to me\n")
    assert vc.main([str(tmp_path), str(findings)]) == 0
    assert (
        f"WARNING: {findings} cites no file:line - any findings in it are unanchored"
        in capsys.readouterr().out
    )


def test_bare_host_and_port_is_not_a_citation(tmp_path, capsys):
    """`example.com:8080` names a host, not a file, when the repo tracks no .com files."""
    vc = load("verify_citations", "verify_citations.py")
    _git(tmp_path, "init", "-b", "main")
    (tmp_path / "setup.py").write_text("a\n")
    commit_all(tmp_path)
    findings = tmp_path / "REVIEW-7-hunter.md"
    findings.write_text(
        "reach example.com:8080 or localhost.localdomain:5432, see setup.py:1\n"
    )
    assert vc.main([str(tmp_path), str(findings)]) == 0
    out = capsys.readouterr().out
    assert "BAD" not in out
    assert "CHECKED: 1 citation(s)" in out


@pytest.mark.parametrize("kind", ["missing", "not-git"])
def test_a_path_that_is_not_a_worktree_fails_instead_of_passing(
    kind, tmp_path, tmp_path_factory, capsys
):
    """A wrong worktree path exits 2 instead of dropping every bare citation and passing."""
    vc = load("verify_citations", "verify_citations.py")
    root = tmp_path_factory.mktemp("outdir")
    if kind == "missing":
        root = root / "does-not-exist"
    findings = tmp_path / "REVIEW-7-hunter.md"
    findings.write_text("see `foo.py:12` and bar.py:3\n")
    assert vc.main([str(root), str(findings)]) == 2
    out = capsys.readouterr().out
    assert f"NOT_A_WORKTREE: {root}" in out
    assert "ALL_CITATIONS_RESOLVE" not in out
