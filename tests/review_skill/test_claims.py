import pytest

from .conftest import _git, commit_all
from .test_phase0 import load


def check():
    return load("k_claims", "checks/k_claims.py")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    return root


def seed(repo, files):
    for name, body in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    commit_all(repo, "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")


def change(repo, files):
    for name, body in files.items():
        (repo / name).write_text(body)
    commit_all(repo, "change")


GATE_BEFORE = '''def compaction_gate(provider):
    """Gated so it only activates on providers without server-side compaction."""
    return provider == AWS_BEDROCK
'''

GATE_AFTER = '''def compaction_gate(provider):
    """Gated so it only activates on providers without server-side compaction
    (AWS_BEDROCK / LLAMA_CPP)."""
    return provider == AWS_BEDROCK
'''


def test_prose_naming_something_the_code_never_references_is_reported(repo):
    """The corpus's R1-005 shape: comment names two providers, the gate checks one."""
    seed(repo, {"a.py": GATE_BEFORE, "other.py": "LLAMA_CPP = 'llama'\n"})
    change(repo, {"a.py": GATE_AFTER})
    out = check().run(str(repo), "origin/main")
    assert "LLAMA_CPP" in out
    assert "AWS_BEDROCK" not in out.split("says:")[1].split("- `")[1]


def test_prose_whose_referents_all_resolve_is_silent(repo):
    """A docstring that names only what its body touches is not a candidate."""
    seed(repo, {"a.py": GATE_BEFORE})
    change(
        repo,
        {
            "a.py": '''def compaction_gate(provider):
    """Returns True only for AWS_BEDROCK."""
    return provider == AWS_BEDROCK
'''
        },
    )
    out = check().run(str(repo), "origin/main")
    assert out.startswith("OK:")


def test_a_referent_defined_elsewhere_in_the_file_is_located(repo):
    """Cross-function claims: the invariant is stated here and enforced there."""
    body = '''def orchestrator_peer(labels):
    """Both selectors sit in one entry; _validated_peer enforces the pairing."""
    return {"podSelector": labels}


def _validated_peer(peer):
    return set(peer) <= {"podSelector"}
'''
    seed(repo, {"a.py": "def orchestrator_peer(labels):\n    return labels\n"})
    change(repo, {"a.py": body})
    out = check().run(str(repo), "origin/main")
    assert "_validated_peer" in out
    assert "elsewhere in this same file" in out


def test_a_referent_that_exists_nowhere_is_called_out(repo):
    """A stale name is the strongest shape: the prose points at code that is gone."""
    seed(repo, {"a.py": "def f(x):\n    return x\n"})
    change(
        repo,
        {
            # the referent sits on a continuation line, which carries no quote prefix,
            # so only the docstring line-span knows it is prose rather than a definition
            "a.py": '''def f(x):
    """Handle the deprecated path.

    Mirrors legacy_scope_resolver, which owns the same fallback.
    """
    return x
'''
        },
    )
    out = check().run(str(repo), "origin/main")
    assert "legacy_scope_resolver" in out
    assert "nowhere in the repo" in out


def test_plain_english_prose_is_not_mined_for_referents(repo):
    """Sentences that name nothing code-shaped must not manufacture candidates."""
    seed(repo, {"a.py": "def f(x):\n    return x\n"})
    change(
        repo,
        {
            "a.py": '''def f(x):
    """Return the value unchanged so the caller can decide what to do next."""
    return x
'''
        },
    )
    out = check().run(str(repo), "origin/main")
    assert out.startswith("OK:")


def test_docstrings_outside_the_diff_are_not_scanned(repo):
    """Only claims this diff touched are in scope, or every review reopens the tree."""
    untouched = '''def old(x):
    """Delegates to vanished_helper for the legacy path."""
    return x
'''
    seed(repo, {"a.py": untouched + "\n\ndef g(y):\n    return y\n"})
    change(repo, {"a.py": untouched + "\n\ndef g(y):\n    return y + 1\n"})
    out = check().run(str(repo), "origin/main")
    assert "vanished_helper" not in out


def test_findings_are_framed_as_candidates_not_defects(repo):
    """A resolved referent proves nothing about the sentence, and the text must say so."""
    seed(repo, {"a.py": GATE_BEFORE, "other.py": "LLAMA_CPP = 'llama'\n"})
    change(repo, {"a.py": GATE_AFTER})
    out = check().run(str(repo), "origin/main")
    assert "CANDIDATE" in out
    assert "not a defect" in out


def test_abbreviations_are_not_treated_as_dotted_referents():
    """`e.g.` and `i.e.` parse as dotted names and would be reported on every docstring."""
    found = check().referents("Handles the fallback, e.g. when the pool is empty.")
    assert "e.g" not in found


def test_a_docstring_never_resolves_against_its_own_text(repo):
    """The docstring is a string constant in its own body; counting it passes everything."""
    seed(repo, {"a.py": "def f(x):\n    return x\n"})
    change(
        repo,
        {
            "a.py": '''def f(x):
    """Delegates to missing_helper_name for the fallback."""
    return x
'''
        },
    )
    out = check().run(str(repo), "origin/main")
    assert "missing_helper_name" in out


def test_a_symbol_named_only_inside_a_string_literal_still_resolves(repo):
    """A test asserting on `"await_layers.py ..."` does reference await_layers."""
    seed(repo, {"a.py": "def t():\n    assert True\n"})
    change(
        repo,
        {
            "a.py": '''def t():
    """Pins that await_layers is named in the call site."""
    assert "await_layers.py <outdir>" in BLOCK
'''
        },
    )
    out = check().run(str(repo), "origin/main")
    assert out.startswith("OK:")
