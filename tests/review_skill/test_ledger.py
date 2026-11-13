import json
import subprocess
import sys

from .conftest import SCRIPTS


def led(tmp_path, *args):
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "ledger.py"),
            *args,
            "--repo",
            "o/r",
            "--pr",
            "7",
            "--dir",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_round_and_finding_lifecycle(tmp_path):
    assert led(tmp_path, "round-start", "--sha", "aaa").returncode == 0
    led(
        tmp_path,
        "upsert",
        "--id",
        "f1",
        "--title",
        "bug",
        "--file",
        "x.py",
        "--line",
        "5",
        "--severity",
        "blocker",
        "--provenance",
        "landmine",
        "--sha",
        "aaa",
    )
    led(tmp_path, "round-end", "--sha", "aaa", "--complete")
    data = json.loads((tmp_path / "o__r__pr7.json").read_text())
    assert data["rounds"][0]["complete"] is True
    assert data["findings"][0]["status"] == "open"
    led(tmp_path, "set-status", "--id", "f1", "--status", "fixed")
    rep = led(tmp_path, "report").stdout
    assert "[open] f1" not in rep
    assert "1 fixed" in rep


def test_report_lists_open_only(tmp_path):
    led(tmp_path, "round-start", "--sha", "aaa")
    led(
        tmp_path,
        "upsert",
        "--id",
        "keep",
        "--title",
        "a",
        "--file",
        "x.py",
        "--line",
        "1",
        "--severity",
        "blocker",
        "--provenance",
        "landmine",
        "--sha",
        "aaa",
    )
    led(
        tmp_path,
        "upsert",
        "--id",
        "gone",
        "--title",
        "b",
        "--file",
        "y.py",
        "--line",
        "2",
        "--severity",
        "nitpick",
        "--provenance",
        "hunter",
        "--sha",
        "aaa",
        "--status",
        "fixed",
    )
    rep = led(tmp_path, "report").stdout
    assert "keep" in rep and "gone" not in rep and "1 closed" in rep


def test_missing_sha_and_unknown_ids_fail_cleanly(tmp_path):
    r1 = led(tmp_path, "round-start")
    assert r1.returncode == 1
    assert r1.stdout == ""
    assert "requires --sha" in r1.stderr

    r2 = led(tmp_path, "round-end", "--sha", "nope", "--complete")
    assert r2.returncode == 1
    assert r2.stdout == ""
    assert "no round with sha nope" in r2.stderr

    r3 = led(tmp_path, "set-status", "--id", "nope", "--status", "fixed")
    assert r3.returncode == 1
    assert r3.stdout == ""
    assert "no finding with id nope" in r3.stderr

    assert led(tmp_path, "upsert", "--id", "x", "--status", "closed").returncode == 2


def test_delta_mode_rules(tmp_path):
    assert "FULL_REHUNT" in led(tmp_path, "delta", "--sha", "bbb").stdout
    led(tmp_path, "round-start", "--sha", "aaa")
    led(tmp_path, "round-end", "--sha", "aaa", "--partial", "layer timed out")
    assert "FULL_REHUNT" in led(tmp_path, "delta", "--sha", "bbb").stdout
    led(tmp_path, "round-start", "--sha", "bbb")
    led(tmp_path, "round-end", "--sha", "bbb", "--complete")
    assert "DELTA_SINCE bbb" in led(tmp_path, "delta", "--sha", "ccc").stdout
    led(tmp_path, "round-start", "--sha", "ccc")
    assert "FULL_REHUNT" in led(tmp_path, "delta", "--sha", "ddd").stdout


def test_fresh_round_on_an_unchanged_head_is_a_full_rehunt(tmp_path):
    """A delta against the head itself is empty, so a re-review of an unchanged PR hunts everything."""
    led(tmp_path, "round-start", "--sha", "aaa")
    led(tmp_path, "round-end", "--sha", "aaa", "--complete")
    assert led(tmp_path, "delta", "--sha", "aaa").stdout.strip() == "FULL_REHUNT"


