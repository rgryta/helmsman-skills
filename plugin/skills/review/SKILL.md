---
name: review
description: Use when a GitHub PR (or a stack of PRs) needs a thorough review - "review this PR", "/helmsman-skills:review <n>", "run a fresh review round" - including when the result should be posted to the PR ("auto-review", "review and post", --post)
argument-hint: "[pr-numbers-or-links...] [--post] [--inline] [--skip-review]"
allowed-tools: Bash, Read, Grep, Glob, Task, Write, Skill, Agent
---

# review

Multi-layer PR review: deterministic Phase 0 + four parallel LLM layers +
verified collation. Default mode writes REVIEW-PR-<n>.md locally and NEVER
touches GitHub. `--post` additionally posts ONE batched review.

## Flags

- `--inline` - read `<skill-dir>/inline-mode.md` before step 5 and follow it;
  it changes how every layer in step 5 (and the wait in step 6) runs.
- `--skip-review` - read `<skill-dir>/skip-review.md` before step 5 and
  follow it (its "In `review`" section); it drops two of the four layers.
  Only load these files when the matching flag was actually passed - they
  are not needed otherwise.

## Steps

1. **Preflight + fetch + worktree.** `gh auth status` first - if it fails,
   stop and tell the user. Resolve owner/repo from the PR URL or the
   checkout's `git remote get-url origin` (ask if ambiguous); pass
   `-R <owner>/<repo>` on every gh call. `gh pr view <n> --json
   title,body,files,commits,headRefName,state,headRefOid`; capture the head
   SHA. From inside the repo's local checkout: `git worktree add
   <repo>-worktrees/review-<n> <headRefOid>`; if the add fails (force-push
   race), re-run `gh pr view` for a fresh headRefOid and retry once. For a
   stack, batch all fetches/worktree adds in one Bash call sized as
   `timeout: max(120000, 45000 * n_prs)` ms, capped at 600000 ms - never
   request over 600000 ms for anything. When the formula would exceed the
   cap (n_prs >= 14), split the fetch/worktree work into multiple batched
   Bash calls of at most 600000 ms each instead of raising the timeout or
   backgrounding blindly. This setup covers every PR in the stack before
   any dispatch happens - step 5 fires one combined wave for the whole
   stack afterward, never a wave per PR.
   Then create this round's artifact directory OUTSIDE every repo:
   `<outdir>` = `~/.helmsman/review-ledger/<owner>__<repo>__pr<n>/run-<epoch>/`
   via `mkdir -p`, with `<epoch>` from `date +%s`. Every artifact this skill
   produces except the final `REVIEW-PR-<n>.md` goes there and never into
   the worktree - a checkout holds tracked code, and untracked scratch
   `.md` dropped inside one is junk. A fresh directory per run is also what keeps a previous
   round's leftover findings file out of step 6's readiness check, which
   tests only for a terminator and cannot tell one round's output from
   another's.
2. **Ledger + mode.** Every `ledger.py` call in this skill requires
   `--repo <owner/repo> --pr <n>` (argparse enforces them on every
   subcommand, e.g. `classify-report --file X` alone exits 2); later calls
   shown below omit them for brevity but assume them. `python3
   <skill-dir>/scripts/ledger.py delta --repo <owner/repo> --pr <n> --sha
   <head>` -> `FULL_REHUNT` or `DELTA_SINCE <sha>`. Besides the no-rounds,
   last-round-not-complete and last-round-skipped (`--skip-review`/`--inline`)
   triggers, `delta` also forces `FULL_REHUNT`
   once `FULL_REHUNT_EVERY` (default 5, override per-call with
   `--full-every N`) consecutive delta-scoped rounds have run since the
   last full one - delta mode must not drift indefinitely on code it has
   never re-examined. Delta only counts if the old SHA is still an
   ancestor: `git merge-base --is-ancestor <sha> <head>` in the worktree -
   on failure (rebase/force-push) treat as FULL_REHUNT. Delta mode: layers
   hunt the diff since that SHA plus re-verify every ledger finding not
   `fixed`; a MISSED-EARLIER claim on unchanged code still requires
   hunk/blob-level verification, never "file unchanged" alone. Full mode
   (also forced when external API shapes changed): layers see the whole PR
   diff. Record the mode this round actually ran with: `ledger.py
   round-start --repo <owner/repo> --pr <n> --sha <head> --mode full` after
   a FULL_REHUNT verdict (including a periodic one), or `--mode delta`
   after DELTA_SINCE, before dispatch.
3. **Phase 0 (deterministic, local, no CI dependency).** COMMIT FIRST:
   Phase 0 repairs a worktree a check dirtied by running `git checkout --`
   on it, which is unrecoverable. It now spares files that were already
   modified when it started, but anything that becomes dirty WHILE it runs
   is reverted, so uncommitted work in the target worktree is not safe
   while Phase 0 is running. Launch `python3
   <skill-dir>/scripts/run_phase0.py <worktree> <outdir>/FACTS-<n>.md` in a
   managed background Bash tool call and retain its task or session handle.
   The tool call owns the process: never append `&`, `nohup` or `disown` to
   the shell command. In Claude use a background Bash task; in Codex let the
   exec call yield a session ID without detaching the command inside the shell.
   Before launching, look in the project's agent docs (AGENTS.md, CLAUDE.md,
   README, CONTRIBUTING) for a test runner the project uses in place of
   plain pytest, such as a parallel harness that takes pytest's arguments
   last. If there is one, set `HELMSMAN_PYTEST_RUNNER` to the command that
   goes where `pytest` would (it runs inside the project env, test paths
   appended); if there is none, leave it unset.
   Do step 4 (write the context pack) meanwhile, then collect that exact tool
   call to completion with a foreground wait bounded at 600000 ms. Do not poll
   only for the facts file because a dead process can never satisfy that wait.
   A non-zero/terminated tool call or a completed call with no facts file is a
   Phase 0 failure: preserve its captured exit/signal and stderr in the one-line
   `<outdir>/FACTS-<n>.md` fallback if `run_phase0.py` could not write it. Layers
   must see an explicit confidence ceiling, never a missing file. SKIPPED sections are
   stated confidence ceilings, never silently ignored. An `ERROR:` section is
   not a ceiling: that check crashed, so the class it covers went unhunted and
   nobody can say whether it was clean. Name the crashed check in the review
   and treat the round the way a PARTIAL one is treated.
4. **Context pack.** Write `<outdir>/CONTEXT-<n>.md` ONCE: PR title/body
   digest, commit list, changed-file map, head SHA, `ledger.py report`
   output, and the FACTS file location. Layer prompts put static skill text
   FIRST and PR-specific text LAST, and reference files by path - never
   paste the diff or a per-layer PR narrative into prompts.
5. **Dispatch layers in ONE message, foreground.** Under `--skip-review`,
   follow `skip-review.md` (already read per Flags above) - only `project`
   and `landmine` run this step. Under `--inline`, follow `inline-mode.md`
   (already read per Flags above) for how every layer in this step runs.
   For a single PR that is
   the four layers below (two under `--skip-review`). For a stack, run steps 2-4 for every PR first
   (each is per-PR-scoped by `<n>` and cheap/local), then combine every
   PR's layers into that same single message - `n_prs * 4` parallel
   foreground `Agent` calls (`n_prs * 2` under `--skip-review`, none under
   `--inline`), never one wave per PR. Each dispatch prompt
   still resolves `<worktree>`, `<outdir>` and `<n>` to that PR's own
   absolute paths and number, so `REVIEW-<n>-<layer>.md` files never
   collide across PRs in the stack. Parallel `Agent` calls, `run_in_background` unset, never named,
   never collected via SendMessage relays - an idle notification is not a
   result. If a layer must run backgrounded, collect it ONLY via its
   task-notification, and before ever declaring a layer dead, read its
   transcript/output tail - a slow layer is not a dead layer.
   **Model and effort per layer** (provisional pending measurement - the
   design spec's largest single dollar lever): dispatch `hunter` and `edge`
   on a balanced coding/reasoning model at medium effort. Resolve the actual
   override from the active CLI: for example, Claude Code `model: sonnet`;
   Codex `model: gpt-5.6-terra` with `reasoning_effort: medium`; or another
   CLI's current comparable balanced model, such as its Gemini Pro-tier
   model. These names are examples, so prefer the CLI's current stable alias
   rather than assuming one vendor's identifier exists everywhere. Do not
   substitute an economy tier such as Haiku, Luna, Flash, or Mini. If the CLI
   cannot set model or effort per subagent, inherit the session model and
   record that fallback. Dispatch `project` and `landmine` with no model or
   effort override so they keep inheriting the session's selected default
   until the balanced-tier pair has been compared against the prior hunter
   set on a handful of reviews.
   - Layer `project`: first check
     `~/.helmsman/review-ledger/<owner>__<repo>__skill-path.txt` (one
     line, an absolute path - a sibling of `ledger.py`'s own `--dir`
     default). If it exists and the path it names still exists on disk,
     use it directly and skip straight to the fan-out inspection step
     below - a cached path still gets checked for whether it spawns
     subagents before it is wrapped in an Agent. Otherwise (no cache file,
     or the cached path no longer exists) discover the repo's own review
     skill (`<repo>/.claude/skills/` then `<repo>/.agents/skills/`, names
     like `*-pr-review`). Zero matches: skip the layer and say so in the
     review. Multiple: pick the one whose frontmatter description names PR
     review for this project, record the choice, and write its path to the
     cache file for later reviews of this repo. Before dispatching, run
     `python3 <skill-dir>/scripts/fanout_check.py <path-to-that-SKILL.md>`.
     A `FANOUT:` verdict means do NOT wrap it in an Agent - run its steps
     directly in the orchestrator, or dispatch its individual sub-passes as
     separate layers, and say so in the review. `FLAT:` means dispatch it
     with its SKILL.md as instructions; tell it Phase 0 already ran
     ruff/pyright/pytest - judgment only, no tool re-runs.
   - Layer `landmine`: run `fanout_check.py` on the `landmine-review`
     skill's SKILL.md the same way, then dispatch its contents as
     instructions on a `FLAT:` verdict. Every layer whose instructions come
     from a skill file outside this one gets that check - a skill that is
     flat today can grow a fan-out in any upstream release, and the check
     costs milliseconds.
   - Layer `hunter`: dispatch with `<skill-dir>/hunter.md` as instructions
     on the balanced model at medium effort resolved above. It carries the
     blind and acceptance briefs; the edge brief is its own layer below, split
     out after two rounds where the findings a merged hunter missed were
     all edge-shaped and all inside one changed function.
   - Layer `edge`: dispatch with `<skill-dir>/edge.md` as instructions on the
     same balanced model at medium effort. It owns the boundary sweep and reads
     `m_bounds`,
     which is why the split costs one cheap agent rather than a third of
     the review: the deterministic sections carry the enumeration and the
     layer answers it. Splitting further is not free - collation, not the
     layer wait, is the measured bottleneck.
   NEVER wrap a skill that itself spawns subagents in another Agent - a
   nested fan-out's completion signal does not route back.
   Every dispatch prompt MUST include, with `<worktree>`, `<outdir>` and
   `<n>` resolved to their actual absolute paths and PR number (dispatched
   subagents do not inherit the orchestrator's cwd, so a bare filename
   fails, and an unresolved `<outdir>` writes the findings file where
   nothing looks for it): "Do NOT post anything to GitHub. Read
   <outdir>/CONTEXT-<n>.md and <outdir>/FACTS-<n>.md first. Write your
   findings to <outdir>/REVIEW-<n>-<layer>.md as your final action; its
   last non-blank line must be exactly COMPLETE, or PARTIAL: <reason> -
   only PARTIAL carries a reason."
6. **Bounded wait, then collect from disk.** Under `--inline`, per
   `inline-mode.md`, skip this step's wait entirely and go straight to
   `classify-report` below for each layer file. Otherwise: never sit in an
   open-ended wait for the layers: run `python3 <skill-dir>/scripts/await_layers.py
   <outdir> <n> project,landmine,hunter,edge 540 5` (drop `hunter,edge` from
   the layer list under `--skip-review`) as a FOREGROUND Bash call
   with `timeout: 600000`. It returns the moment every layer has written a
   findings file whose last non-blank line is COMPLETE or PARTIAL, and
   otherwise returns at the 540s deadline with a `STALLED: <layers>` line.
   That terminator is read leniently on purpose - a layer that decorated it
   (`COMPLETE: <what I covered>`, `**COMPLETE**`, `COMPLETED`) still counts
   as COMPLETE, because the alternative is a stall the orchestrator has to
   hand-correct over a line that said the layer finished.
   A `NO_OUTDIR:` line instead means `<outdir>` does not exist or is not a
   directory, so the script returns at once rather than burning the
   deadline. That is a resolution mistake in step 1, never a slow layer:
   fix the path and re-run the wait - retrying layers cannot help, and
   their findings files may already be sitting where you did not look.
   Waiting happens ONLY inside this script - never poll by hand between
   waits, after a retry, or anywhere else, and never schedule a wakeup.
   `ListAgents` is not a wait loop. For a stack, pass every PR's layers in
   one call per PR.
   A `STALLED:` line is NOT grounds to retry yet. A slow layer is not a
   dead layer, and a layer thirty seconds from writing its file costs a
   whole duplicate dispatch if retried. Re-run the same command for the
   stalled layers alone with a grace deadline - `await_layers.py <outdir>
   <n> <stalled-layers> 240 5` - before anything else. The deadlines are
   split into 540 + 240 rather than one long wait because no Bash call may
   exceed 600000 ms. Only a layer still stalled after its grace window
   counts as dead.
   Before retrying a dead layer, STOP its original agent first (the task id
   came back from that layer's `Agent` dispatch, and `ListAgents` matches it
   by the dispatch description). Two live agents writing one
   `REVIEW-<n>-<layer>.md` race, and the file that survives is whichever
   finished last rather than whichever is better.
   For each layer file run `ledger.py
   classify-report --file <outdir>/REVIEW-<n>-<layer>.md`. Missing file or
   MALFORMED: under `--inline`, do not dispatch anything - rewrite it
   yourself once per `inline-mode.md`. Otherwise dispatch ONE fresh
   foreground Agent for that layer whose sole
   instruction is to write the missing findings file (same layer
   instructions, same output path `<outdir>/REVIEW-<n>-<layer>.md`, last
   non-blank line exactly COMPLETE or PARTIAL: <reason>), then bound that wait the
   same way with `await_layers.py <outdir> <n> <layer> 540 5`; if that
   second attempt also yields no valid file, record the layer as PARTIAL and
   move on.
   Record: `ledger.py round-end --repo <owner/repo> --pr <n> --sha <head>
   --complete` only when every layer that ran classifies as COMPLETE (all
   four, or `project` and `landmine` under `--skip-review`, which also passes
   `--skipped hunter,edge`; `--inline` adds `inline` to that list), else
   `--partial "<layers + reasons>"`. Never post an APPROVE-equivalent verdict on a
   knowingly partial round without saying so in the review body; a PARTIAL
   round forces FULL_REHUNT next time.
7. **Dedup, verify, generalize.** Merge findings (same line flagged twice:
   keep the higher-context version, note both provenances). Verify every
   claim against the code at the PR head; discard false positives silently;
   scope verification greps to the changed-file list. Grade every surviving
   finding against **Severity** below before writing - the sweep, the
   verdict and what the post carries all read that grade. For EVERY
   confirmed blocker: one targeted sweep for the same bug class across the
   PR surface ("found the instance - hunt the class") before writing the
   review.
   Run `python3 <skill-dir>/scripts/verify_citations.py <worktree>
   <outdir>/REVIEW-<n>-*.md` before writing anything. The first argument
   stays the WORKTREE: it resolves the cited code paths, so aiming it at
   `<outdir>` makes every citation report BAD while the script still exits
   0 - the check passes as vacuous instead of failing loudly. A `BAD` line means
   the cited file or line does not exist at this head - a layer has cited a
   position it never read, and every finding resting on that citation is
   unverified until you re-locate it in the real file or drop it. Layers
   have reported real defects at fabricated line numbers, so a wrong
   citation is not by itself grounds to discard the finding; it is grounds
   to re-derive the location before it reaches the review.
   The `l_test_shape` FACTS section lists changed tests that can pass without
   the behaviour they name; combined with `h_mutation`'s surviving mutants it
   covers both halves of a weak test, the production side and the test side.
   The `k_claims` FACTS section lists prose referents the code it describes
   never references. Separately, check the PR's own stated claims against the
   Phase 0 gate sections already in FACTS: a body claiming lint, typecheck or
   tests are clean is contradicted outright by a failing `a_ruff`, `b_pyright`
   or `c_pytest`, and that comparison costs nothing because both halves are in
   the same file.
   The `j_siblings` FACTS section lists places still carrying the shape the
   diff changed; each is a candidate twin, and a finding that fixes one site
   while its listed siblings go unmentioned is incomplete rather than wrong.
   The `m_bounds` FACTS section lists parameters this diff clamps at one end
   only; each row's open end is caller-supplied, so a row nobody accounted
   for is an input shape nobody reviewed.
   The `i_neighborhood` FACTS section is the surface most misses hide in.
   Before calling a gate or guard correct, account for what its section
   lists: every `before`/`after` sibling call at the changed call site (work
   that runs ahead of a guard is ungated, work that runs behind it still
   ran), every other binding of the names the changed lines read (each is
   another shape that reaches the changed code), and every caller from
   outside the file (each is an entry point the diff does not show).
8. **Write** REVIEW-PR-<n>.md at project root (format below) and mirror
   each surviving finding into the ledger (`ledger.py upsert ... --sha
   <head>`; re-confirmed old findings keep their id; fixed ones
   `set-status --status fixed`).
9. **Cleanup, backgrounded, one call per worktree.** Each `git worktree
   remove --force` goes in its own backgrounded Bash call so one hang never
   blocks the others - removal has hung past 590s before; never wait on it.
   Delete this round's `<outdir>` in the same call - step 8 has already
   written the review and mirrored the findings, so the ledger holds
   everything durable and the run directory is spent.

## --post mode

After step 8: only the orchestrator posts; subagents never touch GitHub
write APIs. Run the comment-density/anchor pre-checks BEFORE generating the
payload. Compute anchors against the PR head commit; inline comments only on
RIGHT-side diff lines. Apply `## Verdict` below. The posted body carries
every Blocker and Should-fix the file holds, each with its severity word - an
inline comment is not a substitute, because the reader weighing the verdict
reads the body, and a finding that appears only on a diff line was demoted a
second time without anyone saying so. Nitpicks may stay inline. Post ONCE per pr-comments skill mechanics
(`--input <file>`). Batch any multi-PR thread lookups into a single
`gh api graphql` call with aliases - never a sequential per-PR loop. Verify
anchors landed; on an ambiguous POST, list existing reviews before any
retry.

