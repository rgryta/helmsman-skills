# --skip-review

Read only when `--skip-review` was passed. What it skips differs by skill -
`review` has no spec stage, `fold-review` does.

## In `review`

Skip the `hunter` and `edge` layers entirely - no dispatch, no placeholder
findings file, drop them from any `await_layers.py` layer list. Only
`project` and `landmine` run.

State the skip in the `REVIEW-PR-<n>.md` Overview and in the `Layers run`
line - a review missing its two adversarial LLM layers is reduced coverage,
not a clean pass, and the reader needs that at a glance. This is not a
PARTIAL round: it is a deliberate scope reduction the caller asked for, so it
does not by itself block the verdict. Record it with `ledger.py round-end
--complete --skipped hunter,edge` when both layers that ran are COMPLETE.
`delta` then answers `FULL_REHUNT` for the next round, because a delta since
this SHA would leave `hunter`/`edge` never having seen the code this round
covered. Their coverage only comes back on a round run without
`--skip-review`.

## In `fold-review`

Skip step 3 (spec review) entirely - go straight to step 4 with the spec as
written.

Skip step 5's full-tier dispatch (`hunter.md`, `edge.md` and `landmine-review`) unless a
touched path matches step 5's sensitive-path list - a fold pushes with no
approval checkpoint, and an unreviewed push costs most on those paths, so
they keep the full tier even under this flag. Otherwise
only the lightweight tier's claim-vs-diff self-check runs (it always runs,
full tier or not, since it's a self-check against the spec, not adversarial
review).

Note both skips in step 8's report, and name the sensitive path when it kept
the full tier. Step 6's gates run unconditionally either
way - this flag trades the adversarial safety net for speed, not the gate.
