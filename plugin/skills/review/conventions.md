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
- **Review excludes.** Default: none. When the agent docs name paths,
  directories or sibling repos reviews must ignore, the review context pack
  lists them under `Excludes` and every layer drops findings there.
- **PR count.** Default: no cap. A cap in the agent docs, or feature-pipeline's
  `--max-prs <n>`, merges units along logical seams instead of exceeding it.
- **Reply voice.** Default: `pr-comments`' voice contract.
- **Comment density.** Default: Phase 0's `d_comment_density` cap of 20%
  comment lines over added code; a project's own cap reaches Phase 0 via
  `phase0-launch.md` step 2.
- **Tests in CI only.** Default: Phase 0 runs the diff's tests locally. A
  project whose agent docs say tests run in CI only skips them via
  `phase0-launch.md` step 2.
- **Format and lint before every push.** Run what the repo's CI runs (its
  workflow, pre-commit or pyproject config), e.g. `ruff format` +
  `ruff check` on the changed files, scoped the way CI scopes them. Seconds,
  not minutes, and never skipped. CI lints whole touched files, so a
  violation already in a file you touched fails the push too.
