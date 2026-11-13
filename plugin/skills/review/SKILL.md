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

Project root and worktree locations follow `<skill-dir>/conventions.md`
(defaults, overridable by the reviewed project's agent docs).

## Flags

- `--inline` - read `<skill-dir>/inline-mode.md` before step 5 and follow it;
  it changes how every layer in step 5 (and the wait in step 6) runs.
- `--skip-review` - read `<skill-dir>/skip-review.md` before step 5 and
  follow it (its "In `review`" section); it drops two of the four layers.
  Only load these files when the matching flag was actually passed.

## Steps

1. **Preflight + fetch + worktree.** `gh auth status` first - if it fails,
   stop and tell the user. Resolve owner/repo from the PR URL or the
   checkout's `git remote get-url origin` (ask if ambiguous); pass
   `-R <owner>/<repo>` on every gh call. `gh pr view <n> --json
   title,body,files,commits,headRefName,baseRefName,state,headRefOid`;
   capture the head SHA and `git fetch origin <baseRefName>` (a stack base
   may not be local yet). From inside the repo's local checkout: `git worktree add
   <repo>-worktrees/review-<n> <headRefOid>`; if the add fails (force-push
   race), re-run `gh pr view` for a fresh headRefOid and retry once. For a
   stack, batch all fetches/worktree adds in one Bash call sized as
   `timeout: max(120000, 45000 * n_prs)` ms, capped at 600000 ms - never
   request over 600000 ms for anything. When the formula would exceed the
   cap (n_prs >= 14), split the work into multiple batched Bash calls of at
   most 600000 ms each rather than backgrounding it. This setup covers every
   PR in the stack before any dispatch - step 5 fires one combined wave for
   the whole stack.
   Then create this round's artifact directory OUTSIDE every repo:
   `<outdir>` = `~/.helmsman/review-ledger/<owner>__<repo>__pr<n>/run-<epoch>/`
   via `mkdir -p`, with `<epoch>` from `date +%s`. Every artifact except the
   final `REVIEW-PR-<n>.md` goes there, never into the worktree. A fresh
   directory per run keeps a previous round's findings file out of step 6's
   readiness check, which cannot tell one round's output from another's.
2. **Ledger + mode.** Every `ledger.py` call requires `--repo <owner/repo>
   --pr <n>` (argparse enforces them on every subcommand); later calls below
   omit them for brevity. `python3 <skill-dir>/scripts/ledger.py delta
   --repo <owner/repo> --pr <n> --sha <head>` -> `FULL_REHUNT` or
   `DELTA_SINCE <sha>`. Besides no rounds, last round not complete and last
   round skipped (`--skip-review`/`--inline`), `delta` also forces
   `FULL_REHUNT` once `FULL_REHUNT_EVERY` (default 5, override with
   `--full-every N`) consecutive delta rounds have run since the last full
   one. Delta only counts if the old SHA is still an ancestor: `git
   merge-base --is-ancestor <sha> <head>` in the worktree - on failure
   (rebase/force-push) treat as FULL_REHUNT. Delta mode: layers hunt the
   diff since that SHA plus re-verify every ledger finding not `fixed`; a
   MISSED-EARLIER claim on unchanged code still requires hunk/blob-level
   verification, never "file unchanged" alone. Full mode (also forced when
   external API shapes changed): layers see the whole PR diff. Before
   dispatch, record the mode: `ledger.py round-start --repo <owner/repo>
   --pr <n> --sha <head> --mode full` after FULL_REHUNT (including a
   periodic one), or `--mode delta` after DELTA_SINCE.
3. **Phase 0 (deterministic, local, no CI dependency).** Follow
   `<skill-dir>/phase0-launch.md` (commit first, test runner, managed
   background launch of `scripts/run_phase0.py`, bounded collect, fallback
   FACTS file). Do step 4 while it runs. SKIPPED sections are stated
   confidence ceilings, never silently ignored. An `ERROR:` section is not a
   ceiling: that check crashed, so its class went unhunted. Name the crashed
   check in the review and treat the round like a PARTIAL one.
4. **Context pack.** Write `<outdir>/CONTEXT-<n>.md` ONCE: PR title/body
   digest, commit list, changed-file map, head SHA, `ledger.py report`
   output, and the FACTS file location. Layer prompts put static skill text
   FIRST and PR-specific text LAST, and reference files by path - never
   paste the diff or a per-layer PR narrative into prompts.
5. **Dispatch layers in ONE message, in the background.** Under
   `--skip-review`, follow `skip-review.md` - only `project` and `landmine`
   run. Under `--inline`, follow `inline-mode.md` for how every layer runs.
   For a single PR that is the four layers below (two under
   `--skip-review`). For a stack, run steps 2-4 for every PR first, then
   combine every PR's layers into that same single message - `n_prs * 4`
   parallel `Agent` calls (`n_prs * 2` under `--skip-review`, none under
   `--inline`), never one wave per PR. Each prompt resolves `<worktree>`,
   `<outdir>` and `<n>` to that PR's own paths and number, so
   `REVIEW-<n>-<layer>.md` files never collide. The `Agent` calls return at
   once, so treat every layer as backgrounded whatever `run_in_background`
   says: keep each call's task id, never named, never collected via
   SendMessage relays or idle notifications - an idle notification is not a
   result. The findings files on disk are the only result, and step 6's
   `await_layers.py` is the only collector.
   **Model and effort per layer**: `hunter` and `edge` follow
   `<skill-dir>/model-choice.md`; `project` and `landmine` get no override.
   - Layer `project`: first check
     `~/.helmsman/review-ledger/<owner>__<repo>__skill-path.txt` (one
     absolute path). If it exists and the path it names still exists, use
     it and skip to the fan-out check below. Otherwise discover the repo's
     own review skill (`<repo>/.claude/skills/` then `<repo>/.agents/skills/`,
     names like `*-pr-review`). Zero matches: skip the layer and say so in
     the review. Multiple: pick the one whose frontmatter description names
     PR review for this project, record the choice, and write its path to
     the cache file. Before dispatching, run `python3
     <skill-dir>/scripts/fanout_check.py <path-to-that-SKILL.md>`. A
     `FANOUT:` verdict means do NOT wrap it in an Agent - run its steps
     directly in the orchestrator, or dispatch its sub-passes as separate
     layers, and say so in the review. `FLAT:` means dispatch it with its
     SKILL.md as instructions; tell it Phase 0 already ran
     ruff/pyright/pytest - judgment only, no tool re-runs.
   - Layer `landmine`: run `fanout_check.py` on the `landmine-review`
     skill's SKILL.md the same way, then dispatch its contents as
     instructions on a `FLAT:` verdict. Every layer whose instructions come
     from a skill file outside this one gets that check, every time.
   - Layer `hunter`: dispatch with `<skill-dir>/hunter.md` as instructions.
     It carries the blind and acceptance briefs.
   - Layer `edge`: dispatch with `<skill-dir>/edge.md` as instructions on the
     same balanced model at medium effort. It owns the boundary sweep and
     reads `m_bounds`.
   NEVER wrap a skill that itself spawns subagents in another Agent - a
   nested fan-out's completion signal does not route back.
   Every dispatch prompt MUST include, with `<worktree>`, `<outdir>` and
   `<n>` resolved to their actual absolute paths and PR number (subagents
   do not inherit the orchestrator's cwd): "Do NOT post anything to GitHub.
   Read <outdir>/CONTEXT-<n>.md and <outdir>/FACTS-<n>.md first. Write your
   findings to <outdir>/REVIEW-<n>-<layer>.md as your final action; its
   last non-blank line must be exactly COMPLETE, or PARTIAL: <reason> -
   only PARTIAL carries a reason."
6. **Bounded wait, then collect from disk.** Under `--inline`, per
   `inline-mode.md`, skip the wait and go straight to `classify-report`
   below. Otherwise: run `python3 <skill-dir>/scripts/await_layers.py
   <outdir> <n> project,landmine,hunter,edge 540 5` (drop `hunter,edge`
   under `--skip-review`) as a FOREGROUND Bash call with `timeout: 600000`.
   It returns once every layer has written a findings file ending in
   COMPLETE or PARTIAL (read leniently: `COMPLETE: ...`, `**COMPLETE**`,
   `COMPLETED` all count), otherwise at the 540s deadline with a
   `STALLED: <layers>` line.
   A `NO_OUTDIR:` line means `<outdir>` does not exist or is not a
   directory - a resolution mistake in step 1, never a slow layer: fix the
   path and re-run the wait; retrying layers cannot help.
   Waiting happens ONLY inside this script - never poll by hand between
   waits, after a retry, or anywhere else, and never schedule a wakeup.
   `ListAgents` is not a wait loop. For a stack, one call per PR.
   A `STALLED:` line is NOT grounds to retry yet - a slow layer is not a
   dead layer. Re-run for the stalled layers alone with a grace deadline -
   `await_layers.py <outdir> <n> <stalled-layers> 240 5` - before anything
   else (540 + 240, because no Bash call may exceed 600000 ms). Only a layer
   still stalled after its grace window counts as dead.
   Before retrying a dead layer, STOP its original agent first (its task id
   came back from the dispatch; `ListAgents` matches it by description). Two
   live agents writing one `REVIEW-<n>-<layer>.md` race, and the surviving
   file is whichever finished last, not whichever is better.
   For each layer file run `ledger.py classify-report --file
   <outdir>/REVIEW-<n>-<layer>.md`. Missing file or MALFORMED: under
   `--inline`, rewrite it yourself once per `inline-mode.md`. Otherwise
   dispatch ONE fresh Agent for that layer (stop the original first) whose
   sole instruction is to write the missing findings file (same layer
   instructions, same output path, same terminator), then bound that wait
   with `await_layers.py <outdir> <n> <layer> 540 5`; if that also yields no
   valid file, record the layer as PARTIAL and move on.
   Record: `ledger.py round-end --repo <owner/repo> --pr <n> --sha <head>
   --complete` only when every layer that ran classifies as COMPLETE (all
   four, or `project` and `landmine` under `--skip-review`, which also passes
   `--skipped hunter,edge`; `--inline` adds `inline` to that list), else
   `--partial "<layers + reasons>"`. Never post an APPROVE-equivalent verdict
   on a knowingly partial round without saying so in the review body; a
   PARTIAL round forces FULL_REHUNT next time.
7. **Dedup, verify, generalize.** Merge findings (same line flagged twice:
   keep the higher-context version, note both provenances). Verify every
   claim against the code at the PR head; discard false positives silently;
   scope verification greps to the changed-file list. Grade every surviving
   finding against **Severity** below before writing. For EVERY confirmed
   blocker: one targeted sweep for the same bug class across the PR surface
   before writing the review.
   Run `python3 <skill-dir>/scripts/verify_citations.py <worktree>
   <outdir>/REVIEW-<n>-*.md` before writing anything. The first argument
   stays the WORKTREE: aiming it at `<outdir>` makes every citation report
   BAD while the script still exits 0, so the check passes as vacuous. A
   `BAD` line means the cited file or line does not exist at this head;
   every finding resting on it is unverified until you re-locate it in the
   real file or drop it. A wrong citation alone is not grounds to discard
   the finding. A `WARNING` that a layer cites no file:line is expected
   when that layer reports no findings.
   The `l_test_shape` FACTS section lists changed tests that can pass
   without the behaviour they name; with `h_mutation`'s surviving mutants it
   covers both halves of a weak test, the production side and the test side.
   The `k_claims` FACTS section lists prose referents the code it describes
   never references. Check the PR's own stated claims against the Phase 0
   gate sections: a body claiming lint, typecheck or tests are clean is
   contradicted by a failing `a_ruff`, `b_pyright` or `c_pytest`.
   The `j_siblings` FACTS section lists places still carrying the shape the
   diff changed; a finding that fixes one site while its listed siblings go
   unmentioned is incomplete rather than wrong.
   The `m_bounds` FACTS section lists parameters this diff clamps at one end
   only; each row's open end is caller-supplied, so a row nobody accounted
   for is an input shape nobody reviewed.
   The `i_neighborhood` FACTS section is the surface most misses hide in.
   Before calling a gate or guard correct, account for what it lists: every
   `before`/`after` sibling call at the changed call site (work ahead of a
   guard is ungated, work behind it still ran), every other binding of the
   names the changed lines read, and every caller from outside the file.
8. **Write** REVIEW-PR-<n>.md at project root (format below) and mirror
   each surviving finding into the ledger (`ledger.py upsert ... --sha
   <head>`; re-confirmed old findings keep their id; fixed ones
   `set-status --status fixed`).
9. **Cleanup, backgrounded, one call per worktree.** Each `git worktree
   remove --force` goes in its own backgrounded Bash call so one hang never
   blocks the others; never wait on it. Delete this round's `<outdir>` in
   the same call - the ledger already holds everything durable.

## --post mode

After step 8: only the orchestrator posts; subagents never touch GitHub
write APIs. Run the comment-density/anchor pre-checks BEFORE generating the
payload. Compute anchors against the PR head commit; inline comments only on
RIGHT-side diff lines. Apply `## Verdict` below. The posted body carries
every Blocker and Should-fix the file holds, each with its severity word - an
inline comment is not a substitute, because the reader weighing the verdict
reads the body. Nitpicks may stay inline. The reply voice cap
(`conventions.md`) covers replies only; the review body and its inline
findings carry each finding in full. Post ONCE per pr-comments skill
mechanics (`--input <file>`). Batch any multi-PR thread lookups into a single
`gh api graphql` call with aliases - never a sequential per-PR loop. Verify
anchors landed; on an ambiguous POST, list existing reviews before any
retry.

## Severity

BMAD's triage scale, so a review of ours and a raw BMAD one of the same PR
stay comparable: severity is the consequence for the artifact's main
consumer. Rate from the call site, never from the hunk - open the frames
around the finding, including ones outside the diff, before rating it.

**Severity** is what that consumer does differently because the value is
wrong. "Nothing leaks" settles one question and no others: a governance error
downgraded to a retryable one still gets retried, a silent deny still reads
as an outage. A finding with no consumer sentence is not graded yet.

| heading | BMAD | means |
|---|---|---|
| Blockers | high | intolerable for the consumer |
| Should-fix | medium | tolerable, still real |
| Nitpicks | low | cosmetic, or nothing downstream can observe it |

The `landmine` layer tags in six words; read them onto the three tiers as a
starting grade, then re-rate like any other layer's: `blocker` -> Blockers,
`should-fix` -> Should-fix, `landmine` -> Blockers or Should-fix by its blast
radius, `bug-bait` -> Nitpicks (correct today, so no consumer sees it yet)
unless the guard it leans on is already defeated, `worth confirming` -> Open
questions or Decisions made, `nitpick` -> Nitpicks.

**Disposition** is `this diff` when the change caused the defect or states
the guarantee it breaks, and `pre-existing` when it neither caused nor
claimed it. It decides whether a finding withholds approval. Age is a
disposition, never a severity: a contract this diff asserts, defeated by a
caller older than the diff, is still `this diff`.

Layer severities are guesses made without the collation context - re-rate
every finding here and inherit none. Where you rate one below the layer's own
grade, the finding carries why in one sentence.

## Verdict

Read off the file's own counts rather than off the narrative, and apply it to
the recommendation line in REVIEW-PR-<n>.md exactly as to a posted verdict.

- any Blocker -> COMMENT
- any Should-fix whose disposition is `this diff` -> COMMENT
- a `pre-existing` Should-fix may still APPROVE, with the finding and its
  disposition in the body
- a round recorded PARTIAL, a FACTS file carrying an `ERROR:` section, or a
  gate section (`a_ruff`, `b_pyright`, `c_pytest`) that is neither OK nor
  FINDINGS -> COMMENT, because each leaves the Blocker count unknown rather
  than zero. Exception: the `c_pytest` infra-only skip in
  `<skill-dir>/test-gate-exceptions.md` does not force COMMENT by itself.
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