def test_round_with_skipped_layers_forces_full_rehunt(tmp_path):
    """A complete round that skipped layers must not let the next round narrow to a delta."""
    led(tmp_path, "round-start", "--sha", "aaa")
    led(
        tmp_path, "round-end", "--sha", "aaa", "--complete", "--skipped", "hunter, edge"
    )
    data = json.loads((tmp_path / "o__r__pr7.json").read_text())
    assert data["rounds"][0]["complete"] is True
    assert data["rounds"][0]["skipped_layers"] == ["hunter", "edge"]
    assert "skipped=hunter,edge" in led(tmp_path, "report").stdout
    assert "FULL_REHUNT" in led(tmp_path, "delta", "--sha", "bbb").stdout
    led(tmp_path, "round-start", "--sha", "bbb")
    led(tmp_path, "round-end", "--sha", "bbb", "--complete")
    assert "DELTA_SINCE bbb" in led(tmp_path, "delta", "--sha", "ccc").stdout


def test_reupsert_without_status_preserves_closed_status(tmp_path):
    led(tmp_path, "round-start", "--sha", "aaa")
    led(
        tmp_path,
        "upsert",
        "--id",
        "f1",
        "--title",
        "bug",
        "--file",
        "x.py",
        "--line",
        "5",
        "--severity",
        "blocker",
        "--provenance",
        "landmine",
        "--sha",
        "aaa",
    )
    led(tmp_path, "set-status", "--id", "f1", "--status", "fixed")
    led(
        tmp_path,
        "upsert",
        "--id",
        "f1",
        "--title",
        "bug",
        "--file",
        "x.py",
        "--line",
        "9",
        "--severity",
        "blocker",
        "--provenance",
        "landmine",
        "--sha",
        "bbb",
    )
    data = json.loads((tmp_path / "o__r__pr7.json").read_text())
    f1 = next(x for x in data["findings"] if x["id"] == "f1")
    assert f1["status"] == "fixed"
    assert f1["line"] == "9"


def test_new_upsert_without_status_defaults_to_open(tmp_path):
    led(
        tmp_path,
        "upsert",
        "--id",
        "f2",
        "--title",
        "a",
        "--file",
        "x.py",
        "--line",
        "1",
        "--severity",
        "nitpick",
        "--provenance",
        "hunter",
        "--sha",
        "aaa",
    )
    data = json.loads((tmp_path / "o__r__pr7.json").read_text())
    assert data["findings"][0]["status"] == "open"


def test_corrupt_ledger_degrades_instead_of_traceback(tmp_path):
    (tmp_path / "o__r__pr7.json").write_text("{ broken")
    assert led(tmp_path, "report").returncode == 0
    result = led(
        tmp_path,
        "upsert",
        "--id",
        "f1",
        "--title",
        "a",
        "--file",
        "x.py",
        "--line",
        "1",
        "--severity",
        "nitpick",
        "--provenance",
        "hunter",
        "--sha",
        "aaa",
    )
    assert result.returncode == 0
    data = json.loads((tmp_path / "o__r__pr7.json").read_text())
    assert data["findings"][0]["id"] == "f1"


def test_corrupt_ledger_is_moved_aside_not_overwritten(tmp_path):
    """An unparseable ledger keeps its bytes in a .corrupt-* file and the error is printed."""
    (tmp_path / "o__r__pr7.json").write_text("{ broken")
    result = led(tmp_path, "report")
    assert result.returncode == 0
    assert "unreadable" in result.stderr
    aside = list(tmp_path.glob("o__r__pr7.json.corrupt-*"))
    assert len(aside) == 1 and aside[0].read_text() == "{ broken"


