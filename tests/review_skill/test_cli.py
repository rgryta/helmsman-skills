import json
import subprocess
import sys

from .conftest import SCRIPTS, assert_parity, ported_checks, skip_shims_on_windows
from .test_phase0 import load


def _python_checks(tmp_path):
    checks = tmp_path / "checks"
    checks.mkdir()
    (checks / "ported.py").write_text("print('OK: python ported')\n")
    (checks / "other.py").write_text("print('OK: python other')\n")


def _fake_cli(path, listing_exit=0):
    skip_shims_on_windows()
    path.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "if sys.argv[1] == 'checks':\n"
        "    print('ported')\n"
        f"    sys.exit({listing_exit})\n"
        "print('OK: binary ' + sys.argv[2] + ' ' + sys.argv[4])\n"
    )
    path.chmod(0o755)
    return path


def _facts(phase0, repo, tmp_path, monkeypatch):
    monkeypatch.setattr(phase0, "HERE", tmp_path)
    monkeypatch.setattr(phase0, "CHECKS", None)
    out = tmp_path / "FACTS.md"
    assert phase0.main([str(repo), str(out)]) == 0
    return out.read_text()


def test_listed_check_runs_through_the_binary(fixture_repo, tmp_path, monkeypatch):
    """A check the binary lists runs via `check <name> <worktree> <base>`; the rest stay Python."""
    phase0 = load("phase0", "phase0.py")
    _python_checks(tmp_path)
    monkeypatch.setenv("HELMSMAN_CLI", str(_fake_cli(tmp_path / "helmsman")))
    text = _facts(phase0, fixture_repo, tmp_path, monkeypatch)
    assert "## ported\nOK: binary ported " in text
    assert "## other\nOK: python other" in text


def test_missing_binary_falls_back_to_python(
    fixture_repo, tmp_path, monkeypatch, capsys
):
    """A binary that cannot start costs one stderr NOTE and nothing else."""
    phase0 = load("phase0", "phase0.py")
    _python_checks(tmp_path)
    monkeypatch.setenv("HELMSMAN_CLI", str(tmp_path / "missing"))
    text = _facts(phase0, fixture_repo, tmp_path, monkeypatch)
    assert "## ported\nOK: python " in text
    assert "## other\nOK: python other" in text
    assert capsys.readouterr().err.count("NOTE:") == 1


def test_failing_listing_falls_back_to_python(fixture_repo, tmp_path, monkeypatch):
    phase0 = load("phase0", "phase0.py")
    _python_checks(tmp_path)
    cli = _fake_cli(tmp_path / "helmsman", listing_exit=1)
    monkeypatch.setenv("HELMSMAN_CLI", str(cli))
    assert "## ported\nOK: python " in _facts(
        phase0, fixture_repo, tmp_path, monkeypatch
    )


def test_off_forces_python(fixture_repo, tmp_path, monkeypatch):
    phase0 = load("phase0", "phase0.py")
    _python_checks(tmp_path)
    _fake_cli(tmp_path / "helmsman")
    monkeypatch.setenv("HELMSMAN_CLI", "off")
    assert "## ported\nOK: python " in _facts(
        phase0, fixture_repo, tmp_path, monkeypatch
    )


def test_cached_binary_is_found_by_plugin_version(tmp_path, monkeypatch):
    """Unset HELMSMAN_CLI resolves ~/.helmsman/bin/<plugin-version>/helmsman."""
    phase0 = load("phase0", "phase0.py")
    version = json.loads(phase0.MANIFEST.read_text())["version"]
    cached = tmp_path / ".helmsman" / "bin" / version
    cached.mkdir(parents=True)
    cli = _fake_cli(cached / "helmsman")
    monkeypatch.delenv("HELMSMAN_CLI")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert phase0._cli_routes() == {"ported": str(cli)}


def test_real_binary_rejects_unported_check(helmsman_cli, fixture_repo):
    r = subprocess.run(
        [str(helmsman_cli), "check", "no_such_check", str(fixture_repo), "main"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 2
    assert "not ported" in r.stderr


def test_real_binary_ports_only_known_checks(helmsman_cli):
    python_checks = {
        p.stem for p in (SCRIPTS / "checks").glob("*.py") if not p.stem.startswith("_")
    }
    assert set(ported_checks(helmsman_cli)) <= python_checks


def test_ported_checks_match_python(helmsman_cli, fixture_repo):
    """Every ported check prints what its Python twin prints, with the same exit code."""
    for name in ported_checks(helmsman_cli):
        assert_parity(helmsman_cli, name, fixture_repo)
