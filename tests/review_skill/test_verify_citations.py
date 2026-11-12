from .test_phase0 import load


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
