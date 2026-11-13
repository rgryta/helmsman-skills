---
name: prune-branches
description: Audit local git branches, show status table, prune merged/stale branches, and optionally rebase stale-but-active branches onto the remote's current default branch
argument-hint: "[repo-path]"
allowed-tools: Bash, Read, Edit, Write
---

# prune-branches

Audit local git branches against their remote and PR status. Clean up merged/stale branches, and optionally rebase stale-but-active branches onto the remote's current default branch (`git symbolic-ref --short refs/remotes/origin/HEAD`, below `<default>`) in dedicated worktrees.

## Usage

```
/prune-branches
/prune-branches /path/to/repo
```

If no path is provided, use the current working directory. If the working directory is not a git repo, check for subdirectories that are.

## Instructions

When this skill is invoked:

1. **`cd` to the repo root** (argument or cwd). Verify it is a git repository.

2. **Collect branch data.** For every local branch except `main`/`master`:
   - List branches with `git branch --no-color` and strip ALL prefix characters: `*` (current branch), `+` (worktree branch), and leading spaces. Use `sed 's/^[* +]*//'` — the `+` prefix is critical to strip or branch names won't match when querying PRs.
   - Local HEAD SHA
   - Whether `origin/<branch>` exists
   - Whether local and remote are synced, diverged, or local-only
   - PR number, state (OPEN/MERGED/CLOSED), and title via `gh pr list --head <branch> --state all --json number,state,title --limit 1`

3. **Check worktrees.** Run `git worktree list`. If any worktree points to a branch that has been merged via PR, flag it for removal.

4. **Present a table** to the user with columns: Branch, Pushed, Synced, PR, PR State.

5. **Categorize branches:**
   - **Safe to delete:** PR is MERGED and local is synced with remote (or branch is identical to another merged branch)
   - **Worktree cleanup:** worktree exists but branch is merged
   - **Needs attention:** diverged from remote, or PR is CLOSED (not merged)
   - **Active:** PR is OPEN
   - **Orphan:** no remote, no PR — ask user before deleting
   - **Stale-but-active:** OPEN PR but `git merge-base <default> <branch>` returns nothing (history-disjoint, e.g. after a force-rewrite of main) — flag for the rebase workflow below

6. **Ask the user** what to prune. Suggest the safe-to-delete set. Do not delete anything without confirmation.

7. **On confirmation, execute cleanup:**
   - Remove worktrees first: `git worktree remove <path>`. If it fails with "modified or untracked files" or "Directory not empty", **stop and inspect** with `git -C <path> status --short` and `ls <path>`. Report uncommitted/untracked state to the user before forcing — never blanket `--force` without confirmation.
   - For worktrees that are no longer git-tracked but still have leftover directories: use `rm -rf <path>` after verifying with `git -C <path> status` returns "fatal: not a git repository".
   - Then delete branches: `git branch -d <branch>` for merged, `git branch -D <branch>` only for branches the user explicitly approved that aren't merged to HEAD but are confirmed merged via PR
   - If a `-d` fails because git doesn't see the merge (squash-merged PRs), verify the branch tip matches the remote tracking branch or the merged PR, then use `-D`

8. **Show final state** — remaining local branches as a clean table.

## Rebasing stale-but-active branches

When the user asks to rebase the remaining (typically OPEN-PR) branches onto
current `<default>`, hand off to a rebase skill if one is installed —
it owns dependency ordering, history-disjoint cherry-picking, and conflict
triage (additive / migration-numbering / rename-delete / structural-stop).
Otherwise rebase each branch yourself in that order, stopping on any
structural conflict. Two notes specific to this audit context:

- If local main diverged drastically from origin (force-rewrite), confirm with
  the user before `git reset --hard <default>` — it discards local commits.
- Prefer rebasing into dedicated worktrees on `<branch>-rebased` names so the
  originals stay untouched until reviewed; don't push (pushing is a per-PR
  judgment call for the user).

## Notes

- Never delete `main` or `master`.
- Never delete remote branches. Only delete local branches and local worktrees.
- Never delete branches with OPEN PRs unless the user explicitly asks.
- For worktree branches with no remote/PR, check if the commits exist on another branch that was merged before suggesting deletion.
- Worktrees with uncommitted state: always warn before forcing removal, and tell the user exactly what they'll lose.
- Bash quirks on macOS: `/bin/bash` is 3.2 (no associative arrays); install a newer bash (e.g. via your package manager) if you need `declare -A` or `mapfile`. The Bash tool resets `cwd` between calls, so chain `cd` and the operation in the same call, or use a script file.
- When cherry-picking from a multi-line list of SHAs, use a bash array `SHAS=(sha1 sha2 ...)` and iterate `for sha in "${SHAS[@]}"`. Quoting `"$shas"` from a multi-line capture passes everything as a single arg.
