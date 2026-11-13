# Launching Phase 0

Shared by `review` step 3 and `fold-review` step 5. The calling skill adds
its own environment and says what to do while Phase 0 runs.

1. **Commit first.** Phase 0 repairs a worktree a check dirtied by running
   `git restore` on it, which is unrecoverable. It spares files already
   modified when it started, but anything that becomes dirty WHILE it runs
   is reverted, so uncommitted work in the target worktree is not safe.
2. **Pick the test runner.** Look in the project's agent docs (AGENTS.md,
   CLAUDE.md, README, CONTRIBUTING) for a test runner the project uses in
   place of plain pytest, such as a parallel harness that takes pytest's
   arguments last. If there is one, set `HELMSMAN_PYTEST_RUNNER` to the
   command that goes where `pytest` would (it runs inside the project env,
   test paths appended); otherwise leave it unset. If the agent docs state
   a comment-density cap, set `HELMSMAN_COMMENT_DENSITY_CAP` to it as a
   fraction (e.g. `0.1`).
3. **Launch.** Run `python3 <skill-dir>/scripts/run_phase0.py
   <worktree> <outdir>/FACTS-<n>.md --base origin/<baseRefName>` (the PR's
   `baseRefName`) in a managed background Bash tool call and retain its task
   or session handle. The tool call owns the process: never append `&`,
   `nohup` or `disown`. In Claude use a background Bash task; in Codex let
   the exec call yield a session ID without detaching the command inside the
   shell. `<skill-dir>` is the `review` skill's base dir.
4. **Collect.** Collect that exact tool call to completion with a foreground
   wait bounded at 600000 ms. Do not poll only for the facts file, because a
   dead process can never satisfy that wait.
5. **Fallback.** A non-zero/terminated tool call, or a completed call with no
   facts file, is a Phase 0 failure: preserve its captured exit/signal and
   stderr in a one-line `<outdir>/FACTS-<n>.md` fallback if `run_phase0.py`
   could not write it. Layers must see an explicit confidence ceiling, never
   a missing file.
