# Project conventions

These skills follow a handful of workflow conventions. Each value below is a
default: before applying one, read the reviewed project's agent docs
(AGENTS.md, CLAUDE.md, and any file they point to) and use the project's own
rule wherever it states one.

- **Project root.** Default: the parent directory holding the repo's local
  checkout (one directory per repo), not the repo's own root. `REVIEW-PR-<n>.md`,
  `FOLD-PR-<n>-spec.md` and feature specs are written there, never inside a
  repo.
- **Worktrees.** Default: `<repo>-worktrees/<name>` beside the checkout
  (`review-<n>`, `fold-<n>`, `<branch>`); never work in the main checkout.
- **PR descriptions.** Default: at most 10 lines, professional, no emoji, no
  co-author trailers; state clearly what is deferred.
- **Reply voice.** Default: `pr-comments`' voice contract.
- **Comment density.** Default: Phase 0's `d_comment_density` cap of 20%
  comment lines over added code; a project's own cap reaches Phase 0 via
  `phase0-launch.md` step 2.
