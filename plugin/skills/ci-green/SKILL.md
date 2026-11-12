---
name: ci-green
description: Use when PRs have failing, pending, or flaky CI checks and the goal is getting them green - "CI is failing", "make them green", "monitor this run", or gauging whether a test failure is a flake vs a regression
argument-hint: "[pr-numbers-or-run-url...]"
allowed-tools: Bash, Read, Grep
---

# ci-green

Watch CI on one or more PRs (or a specific workflow run), classify failures,
rerun only what is genuinely failed, and loop until everything settles. Reports
a diagnosis when a failure is real instead of blindly rerunning.

## Usage

```
/ci-green                       # PRs of the current branch / recently pushed branches
/ci-green 10794 10822           # specific PRs
/ci-green <actions run URL>     # one run, monitor + tally mode
```

## Instructions

1. **Resolve targets.** For a PR link, work in the repo it belongs to. For a
   bare PR number, resolve owner/repo from the current checkout's
   `git remote get-url origin` - never assume a default repo. With no
   arguments, find the PR for the current branch (`gh pr view --json number`).

2. **Snapshot check state** per PR:
   ```
   gh pr view <n> --json statusCheckRollup,headRefOid
   ```
   Bucket every check: `SUCCESS` / `PENDING` / `FAILURE` / `CANCELLED`.
   Map each non-green check to its run and job: parse the run ID from the
   check's `detailsUrl` (`/actions/runs/<run-id>/job/<job-id>`), or match via
   `gh run list --branch <head> --json databaseId,name`. Every command below
   needs these IDs.

3. **Classify every non-green check before acting.**
   - **Spot reclamation, not a failure:** conclusion `cancelled`, and the
     job's failed-step log is empty - i.e.
     `gh run view <run-id> --job <job-id> --log-failed` prints nothing (no
     `##[error]` lines, no test output). Happens routinely to jobs on
     preemptible/spot runners. Treat as rerun-eligible noise, never as a regression.
   - **Known flake shape:** an e2e/browser spec that failed on an unrelated
     diff, or a test that passed on the previous run of the same SHA. Rerun
     first, diagnose only if it repeats.
   - **Real failure:** deterministic job (lint, format, typecheck, unit) or a
     failure that reproduces across reruns. Do NOT rerun these more than once
     - pull the log and diagnose.

4. **Rerun failed jobs only:**
   ```
   gh run rerun <run-id> --failed
   ```
   Never rerun a whole suite, never push an empty commit, and never use
   update-branch to force a full re-trigger. Rebasing/pushing is out of scope
   for this skill - report instead.

5. **Poll in the background.** Run the poll loop as a background Bash task
   with all output piped to a file in the scratchpad; check the file, don't
   re-run ad-hoc status commands in the foreground. Poll every 60-120s.

6. **Flake tally mode** (single run URL, or a spec that failed twice): rerun
   the failed subset up to 5 times, and after each run pull the job log and
   grep the per-test markers and final summary line:
   ```
   gh run view <run-id> --log-failed | grep -E "✓|✘|passed|failed"
   ```
   Maintain a running tally per test - seeded with the ORIGINAL failing run,
   so "failed once, passed on rerun" reads 1 fail / 1 pass - and conclude
   flake (intermittent) vs regression (consistent).

7. **Stop conditions.**
   - All checks green on all target PRs → report green, per-PR.
   - Same job fails twice after rerun → stop rerunning; read the failed-step
     log, identify the failing test/step, and report a diagnosis with the log
     excerpt and a proposed fix. Do not fix code unless the user asks - when
     a check needs a code change, "green" is honestly unmet for that check:
     report it red with the fix, don't paper over it.
   - A check is stuck `PENDING` > 30 min → check whether the run is queued
     behind runner capacity vs genuinely hung; report which.

8. **Report** a table per PR: check name, verdict (green / rerun-fixed /
   flake with tally / real failure + diagnosis), and the run links.

## Common mistakes

- Treating a spot-cancelled job (preemptible/spot runner) as a real failure and debugging
  it. Check for the empty failed-log first.
- Rerunning the full suite "to be safe" - only ever `--failed`.
- Rerunning a deterministic failure (format, lint, migration numbering) hoping
  it passes. It won't; diagnose it.
- Polling in the foreground with sleep chains - use one background loop piped
  to a file.
- Declaring green from a stale snapshot - always re-check the rollup for the
  current head SHA after the last rerun completes.
