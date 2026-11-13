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
        "git show",
    ):
        assert token in text, token
    for rel in re.findall(r"\.\./review/scripts/[\w./-]+\.py", text):
        assert (skill.parent / rel).resolve().exists(), rel


FOLD = SCRIPTS.parents[1] / "fold-review" / "SKILL.md"


def _flat_path(path):
    return " ".join(path.read_text().split())


def _flat():
    return _flat_path(FOLD)


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


def test_fold_review_gates_every_reply_on_explicit_approval():
    """A reply body is the one write in this flow the user has to approve first."""
    block = _flat()
    assert "post only the ones they explicitly approve" in block


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


STACK = FOLD.parent / "stack-mode.md"
REVIEW = SCRIPTS.parent / "SKILL.md"
PIPELINE = SCRIPTS.parents[1] / "feature-pipeline" / "SKILL.md"


def test_stack_push_is_the_only_lease_exception_and_stays_on_own_branches():
    """Force-with-lease is allowed only for a stack cascade, with an expected SHA, on the gh user's own PRs."""
    fold, stack = _flat(), " ".join(STACK.read_text().split())
    assert (
        "stack-mode.md"
        in fold.split("### 5. Review the diff")[0].split("**Push before gating.**")[1]
    )
    assert "--force-with-lease=<branch>:<expected-sha>" in stack
    assert "never bare `--force`" in stack
    assert "--json author" in stack and "gh api user" in stack


def test_stack_rebase_is_verified_before_any_push():
    """Every dependent's rebase is checked for completion and scope before anything is pushed."""
    stack = " ".join(STACK.read_text().split())
    assert "git rebase --onto <new-parent-tip> <old-parent-tip>" in stack
    for token in (
        "rebase-merge",
        "the branch tip changed",
        "<parent>...<branch>",
        "push nothing",
    ):
        assert token in stack, token
    assert stack.index("Verify every rebase before any push") < stack.index(
        "git push --force-with-lease"
    )


def test_stack_gates_once_at_the_chain_tip_and_serialises_phase0():
    """A stack runs one gate at its tip and never launches its heavy Phase 0 runs side by side."""
    stack = " ".join(STACK.read_text().split())
    assert "One gate for the whole chain" in stack
    assert "one at a time" in stack
    assert "Phase 0 at a time" in " ".join(REVIEW.read_text().split())


def test_dispatch_waves_stay_under_the_cli_cap():
    """No message carries more agents than the CLI accepts; review no longer sends n_prs * 4 at once."""
    stack = " ".join(STACK.read_text().split())
    assert "16 in Claude, 6 in Codex" in stack
    review = " ".join(REVIEW.read_text().split())
    assert "n_prs * 4" not in review
    assert "stack-mode.md" in review


def test_per_pr_mode_uses_unnamed_inline_agents():
    """One background agent per PR, run --inline, never named or messaged."""
    stack = " ".join(STACK.read_text().split())
    for token in (
        "ONE unnamed background `Agent` per PR",
        "--inline",
        "never address them by SendMessage",
    ):
        assert token in stack, token


def test_every_required_dispatch_prompt_forbids_spawning_agents():
    """Each verbatim dispatch preamble carries the no-nested-agents sentence."""
    assert _flat().count("Do NOT post anything to GitHub. Do not spawn agents.") == 2
    for path in (REVIEW, PIPELINE):
        assert "Do NOT post anything to GitHub. Do not spawn agents." in " ".join(
            path.read_text().split()
        ), path
    assert '"Do not spawn agents."' in STACK.read_text()


def test_stack_push_stops_at_first_foreign_pr_and_resolves_only_pushed_folds():
    """Nothing above a PR the user does not own is pushed, and threads resolve only for folds on the remote."""
    stack = " ".join(STACK.read_text().split())
    assert "stops at the first PR the user does not own" in stack
    assert "folded locally, not pushed" in stack
    assert "run only for PRs whose fold reached the remote" in stack


def test_stack_folds_only_named_prs_each_with_its_own_input():
    """Walked-in PRs are only rebased; several inputs are accepted, one per PR."""
    stack = " ".join(STACK.read_text().split())
    assert "Only the PRs passed in are folded" in stack
    assert "for each named PR, with its own input" in stack
    fold = FOLD.read_text()
    assert "<pr-number-or-link-or-file-path>..." in fold
    assert "one input per PR" in " ".join(fold.split())
    assert "triaged `REVIEW-PR-<n>.md`" in " ".join(PIPELINE.read_text().split())


