import json
import re

from .conftest import SCRIPTS
from .test_phase0 import load

SKILL = SCRIPTS.parent / "SKILL.md"
HUNTER = SCRIPTS.parent / "hunter.md"
EDGE = SCRIPTS.parent / "edge.md"


def test_skill_references_resolve():
    text = SKILL.read_text()
    refs = re.findall(r"scripts/[\w./-]+\.(?:py|yaml)", text)
    assert refs
    for rel in refs:
        assert (SCRIPTS.parent / rel).exists(), rel


def test_skill_encodes_contracts():
    text = SKILL.read_text()
    for token in (
        "phase0.py",
        "ledger.py",
        "classify-report",
        "COMPLETE",
        "PARTIAL:",
        "CONTEXT-",
        "REVIEW-<n>-",
        "FULL_REHUNT",
        "never named",
        "run_in_background",
        "45000 * n_prs",
        "merge-base --is-ancestor",
        "gh auth status",
        "await_layers.py",
        "fanout_check.py",
        "STALLED:",
        "verify_citations.py",
        "i_neighborhood",
        "<outdir>",
        "NO_OUTDIR",
    ):
        assert token in text, token
    assert "bmad-code-review" not in text.split("## History")[0]


def step6():
    return SKILL.read_text().split("6. **Bounded wait")[1].split("7. **Dedup")[0]


def test_skill_bounds_the_layer_wait():
    """The layer wait must be a bounded foreground call, never an open-ended or self-scheduled one."""
    text = SKILL.read_text()
    body = text.split("## History")[0]
    assert "timeout: 600000" in body
    assert "never schedule a wakeup" in body
    assert "ScheduleWakeup" not in body


def test_phase0_uses_a_managed_process_lifecycle():
    fold = (SCRIPTS.parent.parent / "fold-review" / "SKILL.md").read_text()
    blocks = (
        SKILL.read_text().split("3. **Phase 0")[1].split("4. **Context pack")[0],
        fold.split("**Facts first, unconditionally")[1].split("**Sizing")[0],
    )
    for text in blocks:
        block = _flat(text)
        assert "run_phase0.py" in block
        assert "managed background Bash tool call" in block
        assert "retain its task or session handle" in block
        assert "never append `&`, `nohup` or `disown`" in block
        assert "do not poll only for the facts file" in block.lower()
        assert "exit/signal and stderr" in block


def test_plugin_manifests_ship_the_same_version():
    plugin = SCRIPTS.parents[2]
    claude = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text())
    codex = json.loads((plugin / ".codex-plugin" / "plugin.json").read_text())
    assert claude["version"] == codex["version"]


def test_stall_requires_a_grace_wait_before_any_retry():
    """A STALLED layer gets a second bounded wait first, so a nearly-done layer is not duplicated."""
    block = step6()
    assert "240 5" in block
    assert "NOT grounds to retry yet" in block
    assert "STRAIGHT to the retry path" not in block


def test_retry_stops_the_original_agent_first():
    """Retrying a dead layer stops its original agent, so two agents never race on one findings file."""
    block = step6()
    assert "STOP its original agent" in block
    assert "race" in block


def test_every_wait_including_the_retry_runs_through_the_script():
    """The retry's wait is bounded by the same script rather than hand-polled."""
    block = step6()
    retry = block.split("Missing file or")[1]
    assert "await_layers.py" in retry
    assert "never poll by hand" in block
    assert "ListAgents` is not a wait loop" in block


def test_every_external_layer_skill_is_fanout_checked():
    """Both externally-sourced layers route through fanout_check rather than a prose assertion."""
    body = SKILL.read_text().split("## History")[0]
    project_block = body.split("- Layer `project`:")[1].split("- Layer `landmine`:")[0]
    landmine_block = body.split("- Layer `landmine`:")[1].split("- Layer `hunter`:")[0]
    assert "fanout_check.py" in project_block
    assert "fanout_check.py" in landmine_block
    assert "spawns no" not in landmine_block


