import os
import re
import threading
import time

from .test_phase0 import load


def test_existing_facts_file_is_ready_with_its_header(tmp_path, capsys):
    """A facts file already in place resolves at once and echoes its header line."""
    aw = load("await_phase0", "await_phase0.py")
    facts = tmp_path / "FACTS-7.md"
    facts.write_text("# Phase 0 facts (base abc from origin/main)\n\n## a_ruff\nok\n")
    assert aw.main([str(facts), "5", "0.05"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("READY - # Phase 0 facts (base abc from origin/main)")


def test_late_facts_file_is_picked_up_before_deadline(tmp_path, capsys):
    """The wait re-polls, so facts written after launch end it without burning the deadline."""
    aw = load("await_phase0", "await_phase0.py")
    facts = tmp_path / "FACTS-7.md"
    tmp = tmp_path / "FACTS-7.md.tmp"
    tmp.write_text("Phase 0 unavailable: exit 1\n")
    threading.Timer(0.3, os.replace, args=(tmp, facts)).start()
    start = time.monotonic()
    assert aw.main([str(facts), "10", "0.05"]) == 0
    assert 0.2 < time.monotonic() - start < 5
    assert "READY - Phase 0 unavailable: exit 1" in capsys.readouterr().out


def test_absent_facts_file_times_out_at_the_deadline(tmp_path, capsys):
    """A facts file that never appears ends the wait at the deadline with TIMEOUT."""
    aw = load("await_phase0", "await_phase0.py")
    start = time.monotonic()
    assert aw.main([str(tmp_path / "FACTS-7.md"), "0.3", "0.05"]) == 0
    assert time.monotonic() - start < 5
    out = capsys.readouterr().out
    assert "TIMEOUT - no facts file" in out
    assert re.search(r"^WAITED: \d+s$", out, re.M)


def test_temp_file_from_an_unfinished_write_is_not_ready(tmp_path, capsys):
    """Only the renamed-into-place file counts, never the writer's temp file."""
    aw = load("await_phase0", "await_phase0.py")
    (tmp_path / "FACTS-7.md.tmp.123").write_text("# Phase 0 facts\n")
    assert aw.main([str(tmp_path / "FACTS-7.md"), "0.3", "0.05"]) == 0
    assert "TIMEOUT" in capsys.readouterr().out


def test_missing_outdir_returns_at_once(tmp_path, capsys):
    """A facts path whose directory does not exist cannot be fixed by waiting."""
    aw = load("await_phase0", "await_phase0.py")
    start = time.monotonic()
    assert aw.main([str(tmp_path / "absent" / "FACTS-7.md"), "30", "0.05"]) == 0
    assert time.monotonic() - start < 2
    assert "MISSING" in capsys.readouterr().out


def test_usage_error_without_arguments():
    """No arguments exits 2 rather than raising."""
    aw = load("await_phase0", "await_phase0.py")
    assert aw.main([]) == 2