def test_stack_tips_recorded_once_and_ancestry_checked_before_rebase():
    """Old tips are fixed before the first fold, checked as ancestors, and the lease follows each push."""
    stack = " ".join(STACK.read_text().split())
    assert "Before the first fold" in stack
    assert "`<old-tip>` never changes during the flow" in stack
    assert "git merge-base --is-ancestor <old-parent-tip> HEAD" in stack
    assert "git reflog --format=%H <parent>" in stack
    assert stack.index("--is-ancestor") < stack.index(
        "git rebase --onto <new-parent-tip>"
    )
    assert "becomes the pushed SHA after each successful push" in stack


def test_stack_scope_check_uses_recorded_shas_and_handles_squash_merged_parent():
    """The file-scope check needs no gh file list, and a squash-merged parent's commits are rebased off."""
    stack = " ".join(STACK.read_text().split())
    assert "--json files" not in stack
    assert "git diff --name-only <old-parent-tip>...<old-tip>" in stack
    assert "squash-merged" in stack and "git rebase --onto origin/<base>" in stack


NEW_PR = FOLD.parent / "new-pr-mode.md"


def test_fold_review_reads_issue_comments_and_approved_review_bodies():
    """Edited bot comments are re-read by updated_at, approved reviews with a body count, and both are ledgered."""
    block = _flat()
    assert "gh api repos/<o>/<r>/issues/<n>/comments --paginate" in block
    assert "latest `updated_at`" in block and "every severity" in block
    assert "states: [CHANGES_REQUESTED, COMMENTED, APPROVED]" in block
    assert block.count("`<id> <updated_at>`") == 2


def test_fold_review_never_defers_and_folds_nits():
    """Every finding is fixed in this fold or rejected with a reason; nits are folded."""
    block = _flat()
    assert '"Defer to a follow-up" is not a disposition' in block
    assert "Nits are folded by default" in block


def test_fold_review_new_pr_mode_for_merged_or_closed_prs():
    """A merged or closed PR gets its fixes in a new draft PR, and folded threads point at it."""
    block = _flat()
    assert "merged or closed" in block and "new-pr-mode.md" in block
    assert "Opening a new PR" not in block
    mode = " ".join(NEW_PR.read_text().split())
    for token in (
        "defaultBranchRef",
        "gh pr create --draft",
        '"Fixed in #<new>."',
        "nothing is resolved there",
        "git fetch origin pull/<n>/head",
        "FETCH_HEAD",
    ):
        assert token in mode, token


def test_fold_review_updates_a_stale_pr_description():
    """After the push, stale description lines are rewritten on the gh user's own PR, otherwise reported."""
    block = _flat()
    assert "gh pr edit <n> --body-file <outdir>/pr-body.md" in block
    assert "otherwise list them in step 8" in block
    assert "PR description lines updated or listed as stale" in block


def test_fold_review_keeps_earlier_rejections_and_skips_status_bots():
    """A re-read comment keeps a prior spec's rejections, and status or report bot comments are not findings."""
    block = _flat()
    assert "If a previous one exists, read it first" in block
    assert "keeps that rejection and its reason" in block
    assert "skip status or report bots" in block


def test_outdated_threads_are_anchored_by_original_line():
    """An outdated thread reports line null, so the query fetches originalLine and the text says to use it."""
    block = _flat()
    assert "isOutdated path line originalLine" in block
    assert "anchor it by `originalLine`" in block


def test_post_flag_pre_approves_the_review_content():
    """`--post` means post: the body and inline comments go out without another ask."""
    assert (
        "`--post` is the user's approval of the review body and its inline comments: post without asking."
        in _flat_path(REVIEW)
    )


def test_post_replies_pre_approves_fold_replies_and_passes_through_the_pipeline():
    """`--post-replies` is a listed fold flag that skips the reply approval, and feature-pipeline forwards it."""
    fold, pipeline = _flat(), _flat_path(PIPELINE)
    assert "[--post-replies]" in FOLD.read_text().split("---")[1]
    assert (
        "- `--post-replies` - the user's approval of step 7's thread replies and resolutions"
        in fold
    )
    assert "unless `--post-replies` was passed" in fold
    assert "[--post-replies]" in PIPELINE.read_text().split("---")[1]
    assert "`--post-replies` passes through to every `fold-review` call" in pipeline


