# --quick

Read only when `--quick` was passed. A lower-confidence review: one agent per
PR, no Phase 0, no separate layers.

- Steps 1, 2 and 4 run as usual. Skip step 3: write `<outdir>/FACTS-<n>.md`
  with the single line `SKIPPED: --quick`.
- Step 5: dispatch ONE agent per PR, layer name `quick`, whose instructions
  are `<skill-dir>/hunter.md`, `<skill-dir>/edge.md` and the
  `landmine-review` SKILL.md, read by path as one combined brief (no
  `project` layer), plus the shared dispatch sentence. `--quick` replaces
  the layer set; ignore `--skip-review` when both are passed.
- Step 6 waits on `quick` alone. Record `ledger.py round-end --complete
  --skipped project,landmine,hunter,edge` when it is COMPLETE, so the
  next round runs full.
- Steps 7 and 8 and `--post` are unchanged: every finding is verified,
  graded and posted the same way. State "Quick review: one agent, no Phase 0,
  lower confidence" in the Overview and in the posted review body. No
  Phase 0 means every gate is unknown: the verdict is COMMENT at best.
