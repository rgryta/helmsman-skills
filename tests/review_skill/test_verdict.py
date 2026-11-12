import pytest

from .conftest import SCRIPTS
from .test_phase0 import load

DECORATED = [
    "COMPLETE: reviewed all 27 changed files (production + tests + docs)",
    "COMPLETE: traced the core gate logic end-to-end against the PR's own claims",
    "COMPLETED",
    "COMPLETE.",
    "**COMPLETE**",
    "- COMPLETE",
    "`COMPLETE`",
    "COMPLETE - full diff traced against origin/main",
]

NOT_A_TERMINATOR = [
    "- The new file-level comment states a genuine non-obvious invariant",
    "COMPLETENESS of the sweep is unverified",
    "## Findings",
    "Complete",
]


@pytest.mark.parametrize("last", DECORATED)
def test_a_decorated_complete_still_reads_as_complete(last):
    """Layers narrate what they covered on the COMPLETE line; that is still COMPLETE."""
    verdict = load("_verdict", "_verdict.py")
    assert verdict.classify(f"## findings\n\n- x\n\n{last}\n")[0] == "COMPLETE"


@pytest.mark.parametrize("last", NOT_A_TERMINATOR)
def test_prose_is_not_mistaken_for_a_terminator(last):
    """A layer that stopped without a verdict must not be read as having one."""
    verdict = load("_verdict", "_verdict.py")
    assert verdict.classify(f"## findings\n\n{last}\n")[0] is None


def test_partial_keeps_its_reason_through_any_separator():
    """The PARTIAL reason is load-bearing, so it survives the punctuation the layer chose."""
    verdict = load("_verdict", "_verdict.py")
    for last in ("PARTIAL: ran out of budget", "PARTIAL - ran out of budget"):
        assert verdict.classify(f"x\n{last}\n") == ("PARTIAL", "ran out of budget")


def test_an_empty_file_is_distinguished_from_a_missing_terminator():
    """The two failures need different handling, so they keep different details."""
    verdict = load("_verdict", "_verdict.py")
    assert verdict.classify("\n\n  \n")[1] == "empty"
    assert verdict.classify("findings\n")[1] == "no COMPLETE/PARTIAL terminator"


def test_both_readers_share_one_classifier():
    """Two terminator parsers drifted apart once; neither may grow its own again."""
    for name in ("await_layers.py", "ledger.py"):
        source = (SCRIPTS / name).read_text()
        assert "import _verdict" in source, name
        assert 'last == "COMPLETE"' not in source, name
        assert 'startswith("PARTIAL' not in source, name
