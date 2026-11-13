# New-PR mode

Used by `fold-review` when PR `<n>` is merged or closed, or the user asks for
the fixes in a new PR. Steps 1-3 run against `<n>`; the ledger and
`<outdir>` stay keyed by `<n>`. Only these change:

- **Step 2's worktree.** Do not run `setup_round.py` (it needs `<n>`'s head
  branch). Create `<outdir>` yourself:
  `outdir=~/.helmsman/review-ledger/<owner>__<repo>__pr<n>/fold-$(date +%s); mkdir -p "$outdir"; echo "$outdir"`,
  and use that printed path from here on.
  Base the fold on `<base>` = `<n>`'s `baseRefName`, or the
  default branch (`gh repo view --json defaultBranchRef`) when
  `git ls-remote --exit-code --heads origin <base>` finds it gone. Create the
  worktree on a new branch: `git fetch origin <base>`, then
  `git worktree add -b fold-<n> <repo>-worktrees/fold-<n> origin/<base>`
  (the project's own branch prefix, if its agent docs name one, goes in
  front). `<pre-fold-sha>` is that base tip. A PR closed without merging
  never reached `<base>`, so start from its head instead:
  `git fetch origin pull/<n>/head`, then
  `git worktree add -b fold-<n> <repo>-worktrees/fold-<n> FETCH_HEAD`, and
  `<pre-fold-sha>` is that head. An existing `fold-<n>` worktree or branch
  makes the add fail: stop and report it, never reuse it.
- **Step 4.** Push with
  `git push -u origin <branch>`, then open a draft:
  `gh pr create --draft --base <base> --title "<n's title> (review fixes)"
  --body-file <outdir>/new-pr-body.md`, the body linking `#<n>` and listing
  the findings it fixes. Every later step works on this new PR `#<new>`.
- **Step 7.** The threads live on `<n>`, whose diff holds none of these fixes,
  so nothing is resolved there: every thread the fold fixed gets the reply
  "Fixed in #<new>.", under the same approval rule as the other replies.
  The PR-description check is skipped, since `#<new>`'s body was just
  written. Ledger: leave these findings `open` until `#<new>` merges.
- **Step 8** names `#<new>`'s URL next to `<n>`.
