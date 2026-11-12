import re
import threading
import time

from .test_phase0 import load


def write_layer(worktree, pr, layer, terminator="COMPLETE"):
    (worktree / f"REVIEW-{pr}-{layer}.md").write_text(
        f"## findings\n\nnothing\n\n{terminator}\n"
    )


def test_all_terminated_layers_report_ready(tmp_path, capsys):
    """Every layer file ending in COMPLETE or PARTIAL resolves to ALL_READY."""
    aw = load("await_layers", "await_layers.py")
    write_layer(tmp_path, 7, "project")
    write_layer(tmp_path, 7, "hunter", "PARTIAL: ran out of budget")
    assert aw.main([str(tmp_path), "7", "project,hunter", "5", "0.05"]) == 0
    out = capsys.readouterr().out
    assert "project READY - COMPLETE" in out
    assert "hunter READY - PARTIAL: ran out of budget" in out
    assert out.strip().endswith("ALL_READY")


def test_missing_layer_is_bounded_and_reported_stalled(tmp_path, capsys):
    """A layer that never writes its file ends the wait at the deadline, not later."""
    aw = load("await_layers", "await_layers.py")
    write_layer(tmp_path, 7, "project")
    start = time.monotonic()
    assert aw.main([str(tmp_path), "7", "project,landmine", "0.5", "0.05"]) == 0
    elapsed = time.monotonic() - start
    out = capsys.readouterr().out
    assert "landmine MISSING" in out
    assert "STALLED: landmine" in out
    assert elapsed < 5


def test_file_without_terminator_is_incomplete_not_ready(tmp_path, capsys):
    """A findings file lacking the COMPLETE/PARTIAL contract line never counts as ready."""
    aw = load("await_layers", "await_layers.py")
    (tmp_path / "REVIEW-7-hunter.md").write_text("## findings\n\nhalf a thought\n")
    assert aw.main([str(tmp_path), "7", "hunter", "0.3", "0.05"]) == 0
    out = capsys.readouterr().out
    assert "hunter INCOMPLETE - no COMPLETE/PARTIAL terminator" in out
    assert "STALLED: hunter" in out


def test_empty_file_is_incomplete(tmp_path, capsys):
    """A zero-byte findings file is a stall, not a ready layer."""
    aw = load("await_layers", "await_layers.py")
    (tmp_path / "REVIEW-7-hunter.md").write_text("")
    assert aw.main([str(tmp_path), "7", "hunter", "0.3", "0.05"]) == 0
    assert "hunter INCOMPLETE - empty" in capsys.readouterr().out


def test_late_arriving_layer_is_picked_up_before_deadline(tmp_path, capsys):
    """The wait re-polls, so a file written after dispatch resolves without burning the deadline."""
    aw = load("await_layers", "await_layers.py")
    threading.Timer(0.3, write_layer, args=(tmp_path, 7, "hunter")).start()
    start = time.monotonic()
    assert aw.main([str(tmp_path), "7", "hunter", "10", "0.05"]) == 0
    elapsed = time.monotonic() - start
    assert "ALL_READY" in capsys.readouterr().out
    assert 0.2 < elapsed < 5


def test_no_layers_returns_immediately(tmp_path, capsys):
    """An empty layer list is ALL_READY rather than an error or a wait."""
    aw = load("await_layers", "await_layers.py")
    assert aw.main([str(tmp_path), "7", "", "0.3", "0.05"]) == 0
    assert capsys.readouterr().out.strip() == "ALL_READY"


def test_wait_reports_how_long_it_actually_waited(tmp_path, capsys):
    """The elapsed wait is printed so a slow layer can be told from a dead one."""
    aw = load("await_layers", "await_layers.py")
    assert aw.main([str(tmp_path), "7", "landmine", "0.3", "0.05"]) == 0
    out = capsys.readouterr().out
    assert re.search(r"^WAITED: \d+s$", out, re.M)
    assert out.strip().endswith("STALLED: landmine")


def test_usage_error_without_arguments():
    """Too few arguments exits 2 rather than raising."""
    aw = load("await_layers", "await_layers.py")
    assert aw.main([]) == 2


def test_missing_outdir_is_reported_distinctly_from_a_missing_file(tmp_path, capsys):
    """A nonexistent artifact directory says so instead of looking like slow layers."""
    aw = load("await_layers", "await_layers.py")
    gone = tmp_path / "run-never-created"
    assert aw.main([str(gone), "7", "project,landmine", "5", "0.05"]) == 0
    out = capsys.readouterr().out
    assert "project NO_OUTDIR" in out
    assert "landmine NO_OUTDIR" in out
    assert "STALLED:" not in out
    assert out.strip().endswith(f"NO_OUTDIR: {gone}")


def test_missing_outdir_does_not_burn_the_deadline(tmp_path, capsys):
    """A bad directory cannot be fixed by waiting, so the wait returns at once."""
    aw = load("await_layers", "await_layers.py")
    start = time.monotonic()
    assert aw.main([str(tmp_path / "absent"), "7", "project", "30", "0.05"]) == 0
    assert time.monotonic() - start < 2
    capsys.readouterr()


def test_a_file_path_as_outdir_is_not_mistaken_for_a_directory(tmp_path, capsys):
    """An outdir argument pointing at a regular file is reported, not silently polled."""
    aw = load("await_layers", "await_layers.py")
    bogus = tmp_path / "notadir"
    bogus.write_text("x\n")
    assert aw.main([str(bogus), "7", "project", "5", "0.05"]) == 0
    assert "NO_OUTDIR" in capsys.readouterr().out


def test_a_complete_that_narrates_its_coverage_is_ready(tmp_path, capsys):
    """The wait ends on the terminator the layer wrote, not on the one it was told to."""
    aw = load("await_layers", "await_layers.py")
    write_layer(
        tmp_path, 7, "project", "COMPLETE: reviewed all 8 changed files in full"
    )
    write_layer(tmp_path, 7, "hunter", "**COMPLETED**")
    assert aw.main([str(tmp_path), "7", "project,hunter", "5", "0.05"]) == 0
    out = capsys.readouterr().out
    assert "project READY - COMPLETE" in out
    assert "hunter READY - COMPLETE" in out
    assert "STALLED" not in out
