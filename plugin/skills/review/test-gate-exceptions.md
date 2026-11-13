# Test gate exceptions

Shared by `review`'s `## Verdict` and `fold-review`'s step 6 Gates.

`c_pytest` SKIPPED where every non-OK package's reason is `test runner infra
unavailable` (no `FAIL`, no other skip reason mixed in) is not, by itself, a
gate without a verdict, provided the repo carries a CI config that runs its
tests (e.g. `.github/workflows/`). That is the local test runner
(podman/docker) being unreachable, a local-infra problem CI does not share,
not a signal about the diff. With no CI config nothing else ever runs those
tests, so the skip keeps counting as a gate without a verdict.

When the exception applies, say so plainly in the report (review: Overview
and Phase 0 status line; fold: step 8) instead of silently upgrading the
result.

## CI is the judge of a local test run that gave no verdict

A `c_pytest` or `h_mutation` section that timed out or is SKIPPED for any
reason (budget, heavy-check lock, load-induced health check,
`HELMSMAN_PHASE0_SKIP`) does not force COMMENT in `review` and does not
block `fold-review` while the PR head's CI test checks are green or still
pending: `gh pr checks <n> --json name,state,bucket`, at least one test-running
check in bucket `pass` or `pending` and none in `fail` or `cancel`; checks in
`skipping` (path-filtered jobs for other packages) do not count either way.
No CI test check in `pass` or `pending` keeps the gate. A local test `FAIL` always counts. Never
re-run tests by hand to "confirm" a timeout; CI is that confirmation. Say in
the report that CI carried the test verdict.
