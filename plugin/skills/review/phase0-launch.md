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
   fraction (e.g. `0.1`). If they say tests run in CI only, set
   `HELMSMAN_PHASE0_SKIP=c_pytest,h_mutation` and let CI checks carry the
   test verdict (`test-gate-exceptions.md`). Pytest and mutation
   take one machine-wide lock, so a busy machine skips them rather than
   running several suites at once.
3. **Launch.** Run `python3 <skill-dir>/scripts/run_phase0.py
   <worktree> <outdir>/FACTS-<n>.md --base origin/<baseRefName>` (the PR's
   `baseRefName`) in a managed background Bash tool call and retain its task
   or session handle. The tool call owns the process: never append `&`,
   `nohup` or `disown`. In Claude use a background Bash task; in Codex let
   the exec call yield a session ID without detaching the command inside the
   shell. `<skill-dir>` is the `review` skill's base dir.
4. **Collect.** Run `python3 <skill-dir>/scripts/await_phase0.py
   <outdir>/FACTS-<n>.md` as a FOREGROUND Bash call with `timeout: 600000`
   (default deadline 590s, poll 5s). Every writer renames the facts file
   into place, so `READY` means complete. `MISSING` means `<outdir>` does
   not exist - fix the path, never re-wait on it. Do not poll only for the
   facts file: on `TIMEOUT`, check the retained tool call - still running,
   run `await_phase0.py` once more; ended, go to step 5. Still running
   after that second wait: stop the tool call and treat it as a Phase 0
   failure (step 5). Never write
   `sleep`, `pgrep` or `ps` loops, and never wait any other way.
5. **Fallback.** A non-zero/terminated tool call, or a completed call with no
   facts file, is a Phase 0 failure: preserve its captured exit/signal and
   stderr in a one-line `<outdir>/FACTS-<n>.md` fallback if `run_phase0.py`
   could not write it. Layers must see an explicit confidence ceiling, never
   a missing file.