def step7():
    return SKILL.read_text().split("7. **Dedup")[1].split("8. **Write")[0]


def test_citations_are_machine_checked_before_the_review_is_written():
    """Every layer's cited positions are verified against the real files, not trusted."""
    block = step7()
    assert "verify_citations.py" in block
    assert "REVIEW-<n>-*.md" in block
    assert "BAD" in block


def test_boundary_facts_must_be_accounted_for():
    """The neighborhood section is named as the surface a gate must be cleared against."""
    block = step7()
    assert "i_neighborhood" in block
    for token in ("before", "after", "caller"):
        assert token in block, token


def test_edge_carries_the_boundary_sweep():
    """Enumerating a guard's input shapes and neighbours is the edge layer's job."""
    t = EDGE.read_text()
    assert "Boundary sweep" in t
    assert "i_neighborhood" in t
    for token in ("shape", "before", "after", "caller"):
        assert token in t.lower(), token


def test_hunter_defers_boundary_enumeration_rather_than_duplicating_it():
    """Two layers re-enumerating one seam is the cost the split was meant to avoid."""
    t = HUNTER.read_text()
    assert "belongs to the `edge` layer" in t
    assert "Boundary sweep (" not in t


def test_hunter_brief_exists():
    t = HUNTER.read_text()
    for token in ("blind", "acceptance", "COMPLETE", "Do NOT post"):
        assert token.lower() in t.lower(), token


def test_edge_brief_exists_and_owns_one_job():
    t = EDGE.read_text()
    for token in ("boundary", "None", "COMPLETE", "Do NOT post"):
        assert token.lower() in t.lower(), token
    assert "acceptance auditor" not in t.lower()


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


def step1():
    return SKILL.read_text().split("1. **Preflight")[1].split("2. **Ledger")[0]


def step9():
    return SKILL.read_text().split("9. **Cleanup")[1].split("## --post")[0]


def _flat(block):
    return " ".join(block.split())


def test_no_skill_artifact_is_written_into_the_repo():
    """The house rule holds structurally: no per-run artifact path points inside a worktree."""
    for path in (SKILL, HUNTER):
        text = path.read_text()
        for artifact in ("REVIEW-", "FACTS-", "CONTEXT-"):
            assert f"<worktree>/{artifact}" not in text, f"{path.name} {artifact}"


def test_outdir_is_defined_outside_every_repo_and_fresh_per_run():
    """The artifact directory is introduced before use, lives outside repos, and is per-run."""
    block = _flat(step1())
    assert "<outdir>" in block
    assert "~/.helmsman/review-ledger/" in block
    assert "run-<epoch>" in block
    assert "mkdir -p" in block


def test_every_wait_and_report_call_binds_the_outdir():
    """Each await_layers and classify-report call site names <outdir> in the argument position."""
    block = _flat(step6())
    for call in (
        "await_layers.py <outdir> <n> project,landmine,hunter,edge 540 5",
        "await_layers.py <outdir> <n> <stalled-layers> 240 5",
        "await_layers.py <outdir> <n> <layer> 540 5",
        "classify-report --file <outdir>/REVIEW-<n>-<layer>.md",
    ):
        assert call in block, call


def test_citation_check_keeps_the_worktree_as_its_first_argument():
    """Arg 1 resolves cited code, so only the findings glob moves to <outdir>."""
    block = _flat(step7())
    assert "verify_citations.py <worktree> <outdir>/REVIEW-<n>-*.md" in block
    assert "vacuous" in block


def test_dispatch_preamble_requires_the_outdir_to_be_resolved():
    """Subagents inherit no cwd, so <outdir> must be resolved like <worktree> and <n>."""
    block = _flat(SKILL.read_text().split("Every dispatch prompt MUST include")[1])
    assert "<outdir>" in block[:400]
    assert "<outdir>/REVIEW-<n>-<layer>.md" in block