## Severity

BMAD's triage scale (History names the skill), so a review of ours and a raw
BMAD one of the same PR stay comparable: severity is the consequence for the
artifact's main consumer. Rate from the call site, never from the
hunk - open the frames around the finding, including the ones outside the
diff, before rating it, because a guarantee is defeated wherever its consumer
reads it and not where it was written.

**Severity** is what that consumer does differently because the value is
wrong. "Nothing leaks" settles one question and no others: a governance error
downgraded to a retryable one still gets retried, a silent deny still reads
as an outage. A finding with no consumer sentence is not graded yet.

| heading | BMAD | means |
|---|---|---|
| Blockers | high | intolerable for the consumer |
| Should-fix | medium | tolerable, still real |
| Nitpicks | low | cosmetic, or nothing downstream can observe it |

**Disposition** is `this diff` when the change caused the defect or states
the guarantee it breaks, and `pre-existing` when it neither caused nor
claimed it. It decides whether a finding withholds approval and is not the
same question as severity. Age is a disposition, never a severity: a contract
this diff asserts, defeated by a caller older than the diff, is still `this
diff`, because the diff is what put the claim on the page.

Layer severities are guesses made without the collation context - re-rate
every finding here and inherit none. Where you rate one below the layer's own
grade, the finding carries why in one sentence; silent demotion is how a
graded defect becomes a line note nobody weighs.

