import json

from .conftest import SCRIPTS
from .test_phase0 import load


def test_plugin_manifests_ship_the_same_version():
    plugin = SCRIPTS.parents[2]
    claude = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text())
    codex = json.loads((plugin / ".codex-plugin" / "plugin.json").read_text())
    assert claude["version"] == codex["version"]


def test_phase0_full_discovery_order(fixture_repo, tmp_path):
    phase0 = load("phase0", "phase0.py")
    setattr(phase0, "CHECKS", None)
    out = tmp_path / "FACTS.md"
    assert phase0.main([str(fixture_repo), str(out)]) == 0
    text = out.read_text()
    order = [
        text.index(f"## {n}")
        for n in (
            "a_ruff",
            "b_pyright",
            "c_pytest",
            "d_comment_density",
            "e_dead_code",
            "f_import_closure",
            "g_semgrep",
            "h_mutation",
        )
    ]
    assert order == sorted(order)


def test_checks_import_their_helpers_directly():
    """Checks import _diff/_env as modules so pyright can type the reference."""
    for path in (SCRIPTS / "checks").glob("*.py"):
        assert "spec_from_file_location" not in path.read_text(), path.name