def test_pipeline_never_holds_back_its_review_phase():
    """Phase 4 starts after the last push without asking."""
    assert (
        "never hold it back for approval or offer it as an optional next step"
        in _flat_path(PIPELINE)
    )


def test_final_reports_list_every_step_as_ran_or_skipped():
    """Each skill's report names its steps so a skipped one is visible with its reason."""
    for path, steps in (
        (
            REVIEW,
            "Phase 0, project, hunter, edge, landmine, citations check, ledger, post",
        ),
        (
            FOLD,
            "Phase 0, spec review, implement, diff review, citations check, gate, push, PR description, replies, thread resolution",
        ),
        (
            PIPELINE,
            "Phase 0, spec split, spec review, implement, push, review, fold, validate",
        ),
    ):
        text = _flat_path(path)
        assert "`ran` or `skipped: <reason>`" in text, path
        assert steps in text, path
    assert "one line per step (per PR when reviewing a stack)" in _flat_path(REVIEW)


def test_trivial_fold_skips_only_a_small_insensitive_spec_review():
    """One finding in one non-sensitive, non-CI file may skip the spec review, and says so."""
    step3 = _flat().split("### 3. Review the spec")[1].split("### 4.")[0]
    for token in (
        "exactly one finding",
        "in one file",
        "sensitive-path list",
        "CI config",
        "`skipped: trivial change`",
    ):
        assert token in step3, token


def test_pipeline_checks_push_access_in_phase0():
    """Phase 0 verifies gh auth, remote reachability and push permission, and stops on failure."""
    phase0 = _flat_path(PIPELINE).split("## Phase 0")[1].split("## Phase 1")[0]
    for token in (
        "gh auth status",
        "for EACH repo the feature touches, from its checkout",
        "git -C <checkout> ls-remote origin HEAD",
        "gh api repos/{owner}/{repo} --jq .permissions.push",
        "stop and tell the user",
    ):
        assert token in phase0, token


def test_pipeline_opens_each_draft_with_its_spec_right_after_commit():
    """Each unit's draft PR opens right after its commit, described by that unit's spec."""
    phase3 = _flat_path(PIPELINE).split("## Phase 3")[1].split("## Phase 4")[0]
    assert "open its **draft** PR right after its commit" in phase3
    assert "that unit's spec" in phase3 and "do not wait for the other units" in phase3


def test_pipeline_raises_manual_production_steps_before_implementation():
    """A design needing manual operator action in prod is raised before Phase 3 and hunted in Phase 2."""
    text = _flat_path(PIPELINE)
    phase1 = text.split("## Phase 1")[1].split("## Phase 2")[0]
    assert "Manual operator action in production is a decisive constraint" in phase1
    assert "raise it to the user before Phase 3" in phase1
    assert "manual operator steps in production" in text.split("## Phase 2")[1]
    assert "manual operator action in production (Phase 1)" in text


def test_pipeline_caps_pr_count_by_merging_seams():
    """`--max-prs` is a listed flag; over the cap units merge by seam and the merge is reported."""
    assert "[--max-prs <n>]" in PIPELINE.read_text().split("---")[1]
    phase1 = _flat_path(PIPELINE).split("## Phase 1")[1].split("## Phase 2")[0]
    for token in (
        "merge units along their logical seams",
        "never exceed it",
        "say which units were merged",
        "the flag wins over the docs",
        "a docs cap stated per chain applies per chain",
    ):
        assert token in phase1, token
    final_report = _flat_path(PIPELINE).split("## Final report")[1]
    assert "spec split line names any units merged" in final_report


def test_style_rewrite_tool_gets_one_bounded_attempt():
    """An optional rewrite tool is tried once with a time limit, then the text is written by hand."""
    pr_comments = SCRIPTS.parents[1] / "pr-comments" / "SKILL.md"
    for path in (FOLD, pr_comments):
        text = _flat_path(path)
        for token in (
            "one attempt with a time limit",
            "write the text yourself",
            "never retry in a loop",
        ):
            assert token in text, (path.parent.name, token)


def test_reply_body_is_passed_from_a_file():
    """pr-comments' reply mutation reads its body from a file, never inline."""
    assert (
        "-F body=@<body-file>"
        in (SCRIPTS.parents[1] / "pr-comments" / "SKILL.md").read_text()
    )


def test_fold_worktree_setup_runs_in_step_2_before_step_3():
    """The worktree and outdir exist before the spec review writes into them."""
    text = FOLD.read_text()
    assert "setup_round.py" in text.split("### 2.")[1].split("### 3.")[0]
