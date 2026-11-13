---
name: fold-review
description: Use when PR review feedback needs to be folded back into fixes - "fold this review in", "address the PR feedback", "/helmsman-skills:fold-review <n>", or an existing review file's findings need to be implemented and pushed
argument-hint: "<pr-number-or-link-or-file-path>... [--inline] [--skip-review] [--post-replies]"
allowed-tools: Bash, Read, Grep, Glob, Task, Write, Edit, Skill, Agent
---

# fold-review

Fold PR review feedback back into fixes on the same PR: read the review
content, write a fix spec, get the spec adversarially reviewed, implement,
push, review the diff, gate, and resolve the addressed threads. Companion to
the `review` skill (which produces `REVIEW-PR-<n>.md`) - `review` finds
problems, `fold-review` closes them out.

## Usage

```
/helmsman-skills:fold-review 12671
/helmsman-skills:fold-review https://github.com/<owner>/<repo>/pull/12671
/helmsman-skills:fold-review /path/to/REVIEW-PR-12671.md
/helmsman-skills:fold-review 12671 --inline --skip-review
/helmsman-skills:fold-review 12671 --post-replies
```

## Flags

- `--inline` - read `../review/inline-mode.md` before step 3, and again
  before step 5's full-tier dispatch, and follow it; it changes how those
  dispatches run.
- `--skip-review` - read `../review/skip-review.md` before step 3 and follow
  it (its "In `fold-review`" section); it skips step 3 entirely and skips
  step 5's full tier unless a sensitive path is touched. Only load either file when the matching flag was
  actually passed.
- `--post-replies` - the user's approval of step 7's thread replies and
  resolutions for this run: post them without asking.

