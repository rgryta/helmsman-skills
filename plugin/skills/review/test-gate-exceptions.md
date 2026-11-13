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
