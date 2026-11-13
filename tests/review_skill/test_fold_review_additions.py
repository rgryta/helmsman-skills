import re

from .conftest import SCRIPTS

FOLD = SCRIPTS.parents[1] / "fold-review" / "SKILL.md"


def test_fold_review_has_distillation_and_ledger_steps():
    skill = SCRIPTS.parents[1] / "fold-review" / "SKILL.md"
    text = skill.read_text()
    for token in (
        "semgrep-rules",
        "ledger.py",
        "set-status",
        "candidate rule",
        "re-verify",
        "git show",
    ):
        assert token in text, token
    for rel in re.findall(r"\.\./review/scripts/[\w./-]+\.py", text):
        assert (skill.parent / rel).resolve().exists(), rel


FOLD = SCRIPTS.parents[1] / "fold-review" / "SKILL.md"


def _flat():
    return " ".join(FOLD.read_text().split())


def test_fold_review_writes_no_artifact_into_the_repo():
    """No diff-review artifact path points inside the worktree the fold is committed in."""
    text = FOLD.read_text()
    for artifact in ("REVIEW-", "FACTS-", "CONTEXT-"):
        assert f"<worktree>/{artifact}" not in text, artifact


def test_fold_review_defines_its_outdir_outside_every_repo():
    """The fold's artifact directory is created per run, outside any checkout."""
    block = _flat()
    assert "~/.helmsman/review-ledger/" in block
    assert "fold-<epoch>" in block
    assert "mkdir -p" in block


def test_fold_review_diff_review_binds_the_outdir():
    """The diff-review dispatch and its wait both name <outdir> in the argument position."""
    block = _flat()
    assert "await_layers.py <outdir> <n> hunter,edge,landmine 540 5" in block
    assert "<outdir>/REVIEW-<n>-<layer>.md" in block
    assert "NO_OUTDIR" in block


def test_fold_review_citation_check_keeps_the_worktree_first():
    """Arg 1 resolves cited code, so only the findings glob moves to <outdir>."""
    block = _flat()
    assert "verify_citations.py <worktree> <outdir>/REVIEW-<n>-*.md" in block
    assert "vacuously" in block


def test_fold_review_requires_a_non_vacuous_test_proof():
    """A test the fold adds must be shown to fail with its fix reverted."""
    block = _flat()
    assert "Revert the code change that test defends" in block
    assert "FAILS" in block


def test_fold_review_cleanup_removes_the_artifact_directory():
    """The per-fold artifact directory is deleted once the report is written."""
    block = _flat()
    assert "delete `<outdir>`" in block


def test_fold_review_sibling_sweep_starts_from_the_facts_section():
    """The deterministic list comes first; the manual grep extends it, not replaces it."""
    text = _flat()
    assert "Start from the `j_siblings` section of `FACTS-<n>.md`" in text
    assert "starting point, not the answer" in text


def test_fold_review_gates_on_a_wider_pytest_scope_than_a_review():
    """A fold gate widens the test scope a review's fact pass uses."""
    text = " ".join(FOLD.read_text().split())
    assert "HELMSMAN_PYTEST_SCOPE=gate" in text
    assert "HELMSMAN_PHASE0_CHECK_S=270" in text


def test_fold_review_treats_a_gate_without_a_verdict_as_a_failure():
    text = " ".join(FOLD.read_text().split())
    assert "no verdict is not a pass" in text
    assert "anything other than OK or FINDINGS" in text


def test_fold_review_replies_instead_of_resolving_what_it_did_not_fix():
    """A finding fixed elsewhere or rejected gets a thread reply, never a resolve."""
    block = _flat()
    assert "Reply on every thread this diff did not fix" in block
    assert "never resolve it, reply on the thread instead" in block
    assert "addPullRequestReviewThreadReply" in block


def test_fold_review_gates_every_reply_on_explicit_approval():
    """A reply body is the one write in this flow the user has to approve first."""
    block = _flat()
    assert "post only the ones they explicitly approve" in block
    assert (
        "Never post an unapproved body, and never resolve the thread as a substitute"
        in block
    )


def test_fold_review_reply_body_never_passes_through_the_shell():
    """Bodies are read from a file, so backticks and dashes survive the post."""
    block = _flat()
    assert "-F body=@<outdir>/reply-<thread-id>.md" in block


def test_fold_review_spec_records_the_thread_and_a_three_way_disposition():
    """Step 7 can only pick reply-or-resolve if step 2 recorded the ID and the case."""
    block = _flat()
    assert "record the thread ID captured in step 1" in block
    assert (
        "fixed in this PR (and how), fixed in a different PR or commit (and which), "
        "or explicitly rejected (and why)" in block
    )


def test_fold_review_never_writes_into_the_installed_skill_copy():
    """Candidate rules land outside the plugin install, and the probe file is not a fixed /tmp path."""
    block = _flat()
    assert "semgrep-candidates/<kebab-id>.yaml" in block
    assert "/tmp/prefix-probe.py" not in block
    assert 'probe="$(mktemp -d)/prefix-probe.<ext>"' in block


def test_fold_review_paginates_and_skips_folded_review_bodies():
    """No fixed page size truncates feedback, and a folded review body is never re-folded."""
    block = _flat()
    assert "first: 10)" not in block and "first: 50" not in block
    assert "Paginate `reviewThreads` and `reviews`" in block
    assert block.count("folded-reviews.txt") >= 2


def test_fold_review_file_input_wins_over_live_threads():
    """A local file is the findings source; live state only adds IDs and newer feedback."""
    block = _flat()
    assert "The file wins" in block
    assert "no exceptions" not in block
    assert "Layer 1" not in block and "Layer 3" not in block


def test_fold_review_ledgers_every_disposed_review_body():
    """Rejected review bodies are recorded too, so a later fold never re-folds them."""
    block = _flat()
    assert (
        "every review body the spec gave a disposition, whatever the disposition"
        in block
    )


def test_fold_review_file_input_keeps_live_filters_and_reports_drops():
    """Live additions to a file fold obey the same filters, and mtime drops are reported."""
    block = _flat()
    assert "kept by the same `isResolved` and `folded-reviews.txt` filters" in block
    assert "first comment's `createdAt`" in block
    assert "step 1 dropped for predating the review file" in block
    assert "in its own follow-up query" in block


def test_fold_review_pushes_before_it_gates():
    """The gate runs on the pushed head, so a failure is a follow-up commit, never a held push."""
    text = FOLD.read_text()
    assert text.index("**Push before gating.**") < text.index("### 5. Review the diff")
    assert "never a held push" in " ".join(text.split())


def test_ci_carries_a_local_test_run_that_gave_no_verdict():
    """A local timeout or skip defers to green or pending CI; a local FAIL never does."""
    block = " ".join((SCRIPTS.parent / "test-gate-exceptions.md").read_text().split())
    assert "checks in `skipping`" in block and "do not count either way" in block
    for token in ("gh pr checks", "`pass` or `pending`", "FAIL` always counts"):
        assert token in block, token
