# --inline

Read only when `--inline` was passed. Applies to every layer dispatch in this
skill (and, via `../review/inline-mode.md`, to `fold-review`'s spec-review and
diff-review dispatches too).

Never call `Agent`. For each layer that would otherwise be dispatched, work
through that layer's own instructions yourself, in the main agent loop, one
layer at a time - there is no parallelism without subagents. "That layer's own
instructions" means whatever the dispatch would have handed it: `hunter.md`,
`edge.md`, the discovered project-review `SKILL.md`, `landmine-review`, or (in
`fold-review`) the spec-review/diff-review prompt text.

This applies whatever `fanout_check.py` says. A layer's own sub-passes,
including any its instructions would fan out to subagents, also run directly
and serially - never dispatch them as separate `Agent` layers, even though a
`FANOUT:` verdict otherwise allows it.

Still write the same output file the dispatch would have (e.g.
`<outdir>/REVIEW-<n>-<layer>.md`), with the same COMPLETE/PARTIAL terminator
convention. That keeps every downstream step (classify-report, dedup, verify,
citations) unchanged - they read files off disk and don't know or care whether
an `Agent` or the orchestrator itself wrote them.

Because each layer finishes synchronously before you move to the next one,
skip any `await_layers.py` wait for layers run this way - go straight to
whatever the skill does next with that file. Where the skill would dispatch a
fresh `Agent` for a missing or MALFORMED findings file, rewrite that file
yourself once instead, and record the layer PARTIAL only if it is still
missing or MALFORMED after that.

Inline layers are not independent: by the time they run you have already read
the PR narrative and every earlier layer's findings, so `hunter.md`'s blind
reader is not blind and later layers anchor on earlier ones, and they run on
the session model rather than the balanced tier a dispatch would use. State
that in the review's Overview and `Layers run` line (in `fold-review`, in step
8's report) as a confidence ceiling. It is not a PARTIAL round, but in
`review` record it with `ledger.py round-end ... --skipped inline` (plus any
layers `--skip-review` dropped) so `delta` sends the next round back to
`FULL_REHUNT` rather than narrowing independent layers to the diff since a
round they never saw independently.