def test_cleanup_removes_the_run_directory():
    """The per-run artifact directory is deleted alongside the worktree it accompanied."""
    assert "<outdir>" in _flat(step9())


def test_hunter_writes_its_findings_outside_the_repo():
    """The hunter brief's own hardcoded output path honours the artifact directory."""
    text = HUNTER.read_text()
    assert "<outdir>/REVIEW-<n>-hunter.md" in text
    assert "<outdir>/CONTEXT-<n>.md" in text
    assert "<outdir>/FACTS-<n>.md" in text


def test_hunter_carries_the_sibling_sweep():
    """The largest recurring finding class needs a named sweep, not incidental reading."""
    text = HUNTER.read_text()
    assert "j_siblings" in text
    assert "worklist" in text
    assert "reworded" in text


def test_sibling_facts_must_be_accounted_for():
    """A fix that lands on one site while its listed twins go unmentioned is incomplete."""
    block = step7()
    assert "j_siblings" in block
    assert "incomplete" in block


def test_hunter_carries_the_claim_sweep():
    """Prose is a claim; the acceptance-auditor brief needs it named, not implied."""
    text = HUNTER.read_text()
    assert "k_claims" in text
    assert "never raises" in text


def test_pr_claims_are_checked_against_the_gate_facts():
    """A 'tests pass' claim is falsified for free by facts already in the same file."""
    block = step7()
    assert "k_claims" in block
    for gate in ("a_ruff", "b_pyright", "c_pytest"):
        assert gate in block


def test_hunter_carries_the_test_shape_sweep():
    """Vacuous tests are 16% of findings; the sweep needs naming, not implying."""
    text = HUNTER.read_text()
    assert "l_test_shape" in text
    assert "were reverted" in text


def test_test_shape_and_mutation_are_paired_in_the_review():
    """One covers the production side of a weak test, the other the test side."""
    block = step7()
    assert "l_test_shape" in block
    assert "h_mutation" in block


def test_checks_import_their_helpers_directly():
    """Checks import _diff/_env as modules so pyright can type the reference."""
    for path in (SCRIPTS / "checks").glob("*.py"):
        assert "spec_from_file_location" not in path.read_text(), path.name


def severity():
    return SKILL.read_text().split("## Severity")[1].split("## Output format")[0]


def post():
    return SKILL.read_text().split("## --post mode")[1].split("## Severity")[0]


def test_severity_buckets_map_onto_the_bmad_scale():
    """Our headings and a raw bmad-code-review must grade the same PR the same way."""
    text = _flat(severity())
    for heading, bmad in (
        ("Blockers", "high"),
        ("Should-fix", "medium"),
        ("Nitpicks", "low"),
    ):
        assert f"| {heading} | {bmad} |" in text


def test_severity_is_consumer_consequence_rated_from_the_call_site():
    """The miss it prevents: grading a defect by whether data escaped, from inside the hunk."""
    text = _flat(severity())
    assert "consequence for the artifact's main consumer" in text
    assert "Rate from the call site, never from the hunk" in text
    assert "Nothing leaks" in text and "not graded yet" in text


def test_disposition_is_a_separate_axis_from_severity():
    """Age decides who owns a finding, never how bad it is."""
    text = _flat(severity())
    assert "Age is a disposition, never a severity" in text
    assert "this diff" in text and "pre-existing" in text


def test_layer_severities_are_re_rated_and_demotions_are_explained():
    text = _flat(severity())
    assert "re-rate" in text and "inherit none" in text
    assert "carries why in one sentence" in text


def verdict():
    return SKILL.read_text().split("\n## Verdict\n")[1].split("## Output format")[0]