## Verdict

Read off the file's own counts rather than off the narrative, and apply it to
the recommendation line in REVIEW-PR-<n>.md exactly as to a posted verdict:
the file is where a reader forms a view, so "approve pending these" over a
finding this diff caused is the demotion `## Severity` exists to stop.

- any Blocker -> COMMENT
- any Should-fix whose disposition is `this diff` -> COMMENT, since a defect
  the change caused or claimed is not something to approve past
- a `pre-existing` Should-fix may still APPROVE, with the finding and its
  disposition in the body, so no author is held to account for a defect their
  diff neither caused nor claimed
- a round recorded PARTIAL, a FACTS file carrying an `ERROR:` section, or a
  gate section (`a_ruff`, `b_pyright`, `c_pytest`) that is neither OK nor
  FINDINGS -> COMMENT, because a dead layer, a crashed check and a gate that
  ran out of budget all leave the Blocker count unknown rather than zero.
  Exception: `c_pytest` SKIPPED where every non-OK package's reason is `test
  runner infra unavailable` (no `FAIL`, no other skip reason mixed in) does
  not force COMMENT by itself, provided the repo carries a CI config that
  runs its tests (e.g. `.github/workflows/`) - that is the local test runner
  (podman/docker) being unreachable, a local-infra problem CI does not share,
  not a signal about the diff. With no CI config nothing else ever runs those
  tests, so the skip keeps forcing COMMENT. Say so plainly in the Overview and Phase 0 status line
  instead of silently upgrading the verdict.
- an open question whose answer would change a severity -> COMMENT until it
  is answered
- otherwise APPROVE; never REQUEST_CHANGES

## Output format (REVIEW-PR-<n>.md)

# PR #<n> Review Summary
**Title / Branch / Changes / Layers run / Mode (full|delta since <sha>) / Phase 0 status**
## Overview
## Blockers        ### 1. <title> `[provenance]` - [file:line](path#Lline), disposition, what breaks, who consumes it and does what, fix
## Should-fix     ### same shape; a demoted layer-blocker names why it sits here
## Nitpicks
## Decisions made (confirm each was intentional) `[landmine]`
## Open questions
## Verification log   | Claim | Verified against | Status |
## Summary            | Category | Count | + recommendation (per `## Verdict`) + layer attribution

## History

bmad-code-review previously ran here as a wrapped Layer 2; removed after
audit: the wrapper produced zero findings at 28% of the trio's cost, and
wrapping a fan-out skill in an Agent breaks completion routing. Run raw it
grades well, so `## Severity` adopts its triage scale (`steps/step-03-triage`:
low/medium/high by consequence for the main consumer, rated from the call
site, layer severities disregarded) rather than inventing a second one.

The edge brief ran inside the merged hunter until a review graded a
defeated contract as observability and missed six in-function findings a
separately-run set caught. It is one extra balanced-tier agent (originally
Sonnet), anchored by `m_bounds`, on the reasoning that deterministic sections
cost no model turns while an agent costs them on every read.

The design spec called for two separately-measured rollouts: ship Phase 0
plus the shared context pack and dispatch/timeout/ledger contract first,
measure five reviews, then cut the layer set from six agents to three and
A/B that separately. This skill shipped both changes together in one pass
instead, so a before/after measurement against the old baseline reflects
the combined effect of both changes, not either one in isolation.