**Stack mode.** When the input is several PRs, or a PR whose base is another
open PR, read `stack-mode.md` (this skill's base dir) before step 1 and follow
it: it folds only the named PRs bottom-up, rebases and verifies the whole
chain, pushes with a lease, gates once at the chain tip, and caps every
dispatch wave.

**New-PR mode.** When the PR is already merged or closed (`gh pr view <n>
--json state`), or the user asks for the fixes in a new PR, read
`new-pr-mode.md` (this skill's base dir) before step 2 and follow it: the
fold goes onto a new branch and a new draft PR instead of the original.

## Scope

Accepts one or more inputs, each a local file path or a PR number/link;
several inputs are a stack, one input per PR. Ticket-ID input is out of
scope - no reliable ticket-to-PR mapping exists to query generically. So
are fork PRs (head branch in a different remote than `origin`): there is no
remote to push to. Runs
fully autonomously end to end: spec, spec review, implement, push, diff
review, gate, resolve threads - no approval checkpoint before push, this is a
fold-in onto a PR the user explicitly asked to close out (or, in new-PR mode,
its follow-up draft). The one exception is step 7's reply comments on findings this
diff did not fix: a comment posted as the user needs their explicit approval
of the body, which "close out this PR" does not grant, but `--post-replies` does.

## Steps

### 1. Resolve input

**Live GitHub review state is always fetched, for every PR in scope.** This
holds no matter how the target was specified - a single PR number, a link, a
local file path, or a multi-PR ask like "all four" - and no matter whether a
local `REVIEW-PR-<n>.md` already exists or already reads as fully folded in.
A local file can be stale the moment a human (or another bot) posts a new
review after it was written; its presence, or its "Fixed:" annotations, are
never grounds to skip the GraphQL query below. What the fetched state
contributes depends on the input: for a PR number/link it is the review
content; for a file path the file is the review content and the live state
only adds to it (see that branch). When given several PRs at once, resolve
each one through this step independently - do not batch-scan local files
first and treat that as the resolution.

**Project root** and worktree locations follow `../review/conventions.md`
(defaults, overridable by the project's agent docs); `review` writes
`REVIEW-PR-<n>.md` at the project root.

- **File path that exists** - read it as the review content, but still
  resolve `<n>`, owner/repo, and the PR's head branch before continuing -
  step 2 needs `<n>` and the branch to build the worktree, step 7
  needs owner/repo for thread resolution, step 8 needs the PR URL. Never
  guess a repo: derive `<n>` from the filename if it's shaped like
  `REVIEW-PR-<n>.md`; otherwise derive `<n>` and the PR from the containing
  checkout's current branch (`gh pr view --json number,url,headRefName,baseRefName`); if
  that also fails, ask the user. Then run the live GraphQL query below
  anyway. The file wins: a caller such as a stack triage may have
  reattributed, merged, or dropped findings in it on purpose, so never
  re-add a live thread or review body that predates the file's
  modification time (a thread's time is its first comment's `createdAt`, a
  body's is its `submittedAt`, an issue comment's is its `updated_at`). Live
  state contributes two things only:
  thread IDs for findings the file already holds (step 7 resolves or
  replies on them), and threads, review bodies or issue comments posted
  after the file was written, which are new feedback and get added, kept by
  the same `isResolved` and `folded-reviews.txt` filters as the PR-number
  branch below. List every unresolved thread, unfolded review body or issue
  comment this rule
  dropped for predating the file, so step 8 can report it. Only then
  continue to step 2.
- **PR number/link** - resolve owner/repo the same way `review` step 1 does:
  from a full PR URL directly, or for a bare number from the current
  checkout's `git remote get-url origin` (or ask the user if ambiguous).
  The local checkout is the repo's directory under the project root
  (defined above).
  - Primary source is the PR's own reviews on GitHub - this is the live,
    canonical record of open feedback, whether it's a human reviewer,
    another bot, or `review --post` output. Feedback arrives three ways:
    inline comment threads, a top-level review body (e.g. a
    `CHANGES_REQUESTED` review submitted with just a summary, no inline
    comments, or an `APPROVED` one with notes), and PR issue comments -
    fetch all three, since any can exist without the others:
    ```
    gh api graphql -f query='query { repository(owner:"<o>", name:"<r>") {
      pullRequest(number: N) {
        reviewThreads(first: 100) { nodes {
          id isResolved isOutdated path line originalLine
          comments(first: 100) { nodes { body author { login } createdAt }
            pageInfo { endCursor hasNextPage } } }
          pageInfo { endCursor hasNextPage } }
        reviews(first: 100, states: [CHANGES_REQUESTED, COMMENTED, APPROVED]) { nodes {
          id state body author { login } submittedAt }
          pageInfo { endCursor hasNextPage } } } } }'
    ```
    Paginate `reviewThreads` and `reviews` with `after: <endCursor>` while
    their `hasNextPage`, paging each connection in its own follow-up query
    that drops the other (re-running both would re-fetch the other's page
    1 and double-count it), and any thread whose `comments.pageInfo.hasNextPage`
    is true with `node(id: "<thread id>") { ... on PullRequestReviewThread {
    comments(first: 100, after: "<endCursor>") { ... } } }` - a truncated
    page drops feedback silently. Then the issue comments:
    `gh api repos/<o>/<r>/issues/<n>/comments --paginate`. Review bots often
    post ONE comment and edit it as they re-review, so read each comment as
    of its latest `updated_at` and take its findings of every severity, not
    only the top ones.
    An outdated thread has `line: null`: anchor it by `originalLine` and
    re-locate the code at the head before folding.
    Keep only `isResolved: false` threads - already-resolved threads were
    presumably already addressed or deliberately dismissed; re-litigating
    them isn't this skill's job. From `reviews`, keep nodes with a non-empty
    `body` (a bare approval or state change with no body carries no
    feedback to fold) whose `id` is not listed in
    `~/.helmsman/review-ledger/<owner>__<repo>__pr<n>/folded-reviews.txt` -
    a review body cannot be resolved like a thread, so without that list
    every later fold would re-fold the same old summary. From issue
    comments, keep those that carry review findings about the code (skip
    status or report bots such as coverage, CI summaries and deploy
    previews), not written by the PR author, whose
    `<id> <updated_at>` line is not in that same file - an edited comment
    gets a new `updated_at` and is read again. If any of the three sets is
    non-empty, that combined content is the review content, go to step 2. Record each kept thread's `id` - step
    7 needs it to resolve the thread once the fix lands, or to reply on it
    when the fix lands elsewhere (top-level reviews have no thread either
    way, so nothing to record there; neither do issue comments).
  - Nothing in any set (empty PR, or review never posted to GitHub) -
    fall back to a local review file: look for `REVIEW-PR-<n>.md` at the
    project root first, then at
    the repo's own root for a manually-relocated file. Found - use it as
    the review content, go to step 2. Not found either - report that no
    review content exists for this PR and stop.

### 2. Spec the fixes

**Worktree first.** Run `python3 ../review/scripts/setup_round.py --repo <owner/repo> --pr <n>
--fold --tag <task-name>` from inside the repo's local checkout (same error
JSON and `--worktree` option as `review` step 1). It fetches `<headRefName>` and
`<baseRefName>`, adds or reuses the worktree `<repo>-worktrees/fold-<n>`
checked out **on the actual branch, not detached HEAD** (so step 4's plain
`git push` works), takes a lease on it, fast-forwards it to the PR head (so it
picks up anything that landed since the review ran; after a force-push it
resets a clean worktree whose local commits were all pushed to the new PR
head), creates this fold's
`<outdir>` = `~/.helmsman/review-ledger/<owner>__<repo>__pr<n>/fold-<epoch>/`,
prunes older `fold-*` directories, and prints ONE JSON object: take
`worktree`, `outdir` and `pre_fold_sha` as `<pre-fold-sha>` (the starting commit, which step 5
needs to scope its diff to exactly what this fold changes) from it. Exit 3
means another live session holds the worktree's lease - stop and report,
never share it. If the branch is already checked out in another worktree,
the add fails - run `git worktree list` and either rerun with `--worktree
<that path>` or `git worktree remove` it first. A dirty worktree fails setup:
inspect it. Local-only commits (ones the remote never had, e.g. after a
force-push rewrote the branch) also fail setup: never push over the remote
rewrite - stop and report to the user, who decides whether to cherry-pick them
onto the new head or drop them. Removing the worktree does not help, because
the commits live on the branch. Every artifact this fold produces
goes in `<outdir>`, never into the worktree - a checkout holds tracked code, and
untracked scratch `.md` dropped inside one is junk, and a layer told to write into the worktree
can lose its findings to a denied write and then read as a stalled layer.

Write `FOLD-PR-<n>-spec.md` at the project root (same location as
`REVIEW-PR-<n>.md`). If a previous one exists, read it first: a finding that
comes back after being rejected there keeps that rejection and its reason,
unless the review now gives a new argument.
For every finding in the review content: record the thread ID captured in
step 1 and its ledger id (`python3 ../review/scripts/ledger.py report --repo
<owner/repo> --pr <n>` lists open findings as `[open] <id> <severity>
<file>:<line> ... <title>`; match by file:line and title, none if no entry
matches), and give it exactly one of three dispositions - fixed in this PR (and
how), fixed in a different PR or commit (and which), or explicitly rejected
(and why). No silent drops, no blanket "addressed" without substance. Step 7
keys on that three-way split, because only the first case may resolve a
thread and the other two get a reply instead, and a finding with no thread ID
recorded gets neither. "Defer to a follow-up" is not a disposition: every
finding is fixed in this fold or rejected with a stated reason, and "fixed
in a different PR or commit" names a fix that already exists. Nits are
folded by default; "only a nit" is not a rejection reason. This spec file is a local scratch artifact: never
commit it, never push it onto the PR - which is exactly why the reasoning has
to reach the PR as a reply, since nothing else here does.

**Sibling-site sweep.** For each finding being fixed: one grep in the worktree across the touched
package/module for the same pattern - the literal code shape being changed,
not just the symbol name (a rename-only grep undercounts). Note candidate sibling sites directly in the spec next to the
finding; each becomes either an in-scope fix or an explicit rejection with a
reason, the same "no silent drops" rule this step already applies to review
findings.

### 3. Review the spec

**Trivial change** - the spec fixes exactly one finding, in one file, and
that file matches neither step 5's sensitive-path list nor CI config
(`.github/`, pipeline files): skip this step and report it as
`skipped: trivial change`.

Under `--skip-review`, follow `../review/skip-review.md` (already read per Flags
above) - this step is skipped entirely. Under `--inline`, follow
`../review/inline-mode.md` (already read per Flags above) for how this dispatch runs.

Dispatch `../review/hunter.md` and `../review/edge.md` (relative to this
skill's base dir) as two `Agent` calls in ONE message, on the model in
`../review/model-choice.md`, collected the way `review`'s step 5 and 6
collect layers: the calls return at once, so keep each task id, never
address an agent by name or collect it via SendMessage, and read results only
from disk. These are the same prompts `review`'s `hunter` layer (blind
reader + acceptance auditor) and `edge` layer (boundaries and input shapes)
use. Both are flat prompt files, not discoverable skills, so no fan-out check
is needed for them (unlike `landmine-review` in step 5).

There is no diff yet at this point - this review runs pre-implementation,
same as today. Adapt the dispatch prompt's "read CONTEXT/FACTS"
preamble to: "Do NOT post anything to GitHub. Do not spawn agents. Read `FOLD-PR-<n>-spec.md` and
the original review content first (their actual absolute paths - you do not
inherit the orchestrator's cwd). Apply your briefs to this spec text. Write
findings to `<outdir>/REVIEW-<n>-spec-<layer>.md`; last non-blank line
exactly COMPLETE or PARTIAL: <reason>." The `-spec` infix keeps
`REVIEW-<n>-<layer>.md` free for the diff review in step 5. Collect with
`python3 ../review/scripts/await_layers.py <outdir> <n>-spec hunter,edge
540 5` (foreground, timeout 600000); on `STALLED:`, one grace re-wait at 240s for
the stalled layer alone, and a layer still stalled after that is stopped
and reported in step 8 as a confidence ceiling, never retried. Step 4 cannot
start until this wait returns, so there is nothing else to overlap with.

Fold genuine findings back into the spec; no need to re-run the review after
fixing, just fix and move on.

### 4. Implement

Implement per the approved spec directly in step 2's worktree - no subagent
delegation at this scale. Commit locally.

**Prove every test the fold adds or changes.** Revert the code change that
test defends, keeping the test, confirm the test FAILS, then restore the
change. A test that still passes with its fix reverted defends nothing and
locks in no behaviour, which is the single most common shape reviewers send
back on a folded-in fix. Report the proof in step 8; if a test cannot be
made to fail, say so rather than counting it as coverage.

**Push before gating.** Run the format-and-lint pre-push step in
`../review/conventions.md` (fold any fix into the commit), then push at
once: `git push` (plain push suffices, since the worktree tracks the real
branch and these are new commits on top of the existing tip). Steps 5-6
gate the pushed head alongside CI; a gate failure becomes a follow-up fix
commit, never a held push. If the push is rejected as non-fast-forward (the
PR branch moved meanwhile), fetch the new tip, rebase the fold commits onto
it, and retry once. Never `--force` or `--force-with-lease` here - this is
someone else's open PR, not a scratch branch; a second rejection stops the
flow (report, leave the worktree for inspection). The one exception is a
stack fold's cascade push in `stack-mode.md`: `--force-with-lease` with an
expected SHA, only on the gh user's own branches, after every rebase verified.

### 5. Review the diff

Runs after Implement (step 4, local commits exist), before Gates (step 6).
Skipped entirely if step 4 produced zero new local commits since
`<pre-fold-sha>` - an all-rejected spec has no diff to review, and step 8's
report already covers rejected findings with reasons.

**Facts first, unconditionally, and only after step 4's commits exist.**
Launch `python3 ../review/scripts/run_phase0.py` (relative to this skill's
base dir) per `../review/phase0-launch.md` (commit first, test runner, managed background
launch, bounded collect, fallback FACTS file; a failure there is "Phase 0
unavailable"), with `HELMSMAN_PYTEST_SCOPE=gate
HELMSMAN_PHASE0_CHECK_S=270` added to the environment: this is a gate, so it
runs every test directory holding one of the diff's tests, with a budget a
real suite can finish inside. This single
execution also serves step 6 (Gates)
below - its `a_ruff`/`b_pyright`/`c_pytest` checks ARE the gates; nothing
runs lint/typecheck/test twice.

**Sizing (deterministic, no caller flag).** From the worktree: files touched
since `<pre-fold-sha>` (`git diff --name-only <pre-fold-sha>..HEAD`),
finding count from `FOLD-PR-<n>-spec.md`, and whether any touched path
matches the sensitive-path list (case-insensitive substring): `auth`,
`security`, `netpol`, `networkpolicy`, `rbac`, `migrations/`, `permission`,
`sandbox`, `exec`, `secret`, `credential`, `payment`, `billing` - a repo may
extend this list via its own `CLAUDE.md`/`AGENTS.md`, discovered the same
way step 6 discovers gate commands; never remove from the default, only
add.

Full tier if ANY of: more than one file touched, more than one finding
fixed, any touched path matches the sensitive-path list, or the spec
explicitly rejects a finding. Otherwise lightweight tier. Under
`--skip-review`, per `../review/skip-review.md`, the full tier runs only when a touched
path matches the sensitive-path list, whatever the other sizing triggers say.

- **Lightweight tier** (no subagent dispatch) - read `FACTS-<n>.md`,
  `FOLD-PR-<n>-spec.md`, and `git diff <pre-fold-sha>..HEAD` yourself, and
  check the spec's stated claims (what it says it fixed, and why any
  rejection is justified) against what the diff actually contains. This IS
  the claim-vs-diff check for this fold - fix any mismatch in place before
  continuing (same "fix and move on" rule as step 3). Always runs, even
  under `--skip-review` - it is a self-check, not adversarial review.
- **Full tier**, skipped under `--skip-review` unless a sensitive path is
  touched - write `<outdir>/CONTEXT-<n>.md` once (spec digest, finding list,
  touched-file map, head commit, FACTS location, the worktree path and the
  diff range `<pre-fold-sha>..HEAD`). Under `--inline`, follow
  `../review/inline-mode.md` (already read per Flags above) for how every layer's
  dispatches run. Otherwise, dispatch, in ONE message, as `review`'s step 5
  does (the calls return at once; keep each task id, never name an agent or
  collect it via SendMessage - the findings files are the only result;
  per-CLI dispatch and collection in `../review/cli-notes.md`):
  - `../review/hunter.md` and `../review/edge.md` as two `Agent` calls on
    the same balanced model at medium effort resolved in step 3.
  - `../landmine-review/SKILL.md`, after `python3
    ../review/scripts/fanout_check.py ../landmine-review/SKILL.md`. A
    `FANOUT:` verdict means run its steps directly instead of wrapping it
    in an `Agent`; on `FLAT:`, dispatch its contents as `Agent`
    instructions, no `model` override.
  Every prompt, with `<worktree>`, `<outdir>` and `<n>` resolved to actual
  absolute paths (dispatched subagents inherit no cwd): "Do NOT post
  anything to GitHub. Do not spawn agents. Read `<outdir>/CONTEXT-<n>.md` and
  `<outdir>/FACTS-<n>.md` first. Write findings to
  `<outdir>/REVIEW-<n>-<layer>.md`; last non-blank line exactly COMPLETE
  or PARTIAL: <reason>."
  Collect via `python3 ../review/scripts/await_layers.py <outdir> <n>
  hunter,edge,landmine 540 5` (foreground, timeout 600000). A `NO_OUTDIR:` line
  means the path never resolved - fix it and re-wait; retrying layers cannot
  help. On `STALLED:`, one
  grace re-wait for the stalled layer(s) alone at 240s before treating
  any as dead - a slow layer is not a dead layer. A layer still stalled
  after that is dead: stop it, never retry it.
  Run `python3 ../review/scripts/verify_citations.py <worktree>
  <outdir>/REVIEW-<n>-*.md` before trusting any file:line citation. The
  first argument stays the WORKTREE - it resolves the cited code paths, and
  an empty or wrong glob makes this check report zero citations and pass
  vacuously instead of failing.
  Verify every surviving claim against the actual diff; discard false
  positives silently. For every confirmed finding, one class-generalization
  sweep - start from the `j_siblings` section of `FACTS-<n>.md` (verbatim
  survivors of the changed shape only), then grep the touched package for
  the same pattern, same mechanism as step 2's sibling sweep, now run
  against the pushed-shape code.
  Fold every genuine finding into the worktree immediately - fix and move
  on, no re-review after fixing.
  Run `python3 ../review/scripts/ledger.py classify-report --repo <owner/repo>
  --pr <n> --file <outdir>/REVIEW-<n>-<layer>.md` on each collected file; a
  STUB (COMPLETE with no file:line citation, no severity-tagged finding line
  and no `Checked:` line naming a file, path or backticked symbol) counts as
  PARTIAL, after one rewrite under `--inline`; any findings a STUB file holds
  are still folded like the rest.
  A layer that comes back PARTIAL, STUB or dead-after-grace is reported as a
  confidence ceiling in step 8, never silently treated as clean.

### 6. Gates

`run_phase0.py`'s output (already computed in step 5) IS the gate:
`a_ruff`/`b_pyright`/`c_pytest` sections failing is a gate failure, and so is
any of the three coming back as anything other than OK or FINDINGS - a check
that crashed, timed out or skipped ran no gate at all, and no verdict is not
a pass. Exception: a test run CI carries
(`../review/test-gate-exceptions.md`) is not a gate failure; note it in
step 8 and continue. Otherwise say which check gave no verdict
and why rather than resolving threads on it. On failure, commit the fix as a
follow-up commit, push it (step 4's push rules), and re-run the full gate set
once, not a separate retry per failing check - still failing, stop, report
the failing gate output, leave the worktree in place for inspection, and
resolve no threads. `phase0.py`
already does repo-correct diff-scoped discovery (e.g. a root pyright config
that excludes one package while another stays in scope is respected
automatically, since the check runs the repo's own configured tool rather
than a hand-maintained exception list). If step 5 fell back to "Phase 0
unavailable" (the FACTS file holds only the `Phase 0 unavailable` line), discover the repo's own
gate commands manually, in the same places `review`'s step 5 Layer
`project` looks for a repo's own review skill: check the repo's own
`CLAUDE.md`/`AGENTS.md`/`README`, CI config, and Makefile/package-manager scripts for the actual lint/typecheck/
test commands - never assume another repo's toolchain. If none are
discoverable even then, report that explicitly and proceed - blocking on an
undiscoverable gate would stall this autonomous flow with no data to act on.

### 7. Push follow-ups, resolve, reply, ledger

Commit and push whatever steps 5-6 folded in, with the same format-and-lint
step and push rules as step 4.

Then split the spec's findings by what this PR's own diff actually contains,
because resolving a thread asserts that the diff addressed it:

- **Fixed in this diff** - resolve the thread, using the thread IDs captured
  in step 1:
  ```
  gh api graphql -f query='mutation { resolveReviewThread(input:{threadId:"<id>"}) { thread { isResolved } } }'
  ```
  A top-level review body or issue comment has no thread to resolve; its
  findings are closed out by the pushed fix and the step 8 report.
- **Fixed in a different PR or commit, or explicitly rejected** - never
  resolve it, reply on the thread instead (below). A fix that lives somewhere
  else is not in this diff, so resolving would tell the next reader the
  opposite of what happened.

After the push succeeds, append the `id` of every review body the spec gave
a disposition, whatever the disposition, to
`~/.helmsman/review-ledger/<owner>__<repo>__pr<n>/folded-reviews.txt`
(one per line, `mkdir -p` first), and `<id> <updated_at>` of every issue
comment step 1 kept - that file is what step 1 skips on the
next fold, and a body has no thread to carry a rejection, so a rejected one
left out would be re-folded and possibly implemented next time. It lives
beside `<outdir>`, not inside it, so cleanup keeps it.

**Thread IDs when step 1 fell back to a local file.** If the review content
came from a local `REVIEW-PR-<n>.md` that had also been posted to GitHub via
`review --post` (so real inline comments/threads exist for it), no IDs were
captured, so run the same `reviewThreads` GraphQL query as step 1's primary
path to get real ones - matching by file:line alone is too fragile, and per
`pr-comments`' `resolve` operation, `isOutdated: true` is not evidence a
thread was addressed. Verify each candidate thread against the code actually
pushed, using `pr-comments`' `resolve` verification bar, before resolving it
or replying on it.

A thread-resolve failure (e.g. permissions) does not block or unwind the push
- report it as a partial result, don't swallow it.

**Reply on every thread this diff did not fix.** Without it the reasoning
only exists in `FOLD-PR-<n>-spec.md`, which never leaves this machine, so the
PR is left carrying a bare unresolved thread with no trace that the finding
was considered at all. One reply per such thread, naming the case and the
reason:
- rejected: the rejection reasoning the spec already captured, so the
  reviewer can push back on it directly instead of re-deriving it
- fixed elsewhere: which PR or commit carries the fix, since this diff won't
  show it

Bodies follow the reply voice in `../review/conventions.md`; use a
style-rewrite tool when the environment provides one (one attempt with a time limit; on
failure or hang write the text yourself, never retry in a loop). A finding with no thread behind it - a top-level review body, an issue
comment, or a local review file that was never posted to GitHub - has nothing to reply to,
and its reasoning reaches the reader through step 8's report only.

**These replies are the one write in this flow that needs approval**, unless
`--post-replies` was passed: then post every body without showing it first. Write
each body to `<outdir>/reply-<thread-id>.md`, show the user every body next
to its thread (path:line and the finding it answers), and post only the ones
they explicitly approve. Post approved bodies one thread at a time with
`pr-comments`' `reply` operation. A body the user declines or never answers is not posted, and the thread
is still not resolved - step 8 reports it as left open with no reply. A reply
that fails to post is reported the same way a failed resolve is, not
swallowed and not retried as a resolve.

**PR description.** After the last push, compare the PR description (`gh pr
view <n> --json body,author`) with `git diff <pre-fold-sha>..HEAD` and the
spec, and pick out the lines the fold made wrong (changed behaviour, renamed
flags, a limitation it removed). If the author is the gh user (`gh api user
--jq .login`), rewrite only those lines and apply them with `gh pr edit <n>
--body-file <outdir>/pr-body.md`; otherwise list them in step 8.

**Ledger sync.** Write one `{"id": <finding-id>, "status": <s>}` object per
finding with a ledger id from step 2 into a JSON list at
`<outdir>/statuses.json`, then make one call (flags literal, never through
a shell variable - zsh does not word-split it): `python3
../review/scripts/ledger.py set-status --repo <owner/repo> --pr <n>
--from-json <outdir>/statuses.json`. Status: `fixed` when this diff fixed it and step 6's
gate passed, `wontfix` when the spec rejected it, `regressed` when a fix
does not hold. A finding whose fix lives in a different PR stays `open`
until that PR lands, because the ledger tracks whether a finding is closed
and not whether someone means to close it.

**Rule distillation.** For every CONFIRMED BLOCKER folded in, decide whether the bug class is
mechanically expressible. If yes, write a candidate rule to
`~/.helmsman/review-ledger/<owner>__<repo>__pr<n>/semgrep-candidates/<kebab-id>.yaml`
(`<outdir>`'s parent, which step 9's cleanup keeps), using the seed
rules in `../review/scripts/semgrep-rules/` as the shape. Never write
into that seed directory: it is the installed skill copy, which a
plugin update replaces. Validate the rule fires on the PRE-FIX code:
`probe="$(mktemp -d)/prefix-probe.<ext>"` with `<ext>` the extension of
the file under test (semgrep picks the language from it), `git show
<pre-fold-sha>:<path> > "$probe"`, then `semgrep --config <the-new-file>
"$probe"` (or `uvx semgrep`). A rule that cannot be made to fire on the
actual bug is not worth keeping - delete it. A rule that fires belongs
in a separate follow-up PR to the repository that ships these skills,
never in the fold itself.

### 8. Report

One-line summary, diff stat, gate results (noting CI carrying the test
verdict, if that carve-out applied), the non-vacuous test proof from
step 4 (which tests were reverted and confirmed failing, and any that could
not be), whether `--inline` and/or `--skip-review` were in effect and what
that skipped (step 3's spec review, step 5's full-tier dispatch, or the
sensitive path that kept it), the `--inline` confidence ceiling, which
diff-review tier ran
(lightweight/full/skipped) and any findings folded in from it (with the same
fixed/rejected shape as spec findings), any PARTIAL/dead diff-review layer,
sibling-sweep sites found in step 2 and class-sweep sites found in step 5,
which spec findings were fixed in this diff, fixed in a different PR or
commit (naming which), or explicitly rejected (with reasons), the PR URL, the
resolved-thread count (plus any that failed to resolve), and for every thread
this diff did not fix either the URL of the reply posted on it or why none
was (body declined, no thread behind that finding, or the post failed),
every unresolved thread or review body step 1 dropped for predating the
review file, and the PR description lines updated or listed as stale.

End with one line per step, each `ran` or `skipped: <reason>`: Phase 0,
spec review, implement, diff review, citations check, gate, push, PR
description, replies, thread resolution, ledger, cleanup.

### 9. Cleanup

Step 8's report is the final message, so compose it, run this cleanup, then
send the report with one `cleanup:` line (removed, or every leftover path).
Remove the worktree (`git worktree remove <repo>-worktrees/fold-<n>`,
no `--force`; only if that fails, `--force`) and delete `<outdir>`, unless a
step above says to leave the worktree for inspection. Some CLIs'
command policies block `--force` removal and `rm -rf`; when a removal fails,
run `setup_round.py --release <worktree> --tag <task-name>` and list every
leftover path on the `cleanup:` line - never skip it silently.
