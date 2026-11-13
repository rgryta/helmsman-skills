---
name: pr-comments
description: Use for any write operation on GitHub PR review comments - "post/send/add a review comment", "update that comment", "mark resolved comments as resolved", "check which threads are folded in", or deleting own comments after a bad post
argument-hint: "<pr> <post|update|resolve|delete-own> [...]"
allowed-tools: Bash, Read, Grep
---

# pr-comments

All write operations on PR review comments: post, update, resolve, delete-own.
One shared rulebook, one place for the mechanics.

## Rules (apply to every operation)

- Resolve owner/repo from the PR URL or `gh pr view <n> --json url` - never
  assume a default repo.
- NEVER post test/probe comments to a live PR. Compute anchors offline, then
  post once with the real body.
- Comment bodies via `curl` with a JSON heredoc (or `gh api --input <file>`) -
  never `gh api -f/--field`, it mangles backticks and em-dashes.
- **NEVER dismiss a review.** Approval is one per-user state - dismissing any
  review drops that user's entire approval. No teammate, bot, or "it blocks
  the merge" argument authorizes it - only the user, doing it themselves.
  Resolving threads is safe; dismissing is not.
- Never submit a pending review, and never delete a comment that isn't yours,
  without explicit user instruction.

## Voice contract (comments authored as the user)

Max 25 words. No em-dash, no semicolon, no emoji. State the defect and a
concrete fix. Never blame a ticket or author - frame as a guard against a
described failure. Use a style-rewrite tool when the environment provides one. Review
verdicts are approve-with-comments or comment-only - never REQUEST_CHANGES.

## post

Anchor with `line` (the line number in the new file) + `side: "RIGHT"`; for
a range add `start_line` + `start_side: "RIGHT"`. `line` (and `start_line`)
must sit in the same hunk of this file's block - between its `diff --git`
line and the next one. Never use the legacy `position` field.

```bash
diff=$(mktemp)
gh pr diff <PR> > "$diff"
grep -n "^diff --git" "$diff" | grep "<file>"                # file's block
grep -n "^@@" "$diff"                                       # +start,count; use only hunks inside the file's block
gh pr view <PR> --json headRefOid --jq .headRefOid          # commit_id

payload=$(mktemp)
cat > "$payload" <<'EOF'
{"body": "<BODY>", "commit_id": "<HEAD_SHA>", "path": "<PATH>", "line": <LINE>, "side": "RIGHT"}
EOF
jq . "$payload" >/dev/null && gh api repos/<OWNER>/<REPO>/pulls/<PR>/comments --input "$payload"
```

This single-comment POST publishes immediately - it does not join a pending
review. Return the `html_url`.

**Off-diff branch:** if the target line is in no hunk, never force an inline
anchor - the finding goes in a review body or a general PR comment instead.

**Several findings at once:** use the batched review POST
(`.../pulls/<PR>/reviews`, `--input <file>`: `{commit_id, event, body,
comments: [{path, line, side: "RIGHT", body}]}`, ranges add `start_line` +
`start_side`). On-diff findings go in `comments[]`, off-diff in `body`, and
`event: COMMENT` unless the user explicitly asked for a verdict. If you
already have a pending review (`gh api .../pulls/<PR>/reviews`,
`state == PENDING`, your login), stop and tell the user - never submit it.

Validate every payload with `jq .` before posting. If a post is interrupted
or its result unclear, list your own recent comments
(`gh api repos/<OWNER>/<REPO>/pulls/<PR>/comments --paginate`, filter by
your login) first. If the comment is there, use it and do not retry.
Retry only if it is missing. Delete only when two copies really exist.

## update

```bash
curl -s -X PATCH -H "Authorization: Bearer $(gh auth token)" \
  "https://api.github.com/repos/<OWNER>/<REPO>/pulls/comments/<COMMENT_ID>" \
  -d @- <<'EOF'
{"body": "<NEW BODY>"}
EOF
```

## resolve

Reconcile open threads against the code actually pushed - resolve exactly the
verified ones.

1. Fetch unresolved threads (REST doesn't expose resolution):
   ```
   gh api graphql -f query='query { repository(owner:"<o>", name:"<r>") {
     pullRequest(number: N) { reviewThreads(first: 100) { nodes {
       id isResolved isOutdated path line
       comments(first: 10) { nodes { databaseId author { login } body } } }
       pageInfo { endCursor hasNextPage } } } } }'
   ```
   Keep `isResolved: false`; paginate with `after: <endCursor>` while
   `hasNextPage`. `databaseId` is the REST `<COMMENT_ID>` for
   update/delete-own.
2. Verify each against HEAD, not intentions: `gh pr checkout <n>` (worktree)
   or `gh pr diff` + file reads, find the fix commit. "We pushed a fix round"
   is not evidence for any individual thread. `isOutdated: true` alone is NOT
   addressed - the line moved, the concern may remain. Discussion-answered
   counts only if the answer is sound against the code; wrong or unsure →
   NOT addressed.
3. Resolve verified only:
   ```
   gh api graphql -f query='mutation { resolveReviewThread(input:{threadId:"<id>"}) { thread { isResolved } } }'
   ```
4. Survivors → numbered fix list (file, line, concern, proposed fix) for the
   user. Never resolve them, never reply unprompted, never drop silently.
5. Report: thread (author, path:line) → resolved-verified-at-<sha>, or
   left-open with reason (outdated-not-fixed / rationale-wrong /
   not-addressed).

## delete-own

Only with explicit user instruction, or when recovering YOUR OWN bad post:
```bash
gh api repos/<OWNER>/<REPO>/pulls/comments/<COMMENT_ID> --method DELETE
```
A submitted review can't be deleted - only its comments. Never fix a bad post
by dismissing.

## Common mistakes

- Probing with a test comment to find the anchor - compute offline.
- Dismissing a review to unblock a merge - it silently revokes approval.
- Resolving from the changelog instead of verifying each thread at HEAD.
- Treating `isOutdated` as addressed.
- `--field` for bodies with backticks.
- Replying "Done" everywhere when only resolution was asked for.