def test_classify_report(tmp_path):
    f1 = tmp_path / "a.md"
    f1.write_text("# findings\n- x\n\nCOMPLETE\n\n")
    f2 = tmp_path / "b.md"
    f2.write_text("# findings\nPARTIAL: ran out of budget\n")
    f3 = tmp_path / "c.md"
    f3.write_text("# findings\n- x\n")
    assert (
        led(tmp_path, "classify-report", "--file", str(f1)).stdout.strip() == "COMPLETE"
    )
    assert led(tmp_path, "classify-report", "--file", str(f2)).stdout.startswith(
        "PARTIAL"
    )
    assert (
        led(tmp_path, "classify-report", "--file", str(f3)).stdout.strip()
        == "MALFORMED"
    )


def test_classify_report_without_file_fails_cleanly(tmp_path):
    result = led(tmp_path, "classify-report")
    assert result.returncode == 1
    assert result.stdout == ""
    assert "requires --file" in result.stderr
    assert "Traceback" not in result.stderr


def test_round_start_mode_defaults_to_full(tmp_path):
    led(tmp_path, "round-start", "--sha", "aaa")
    led(tmp_path, "round-start", "--sha", "bbb", "--mode", "delta")
    data = json.loads((tmp_path / "o__r__pr7.json").read_text())
    assert data["rounds"][0]["mode"] == "full"
    assert data["rounds"][1]["mode"] == "delta"


def test_full_rehunt_after_default_delta_streak(tmp_path):
    led(tmp_path, "round-start", "--sha", "s0", "--mode", "full")
    led(tmp_path, "round-end", "--sha", "s0", "--complete")
    for i in range(1, 5):
        sha = f"s{i}"
        assert "DELTA_SINCE" in led(tmp_path, "delta", "--sha", sha).stdout
        led(tmp_path, "round-start", "--sha", sha, "--mode", "delta")
        led(tmp_path, "round-end", "--sha", sha, "--complete")
    # 4 consecutive delta rounds (s1..s4) recorded, still below the default threshold of 5
    assert "DELTA_SINCE" in led(tmp_path, "delta", "--sha", "s5").stdout
    led(tmp_path, "round-start", "--sha", "s5", "--mode", "delta")
    led(tmp_path, "round-end", "--sha", "s5", "--complete")
    # 5th consecutive delta round now recorded; the next delta call must force a full re-hunt
    assert "FULL_REHUNT" in led(tmp_path, "delta", "--sha", "s6").stdout


def test_full_rehunt_every_override_lowers_threshold(tmp_path):
    led(tmp_path, "round-start", "--sha", "s0", "--mode", "full")
    led(tmp_path, "round-end", "--sha", "s0", "--complete")
    led(tmp_path, "round-start", "--sha", "s1", "--mode", "delta")
    led(tmp_path, "round-end", "--sha", "s1", "--complete")
    assert "DELTA_SINCE" in led(tmp_path, "delta", "--sha", "s2").stdout
    assert (
        "FULL_REHUNT"
        in led(tmp_path, "delta", "--sha", "s2", "--full-every", "1").stdout
    )


def test_full_mode_round_resets_delta_streak(tmp_path):
    led(tmp_path, "round-start", "--sha", "s0", "--mode", "full")
    led(tmp_path, "round-end", "--sha", "s0", "--complete")
    for i in range(1, 6):
        sha = f"s{i}"
        led(tmp_path, "round-start", "--sha", sha, "--mode", "delta")
        led(tmp_path, "round-end", "--sha", sha, "--complete")
    assert "FULL_REHUNT" in led(tmp_path, "delta", "--sha", "s6").stdout
    led(tmp_path, "round-start", "--sha", "s6", "--mode", "full")
    led(tmp_path, "round-end", "--sha", "s6", "--complete")
    assert "DELTA_SINCE" in led(tmp_path, "delta", "--sha", "s7").stdout


def test_classify_report_accepts_a_complete_that_narrates_its_coverage(tmp_path):
    """A round is only recorded partial when a layer really fell short, not on phrasing."""
    f = tmp_path / "narrated.md"
    f.write_text("# findings\n- x\n\nCOMPLETE: reviewed all 8 changed files in full\n")
    assert (
        led(tmp_path, "classify-report", "--file", str(f)).stdout.strip() == "COMPLETE"
    )
