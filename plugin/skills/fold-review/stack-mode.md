# Stack mode

Shared by `fold-review` (a stack fold) and `review` (dispatch waves and
per-PR mode). A stack is several PRs passed together, or one PR whose
`baseRefName` is another open PR's head branch; walk `baseRefName` down to
the PR that targets the default branch and up through every open PR based on
a member, and order the chain bottom-up. Only the PRs passed in are folded;
the rest of the walked chain is only rebased, verified and pushed.

## Dispatch waves

One message never carries more `Agent` calls than the active CLI's cap:
16 in Claude, 6 in Codex (another CLI: 6). A request over the cap is refused
whole, not trimmed. Waves hold whole PRs - `floor(cap / agents_per_pr)` PRs
each (4 layers per PR in `review` gives 4 PRs a wave in Claude, 1 in Codex) -
and the next wave goes out once the current one is collected. A PR's layers
never straddle two waves, so each PR keeps one `await_layers.py` collection.

## Phase 0 for a stack

Pytest, pyright and mutation share one machine-wide lock, so parallel Phase 0
runs do not go faster: they queue, and a run that waits past its budget skips
those checks. Launch a stack's Phase 0 runs one at a time, the next once the
previous returns; a PR's wave can dispatch as soon as its own FACTS exists.
In a stack fold, per-PR runs add `HELMSMAN_PHASE0_SKIP=b_pyright,c_pytest,h_mutation`
(facts for the diff review only, no lock taken, so these may run together)
and the one gate below carries the heavy checks.

## Per-PR mode

When the user asks for one agent per PR, or the stack is too large to hold in
one context, each PR's work runs `--inline` in ONE unnamed background `Agent`
per PR: `review <n> --inline` (it writes `REVIEW-PR-<n>.md`), or `fold-review`
steps 1-5 for `<n>` under `--inline` that commit in the PR's own worktree and
stop before any push (the orchestrator owns the cascade below). Send them in
waves of at most the cap. Never name these agents and never address them by
SendMessage; collect each from its completion notification and the file it
wrote. The prompt includes "Do not spawn agents.", so a per-PR agent never
fans out under itself.

## Stack fold

0. Before the first fold, fetch and record for every PR in the walked chain
   its local tip as `<old-tip>` and its remote tip as `<expected-sha>`.
   `<old-tip>` never changes during the flow; `<expected-sha>` becomes the
   pushed SHA after each successful push of that branch. If the bottom PR's
   `git log --format=%H origin/<base>..<branch>` contains the `headRefOid` of
   a merged PR (`gh pr list --state merged --search <sha> --json headRefOid`),
   its parent was squash-merged: rebase it with
   `git rebase --onto origin/<base> <that-sha>` and verify it as in step 3.
1. Fold bottom-up in one orchestrator: steps 1-5 for each named PR, with its
   own input (its `REVIEW-PR-<n>.md`, or the PR), itself or in per-PR mode,
   committing in that PR's worktree but not pushing.
2. After a PR's fold commit (or rebase, for a PR that is not folded), rebase
   each direct dependent onto it from the dependent's worktree. First run
   `git merge-base --is-ancestor <old-parent-tip> HEAD` (the parent's
   recorded `<old-tip>`). If it fails, the parent was rewritten (amended)
   after the dependent branched: use as `<old-parent-tip>` the first of the
   parent's recorded `<expected-sha>`, then each entry of
   `git reflog --format=%H <parent>` and `git reflog --format=%H origin/<parent>`,
   that passes `--is-ancestor`; none passes - stop as in step 3. Then
   `git rebase --onto <new-parent-tip> <old-parent-tip>`. The dependent's own
   new tip becomes the next parent tip, so the cascade continues up the chain.
3. Verify every rebase before any push:
   - no rebase in progress (`git status` names none; no
     `.git/rebase-merge` or `.git/rebase-apply` in its git dir);
   - the branch tip changed;
   - `git diff --name-only <parent>...<branch>` is non-empty and lists only
     the files of `git diff --name-only <old-parent-tip>...<old-tip>` plus
     files its own fold touched.
   A rebase that stops on conflicts, or a check that fails, stops the whole
   cascade: `git rebase --abort` there, push nothing, and report the PR and
   the conflicting files. Never push a half-rebased chain - pushing a branch
   whose rebase stopped can push the base's tip, and GitHub closes a PR whose
   head no longer differs from its base.
4. Push bottom-up with `git push --force-with-lease=<branch>:<expected-sha>`,
   only on a branch whose PR author is the gh user (`gh pr view <n> --json
   author` against `gh api user --jq .login`); never bare `--force`. The
   push stops at the first PR the user does not own: nothing above it is
   pushed, since a child pushed over an unpushed parent shows the parent's
   changes in its diff. Report those PRs as "folded locally, not pushed".
   This is the one exception to step 4's no-force rule.
5. One gate for the whole chain: step 5's Phase 0 gate run (with its
   `HELMSMAN_PYTEST_SCOPE=gate`) on the chain tip's worktree with
   `--base origin/<bottom PR's baseRefName>`, under the same CI-aware rules
   (`../review/test-gate-exceptions.md`). A gate failure is fixed in the PR
   that introduced the failing code and cascades up again from there.
6. Step 7's resolve/reply and follow-up pushes run only for PRs whose fold
   reached the remote; step 8's report runs per PR after the gate.