def test_verdict_withholds_approval_where_the_round_cannot_see():
    """A blocker, a self-inflicted should-fix, a blind gate, or an open severity question."""
    text = _flat(verdict())
    assert "any Blocker -> COMMENT" in text
    assert "Should-fix whose disposition is `this diff` -> COMMENT" in text
    assert (
        "gate section (`a_ruff`, `b_pyright`, `c_pytest`) that is neither OK nor"
        in text
    )
    assert "PARTIAL" in text and "`ERROR:` section" in text
    assert "would change a severity -> COMMENT" in text
    assert "never REQUEST_CHANGES" in text


def test_the_local_recommendation_obeys_the_same_verdict_rule():
    """A review file read by a human is where the demotion actually lands."""
    text = _flat(verdict())
    assert "REVIEW-PR-<n>.md" in text
    body = SKILL.read_text().split("## History")[0]
    assert "recommendation (per `## Verdict`)" in body


def test_post_mode_defers_to_the_shared_verdict_rule():
    text = _flat(post())
    assert "Apply `## Verdict`" in text
    assert "carries every Blocker and Should-fix" in text


def test_posted_body_carries_every_finding_that_gates_the_verdict():
    text = _flat(post())
    assert "carries every Blocker and Should-fix" in text
    assert "inline comment is not a substitute" in text
    assert "Nitpicks may stay inline" in text


def test_hunter_findings_name_the_consumer_behind_the_severity():
    text = _flat(HUNTER.read_text())
    assert "what the consumer of the wrong value does differently" in text


def test_balanced_layer_model_is_resolved_for_the_active_cli():
    """The cheaper layer tier stays portable without falling back to economy models."""
    body = SKILL.read_text().split("## History")[0]
    model_block = body.split("**Model and effort per layer**")[1].split(
        "- Layer `project`:"
    )[0]
    assert "Claude Code `model: sonnet`" in model_block
    assert "Codex `model: gpt-5.6-terra`" in model_block
    assert "`reasoning_effort: medium`" in model_block
    assert "Gemini Pro-tier" in model_block
    assert "Haiku, Luna, Flash, or Mini" in model_block


def test_edge_is_dispatched_as_its_own_balanced_layer():
    """The split buys a brief, not a tier: edge shares hunter's balanced model."""
    body = SKILL.read_text().split("## History")[0]
    block = _flat(body.split("- Layer `edge`:")[1].split("NEVER wrap a skill")[0])
    assert "edge.md" in block
    assert "same balanced model at medium effort" in block
    assert "m_bounds" in block


def test_related_workflows_resolve_the_same_portable_model_tier():
    for skill_name in ("fold-review", "feature-pipeline"):
        text = (SCRIPTS.parent.parent / skill_name / "SKILL.md").read_text()
        assert "Claude Code `model: sonnet`" in text
        assert "Codex `model: gpt-5.6-terra`" in text
        assert "`reasoning_effort: medium`" in text
        assert "Gemini Pro-tier" in text
        assert "Haiku, Luna, Flash, or Mini" in text


def test_edge_writes_its_findings_outside_the_repo():
    text = EDGE.read_text()
    assert "<outdir>/REVIEW-<n>-edge.md" in text
    assert "<outdir>/CONTEXT-<n>.md" in text
    assert "<outdir>/FACTS-<n>.md" in text


def test_edge_carries_the_bound_sweep():
    """A deterministic section is inert unless a layer is told to answer it."""
    text = EDGE.read_text()
    assert "m_bounds" in text
    assert "one end" in text


def test_bound_facts_must_be_accounted_for():
    """Collation reads the same section, so an unanswered row is visible there too."""
    block = step7()
    assert "m_bounds" in block
    assert "caller-supplied" in block


def test_both_skills_warn_before_launching_phase0_on_uncommitted_work():
    """Agents have lost folds to the repair; the warning sits at the launch site."""
    fold = (SCRIPTS.parent.parent / "fold-review" / "SKILL.md").read_text()
    for text in (SKILL.read_text().split("4. **Context pack")[0], fold):
        flat = _flat(text)
        assert "git restore" in flat and "unrecoverable" in flat
        assert "COMMIT FIRST" in flat or "Commit before you launch it" in flat
