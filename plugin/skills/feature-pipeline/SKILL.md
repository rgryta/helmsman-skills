---
name: feature-pipeline
description: Use when shipping a non-trivial feature - work that spans multiple PRs, needs design before code, or arrives as "close the gap of X, Y, Z" / "build out ..." / "each piece its own PR". Not for one-line fixes.
argument-hint: "[feature/goal description] [--inline] [--skip-review]"
allowed-tools: Bash, Read, Grep, Glob, Write, Edit, Task, Agent, Skill, WebSearch, WebFetch
---

# feature-pipeline

Orchestrates a feature from a vague goal to reviewed, validated draft PRs. It is
the connective tissue over existing skills (rtfc, a brainstorming/PRD skill
if installed, review, fold-review, landmine-review) plus the repo-specific mechanics
and gotchas that make a multi-PR effort actually land.

`--inline` and `--skip-review` pass through unchanged to every
`/helmsman-skills:review` and `/helmsman-skills:fold-review` call in Phase 4.

Run the phases in order. Each phase has a gate: do not start the next until the
current one's output exists. Track phases with the task tools.

## Human involvement — build the whole chain, then push drafts (no approval gate)
Run Phases 0→4 **autonomously** and do NOT stop for approval of the specs, the PR
split, the design, or the push. **Draft PRs are pre-authorized** by this pipeline: build
**every** unit in worktrees, **push the branches and open the PRs as drafts yourself**
as each is committed (Phase 3), then review them (Phase 4) — there is no "ready-to-push"
gate and no waiting. Do NOT stop half-built to ask which option the user prefers; build the full
chain first. After pushing, report per branch: a one-line summary, the diff stat, the
gate results (the project's lint/type/test gates), and (if any PRs stack) the merge order.

What still needs an **explicit ask**: marking a PR **ready for review**, **merging**, or
any non-draft outward action (posting comments, flipping prod flags). Those are never
implied by this pipeline.

The only reasons to pause *before* pushing the drafts: a *decisive external constraint*
surfaced in Phase 0 (auth/credentials/provisioning/cost) that changes scope, or a genuine
fork the specs can't resolve from the request + codebase. Otherwise, silence between the
request and the pushed drafts is expected — narrate progress, don't ask permission, and
don't leave the chain half-built.

## When to use
- The goal touches multiple concerns / would be more than one PR.
- It needs design before code (new subsystem, provider integration, migration).
- "Close the gap of X, Y, Z" / "build out …" / "each piece its own PR".

For a one-line fix, skip this — just do it.

## Phase 0 — Gather context (never skip)
Cast wide before writing anything. Pull from every source that exists, in
parallel where possible:
- **Conversation history** — invoke `rtfc find the past session about <topic>`;
  its search mode picks the best-matching session and extracts its decisions.
- **Reference branches / prior PRs** — `git branch -a`, `gh pr list --search`,
  read any `[DO NOT MERGE - REFERENCE]` branches. Past attempts encode constraints.
- **The project's issue tracker** — find the project/epic, list its issues,
  read the spike/ADR tickets. Map Done vs Todo vs Cancelled — cancelled spikes
  often record *why an approach was rejected*.
- **The project's docs/wiki** — search for strategy/setup/gap-analysis pages;
  fetch the canonical ones.
- **Team chat** — if you have access, search the relevant channels for the
  latest direction (decisions often live here before they reach the tracker
  or docs).
- **Web + context7** — for any external service/SDK/API the feature depends on,
  verify the CURRENT reality (GA dates, auth model, API shapes). Training data
  rots; a wrong assumption here poisons every downstream spec. context7 for
  library docs, WebSearch/WebFetch for service docs.
- **Codebase** — dispatch a read-only exploration subagent to map the
  exact integration points (the template to mirror, the wiring seam, the
  contracts, the config surface). Demand file:line citations.

**Output:** a written understanding of the gaps + the load-bearing constraints.
Surface any *decisive* external constraint (auth/credentials, a provisioning
dependency, a cost) to the user EARLY — before writing specs — if it changes
scope or the validation plan. This is the one place a blocking question is
worth it; getting it wrong wastes the whole pipeline.

## Phase 1 — Split into units, then author atomic specs (brainstorming)
First decide the split; then write one **complete, atomic** spec per unit.
Invoke a brainstorming skill (or a PRD/architecture skill for larger
efforts) to drive it, if one is installed; otherwise drive it yourself.

**How to split — logical seams, sized for review.** The goal is 1→N PRs each
small enough to review by hand (target **≤500–800 changed lines**, tests
included). There is NO fixed PR count — a small feature is one PR; a large one
is however many its natural seams produce. Split along whichever of these draws
the cleanest boundaries:
- **By service / deployable** — API vs frontend vs gitops/infra vs migration.
- **By library / package** — a new internal library is its own unit; follow
  the repo's existing package-naming convention rather than inventing one.
- **By logic / concern** — foundation vs each consumer; read-path vs write-path.

Prefer the fewest PRs that each stay under the size budget and each stand alone
(review + revert without dragging the others). Only split further when a unit
would blow the line budget or mix unrelated concerns; only merge units when the
combined diff still reviews cleanly. If the whole feature fits one reviewable
PR, ship one PR — do not manufacture a chain.

- Each spec is self-contained: goal, scope, architecture, contracts, security,
  merge-safety, testing, rollout, open decisions.
- Order for **no regression at each merge step**: shared foundation first
  (dormant, default-off), then each consumer. A foundation PR that registers
  nothing and changes no runtime path is a valid standalone unit — and if two
  units have no code dependency, they target main independently (no chain).
- Write specs to **project root** (see `../review/conventions.md`) as
  `<feature>-NN-<name>-spec.md`. NEVER put specs in a repo subtree.
- Bake the decisive constraints from Phase 0 into every spec (e.g. "this auth
  path needs IAM, not the bearer token").

## Phase 2 — Adversarial spec review + fold-in
Review the SPECS before any code. For each spec, dispatch `../review/hunter.md`
and `../review/edge.md` (relative to this skill's base dir - `review` and
`feature-pipeline` are sibling plugin skills) as two `Agent` calls,
on the model in `../review/model-choice.md`. These are the same `hunter` and `edge` prompts
`review` and `fold-review` already use, and they are collected the same way:
the calls return at once, so keep each task id, never address an agent by
name or collect it via SendMessage relays (idle notifications have stalled
here with no result ever routing back), and read results only from disk. For
N specs, dispatch 2N of these in ONE message (two per spec) - nothing else in
this phase proceeds until all are collected. Delete any existing
`<spec-dir>/REVIEW-<spec-stem>-{hunter,edge}.md` first: a previous round's
file already ends in COMPLETE and would be collected as this round's result.

Adapt each prompt's "read CONTEXT/FACTS" preamble per dispatch to: "Do NOT
post anything to GitHub. Read `<spec-path>` and the Phase 0 context notes
first (their actual absolute paths - you do not inherit the orchestrator's
cwd). Apply your briefs to this spec text, additionally grounded in the verified Phase 0
facts so it flags fiction, not just style. Write findings to
`<spec-dir>/REVIEW-<spec-stem>-<layer>.md` (same directory as the spec -
project root, never a repo checkout); last non-blank line exactly COMPLETE
or PARTIAL: <reason>." Then, per spec, run `python3
../review/scripts/await_layers.py <spec-dir> <spec-stem> hunter,edge 540 5`
(foreground, timeout 600000), with `<spec-stem>` the spec's filename minus
`-spec.md`; on `STALLED:`, one grace re-wait at 240s for the stalled layer
alone. Never a bare "scratch file" with an unresolved
location: a layer told to write into a repo checkout can have its write denied outright and
then read as a stalled layer with no findings ever recovered. Hunt:
contradictions with ground truth, chain-safety holes ("would merging PRx
actually regress?"), hand-waved "reuse the existing path" claims, missing
failure modes, completeness ("does the set actually close the whole gap?").

If `../review/hunter.md` or `../review/edge.md` doesn't exist (an
out-of-date `review` install), report that explicitly and skip that layer for the affected spec(s) rather
than blocking the pipeline on missing tooling - do not invent reviewer
instructions to fall back to. A layer still stalled after its grace wait,
or whose file is missing or erroring, is stopped and treated as absent:
proceed on whichever specs did return findings rather than retrying.

Fold every genuine finding back into the specs (rev the docs). This gate
routinely catches wrong-template and wrong-seam assumptions that would cost a
rewrite later.

## Phase 3 — Implement: one worktree + draft PR per spec
One worktree + one draft PR per spec. Branch off `origin/main`.
- **Worktrees** per `../review/conventions.md` (default `<repo>-worktrees/<branch>`);
  never work in the main checkout.
- **Independent by default:** each PR targets `main` directly. Most logical-split
  units (different service, different package, unrelated concern) have no code
  dependency — do NOT stack them.
- **Stack ONLY for a genuine code-level dependency** (a shared new lib PR2 imports
  from PR1). Then PR2's GitHub base is PR1's branch, retargeting main as PR1
  merges. Stacking guarantees a clean sequential merge; use it for the dependent
  units only, not the whole set.
- **Delegate the bulk** of a large PR to an implementation subagent suited to
  the repository language, with the spec +
  the exact template/seam + the gotchas below; then YOU verify gates, review the
  diff, and commit locally. Subagents must NOT commit. Push each branch and open
  its **draft** PR right after its commit (see "Human involvement" — drafts are
  pre-authorized; no gate), then run Phase 4 over all of them. Do not stop
  half-built to ask the user which unit to build or whether to push.
- **Before every push** (from the worktree): the format-and-lint step in
  `../review/conventions.md`, plus the project's type-check (e.g. `uv run
  pyright --level error`) and the unit's new tests only. The suite runs in CI
  and in Phase 4's Phase 0 on the pushed head, never as a per-commit local
  run; a failure there becomes a follow-up fix commit, never a held push.
  - **New-test regression proof**: for every NEW test added as part of this
    implementation (not pre-existing tests already in the suite): capture the
    production-code hunk the test is meant to cover (`git diff -- <changed-file>
    > /tmp/fix-<n>.patch`), reverse-apply it (`git apply -R /tmp/fix-<n>.patch`
    - never `git stash`, its ref is repo-wide and pops into whichever worktree
    touches it next), re-run the specific new test, confirm it FAILS, then
    reapply (`git apply /tmp/fix-<n>.patch`) and confirm it passes again. A new
    test that stays green with the fix reverted is not a real regression guard
    - strengthen the assertion or setup until reverting the fix actually
    breaks it, don't commit it as-is. If the fix hunk and the new test aren't
    cleanly separable for a reverse-apply (e.g. they touch the same lines),
    fall back to a manual read-through confirming the assertion would fail
    without the fix, and say so explicitly in the gate report rather than
    skipping the proof silently.
- PR descriptions follow `../review/conventions.md` (default: ≤10 lines,
  professional, no emojis, no coauthoring). State clearly what's deferred.

## Phase 4 — review + fold-in
Run `/helmsman-skills:review <pr numbers>` (the target repo's own project review
skill, if it has one, + hunter + landmine-review) on all the PRs - writes
`REVIEW-PR-<n>.md` per PR at project root.

**Triage** findings across the whole stack: a finding reported against PR2's
diff can belong to PR1 if PR2 only stacks on PR1's code - reattribute to
**the proper PR** (the one that introduced the issue) by editing that PR's
own `REVIEW-PR-<n>.md` in place, not tracking it separately - the next step
feeds that exact file to `fold-review`, which has no other channel for a
cross-PR reattribution.

**Fold**: for every PR left with genuine findings after triage, invoke
`/helmsman-skills:fold-review <n>` directly, passing that PR's (possibly amended)
`REVIEW-PR-<n>.md` as the local-file input. `fold-review` owns spec, spec
review, implement, diff review, gate, push, and thread resolution end to
end - do not reimplement any of that here. A PR whose `REVIEW-PR-<n>.md` is
empty after triage needs no `fold-review` call.

**Stack dependents**: after a `fold-review` run changes a PR that a stacked
child depends on, rebase the child - `git fetch` then plain `git rebase
<base-branch>` in the child's own worktree (`fold-review` only adds commits,
never rewrites history, so no `--onto` needed; that form stays for the
amended-parent gotcha below, a different scenario) - then re-run the
child's own Phase 3 gates once. If a `fold-review` call itself fails or
stops (gate failure, push rejection), that failure is `fold-review`'s own
and already reported per its own contract - surface it here rather than
swallowing it, and do not rebase dependents of a PR whose fold didn't land.

This reviews the CODE (Phase 2 reviewed the design) — it catches real bugs
(path-jail bypasses, silent-success-on-error, lock races) the spec review
can't.

## Phase 5 — Validate
Run the project's local end-to-end validation, if it has one, from a
worktree carrying **all PRs' code** (the chain tip for stacked units, or a
throwaway integration branch merging the independent ones) — e.g. start the
stack locally, flip the relevant flags, drive the feature through the UI or
API, and verify the resulting DB/log state. On top of it, pipeline-specific
requirements:
- **No-regression proof**: confirm new default-off features stay dormant — check
  the service logs for the feature's "disabled/skipped" signal and zero import
  errors.
- **Deferred-live**: anything needing credentials/infra the user opted to defer
  cannot be validated — say so explicitly; never represent a green run as having
  validated the deferred path.
- Fix any issue found in the proper PR and rebase; clean up test data + restore
  any local state you flipped.

## Repo-specific execution gotchas

Every repo has its own toolchain quirks (lint rule selections, dependency-sync
scope, package-registration mechanics, feature-flag storage, upload sandboxing,
rebase conflict shapes). Discover them before relying on any specific command:
check the repo's own `CLAUDE.md`/`AGENTS.md`/`README`, CI config, and recent
commits for the actual gate commands and known-tricky spots, rather than
assuming another project's conventions carry over. Record what you learn
(scoped to that repo) so the next run in the same repo doesn't rediscover it
from scratch.

A few conventions that generalize across repos regardless of toolchain:
- **Stacked-rebase after amending a parent**: a plain `git rebase parent`
  replays the parent's OLD commit and conflicts on everything. Use
  `git rebase --onto <new-parent> <old-parent-sha> <branch>` to replay only the
  child's own commits.
- **`--force-with-lease`** (never bare `--force`) to update your own draft
  branch after amend/rebase.
- **Pre-existing lint/type findings are not yours to fix** — verify via
  `git show origin/main:<file>` before dismissing a finding as pre-existing,
  never assume.
- **Lint/typecheck scope can exclude a package the repo otherwise ships** —
  e.g. a monorepo's root `pyright` config excluding one application package
  while a library package under the same tree stays in scope. A clean gate
  run is not evidence the excluded package is clean — check the repo's
  actual lint/typecheck config for scope exclusions before trusting a "gate
  passed" claim; never assume uniform coverage across a monorepo.
- **No ticket IDs or ticket names in implemented code** — no issue-tracker IDs
  or issue titles in source comments, identifiers, log strings, or test
  names (commit messages and PR descriptions are fine — they're metadata,
  not code). Tickets get renamed, closed, and renumbered; code that cites
  one rots the moment it does. Explain the WHY directly in the comment
  instead of pointing at a ticket for it.

## Notes
- Pipe long command output to files; don't parse inline.
- Save durable project state (the PR set + any stack order, deferred items,
  decisive constraints) to memory at the end so a future session resumes cleanly.
