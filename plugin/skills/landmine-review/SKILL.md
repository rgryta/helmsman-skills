---
name: landmine-review
description: >-
  Generic PR review lens that hunts SILENT failures and unstated decisions under
  realistic conditions - the operationalised form of two review prompts
  ("find me the landmines in this PR" / "what decisions were made as part of
  this PR"). Domain-agnostic across DB writes, locks, migrations, identity,
  gitops, schedulers, file/copy flows, and network I/O. Use standalone, or as the
  landmine layer of the review skill.
argument-hint: "[pr-number-or-link]"
allowed-tools: Bash, Read, Grep, Glob, Write
---

# landmine-review

Reverse-engineered from an experienced reviewer's method, which comes down to
two prompts that "seriously help" —

1. **"Find me the landmines in this PR"**
2. **"What decisions were made as part of this PR"**

That's it — no specialised tool, no domain. This skill turns those two prompts
into a repeatable lens. It is **domain-agnostic**: the same questions apply to a
database write, a migration, a lock, an identity mapping, a gitops overlay, a
scheduler, a file/copy flow, or a network call.

## What a landmine is

Not a bug — a bug throws and you notice. A **landmine** is a failure that
**returns success while being wrong**, or that detonates only under a real
condition the diff never mentions (a crash mid-write, a slow dependency, a
customer overlay, an empty/oversized/odd input, a sibling PR's shape). The code
*looks* complete; nobody notices until production.

## Two passes

### Pass 1 — Landmines ("what silently fails?")
For every changed unit, deploy it in your head into the messiest realistic
environment and input, then look for the failure nobody will see until prod.

### Pass 2 — Decisions ("what was decided here, silently?")
Surface every non-obvious decision the diff *encodes* without stating: a changed
default, a dropped fallback, a new dependency, an ordering, a chosen error
behaviour, a scope boundary. List them back so the author/reviewer can confirm
each was intentional. Many landmines are just an unexamined decision.

## The landmine classes (domain-agnostic)

Derived from a point-in-time read of several hundred real review messages (not
a metric to maintain — the ordering is guidance, not a contract). The three hit most
in practice are **resource/lock/concurrency, deployment/config/default, and
silent-failure**; class 1 is listed first because it's the archetype every other
class collapses toward (a failure with no signal). For each: the *tell* in code
and the *question* to ask. Examples span the whole codebase, not one subsystem.

1. **Silent failure / swallowed error.** A failure is caught-and-logged, or
   produces an empty/partial/placeholder result, so the operation reports
   success. *Tells:* `except: log.warning(...)` then continue; a per-item loop
   that skips failures; an id remap that keeps only ids present in a map so
   failed items drop out; `errors="replace"`; `.get(k)` fallthrough. *Example:*
   "one item that cannot be copied bricks the whole bulk copy — opaquely; the
   real cause lives only in a swallowed log line" / "a failed upload drops out
   of the record's attachment list with no 500 and no user signal." *Ask:* if this fails for one
   item/row/file, does the caller get a typed error or a happy-looking partial?
   *High-yield seam — lenient-swallow + strict-gate:* when a new strict gate
   (`if len(copied) != len(all): raise 500`) is bolted on top of helpers that
   *swallow-and-continue*, the two layers disagree about what a partial result
   means, and the gate fails **opaquely** — a generic message while the real
   cause sits in a swallowed log. Don't praise the gate as "good isolation" and
   stop: check what its failure tells the operator, and whether sibling paths
   (e.g. two resource types moved by the same flow) handle partial the same way or asymmetrically.

2. **Partial-write / non-atomic multi-step.** Two+ writes that aren't one
   transaction; a snapshot taken before the transaction a rollback won't undo.
   *Tells:* `create_x(...)` then `update_y(...)` as separate calls; a write +
   a file upload; index stamp split from the row insert. *Example:*
   "insert_run then advance_schedule as separate calls; crash between them →
   run row exists but next_due_at never advances; retry duplicates (no unique
   constraint)" / "orphaned snapshot on rollback." *Ask:* process
   dies between A and B — what's orphaned, what does retry double-create, is
   there a constraint to stop it?

3. **Resource / lock / concurrency.** A connection or lock held across slow I/O;
   a global lock serializing everything; an idle pooled connection pinned during
   a serial loop; a missing shutdown drain; sync CPU on an async loop. *Example:*
   "one asyncio.Lock serializes ALL export generation; one slow export (1h
   timeout) blocks every queued job" / "pooled conn + advisory lock held idle
   across a serial loop of remote API calls." *Ask:* under burst/backlog, does one slow item
   stall everyone? what's held while we wait?

4. **Deployment / config / default / stale-read window.** Assumes a default,
   env var, schema, or network shape some customer overlay doesn't have;
   default-on vs the ticket's intent; gitops/helm ordering drift across
   stg/prod; behaviour that only holds single-pod and breaks multi-pod.
   *Tells:* `default=True` on a capability; reads of a secondary schema with no
   absent-fallback; a value patched in one overlay but not the others.
   *Passive-staleness sub-pattern (high-yield, easy to miss):* a state change
   (soft-delete, flag flip, config update, cache invalidation) that an **async
   consumer** — scheduler, poller, cache, in-memory map — won't observe until
   its next sync tick, leaving a window where the system acts on stale state.
   *Example:* "setting `deleted_at`/`is_active=FALSE` doesn't notify the
   scheduler; the in-process scheduled job persists until the next sync (up to
   5 min) and a deleted recurring job still fires." *Ask:* deploy this in the env/replica-count
   that set it the other way — does it fail loud or silently misbehave? When
   state changes here, who else holds a stale copy, and for how long?
   *Enablement / reachability sub-pattern (high-yield, easy to miss):* the SAFE
   default is verified (flag off → tool drops; capability absent → fail-open) but
   the **ON path is never traced end-to-end**, or a unit is correct-when-called
   but **never called in prod**. *Tells:* a feature-config field consumed
   by code with no env→config binding that ever sets it; a reaper/drainer/
   refunder whose only callers are its own definition + tests (`grep` proves it).
   *Ask:* can this flag actually be turned ON by ANY real deployment — trace env
   var → config → feature config → ctx → call site? Who *invokes* this in prod, on
   what trigger? "Behaves correctly when called / when off" is not "reachable
   when on / ever called" — a feature unreachable from every deployment, or a
   periodic task with no scheduler, is dead-on-arrival even with every off-path
   test green.

5. **Migration / schema / data / back-compat.** In-place irreversible mutation
   with a no-op `down.sql` and no verify step; a migration-number collision; a
   backfill that assumes a column/format. *Example:* "migration mutates
   ownership in place with a no-op down and no verify" / "a migration number
   already taken on main collides → breaks the migrator on merge." *Also — a
   new predicate/filter that sweeps in pre-existing rows:* a check added this PR
   (`if row.copied_from_id and not metadata.is_copy: treat as share-origin`)
   may match data written *before* the PR existed. *Example:* "every pre-PR
   record (old copies, old received shares) fits this shape, so they're
   permanently unshareable." *Ask:* is it
   reversible, does the number collide, what happens on double-apply, and does
   any new predicate also capture legacy rows it shouldn't?

6. **Cross-system / cross-PR implicit contract.** Works only if another system
   (frontend, sibling PR, migration) sends exactly the right shape, and nothing
   in *this* diff pins it. *Example:* "the record id is BIGINT; the frontend
   (a sibling PR) must send the stringified DB id — nothing here pins that
   contract; send a UUID and every lookup 404s." *Ask:* what must the other side do, and is
   it tested here? *A robust-looking backend is NOT a discharge:* confirming
   this side handles its input correctly (e.g. `str(id)` compares cleanly) does
   not close the finding — the landmine is that nothing *enforces* the
   counterpart's shape. If the only thing keeping it working is an untested
   convention on the other side, flag it even though this side is correct.

7. **Future-refactor bug-bait.** Correct today only because of a guard
   elsewhere; asymmetric inputs to two call sites; a duplicated predicate that
   will drift. *Ask:* if someone deletes the "redundant" guard next quarter
   because "the caller already gates it," what breaks?

8. **Input-shape / boundary.** Empty, null, oversized, mixed-case, duplicate,
   timezone/DST, locale, pagination-truncated, or binary/encoded inputs the
   happy path didn't consider. *Example:* "`datetime(... tzinfo=tz)`
   produces ambiguous/non-existent times during DST (2:30am spring-forward);
   no explicit test" / "a time picker that snaps to 30 minutes silently rounds
   a saved 9:15 to 9:30 on load — user saves without noticing." *Ask:* feed it the ugliest
   realistic input — garbage out, crash, or silent wrong answer?

9. **Test gives false confidence (false GREEN / false RED).** A test that can't
   fail on the thing it claims to guard (asserts the wrong selector/string, so a
   real regression stays green), or one that fails on a healthy state (matches
   too narrowly, so a valid empty/variant state goes red). *Example:*
   "`[role=\"alert\"]` is not where the app renders this error — this assertion
   can never fail on the errors it's meant to catch (false GREEN)" / "this regex
   misses two real empty-state strings, so a healthy-but-empty environment fails
   the smoke test (false RED)." *Ask:* what real regression would this test miss, and
   what healthy state would it wrongly fail on? Does the assertion actually bind
   to the behaviour under test? *Guard/cap-test-drives-real-input sub-pattern:* a
   guard or cap test that hand-builds the trigger value the real upstream never
   produces is FALSE-GREEN — it pins the guard's logic while the guard is dead on
   the live path. *Example:* "`test_classify_neutralises_html` feeds a
   hard-coded `text/html`, but the real sniffer returns `text/plain` for markup,
   so the XSS neutralisation never fires in production." Corollary: a cap/
   watermark keyed on a field the real source leaves empty (`candidate.size` from
   a `listFiles` that carries no size → always 0) is a silent no-op. *Ask:* does
   the real source actually emit the value this guard/cap keys on? Feed the test
   the shape the live API returns, not the shape the spec assumed.

(Network specifics — proxies, cookies-over-redirect, charset/binary bodies — are
just class 4/8 instances for the HTTP domain, not a separate concern.)

## Signature moves (do these — they're what makes a landmine review land)

- **Steel-man the direction first.** State what the change is right about and why
  the status quo is worse. Separate "wrong direction" from "right direction,
  risky execution." Most landmine reviews conclude RIGHT-HEADED.
- **Discharge non-issues out loud.** When you suspect a landmine and confirm it's
  safe, say so ("checked X — non-issue because Y; do not re-flag"). Stops the
  next reviewer re-litigating.
- **Every finding gets a concrete fix**, or the fallback: "at minimum, document
  as a known v1 limitation and file a follow-up ticket."
- **Ground every claim** in the actual code (read the swallow site, the schema,
  the call sites). Discard anything you can't verify.
- **Mark probed vs assumed in the discharge.** When "verified safe" rests on an
  external contract you have NOT observed live (API response shape, sniffer
  output, header behaviour), say so: "safe IF listFiles carries size / IF the
  sniffer returns text/html — UNVERIFIED against a live probe." Never promote a
  spec-assumed shape into "do not re-flag"; re-open it the moment the real shape
  is known. Re-run the full review after a rework that changes an external-API
  shape — a prior "verified safe" built on the old assumption does not carry
  forward (a rework can turn a working guard/cap into a dead one without changing
  its line count).

## Method

1. Check the PR out (`gh pr checkout <n>` / existing worktree), `gh pr view <n>
   --json title,body,files`, `gh pr diff <n>`. As a dispatched layer, use the
   worktree and diff scope the CONTEXT file names instead; never check out. **Enumerate every changed file**
   — backend, frontend, migrations, config, tests — and review each in full (the
   landmine is usually in the surrounding code the diff calls into, not the
   hunk). If you skip any file, say which and why: silently reviewing only the
   "interesting" backend files is itself a coverage landmine — half the findings
   (UI silent-mutation, migration back-compat) live in the files a backend-only
   pass skips.
2. Steel-man the direction.
3. Pass 1: walk the nine classes against every unit that writes, holds a
   resource, crosses a boundary, parses input, adds a default, or adds a test.
   Verify each hit.
4. Pass 2: list the unstated decisions for confirmation.
5. Discharge non-issues; give every finding a fix.

## Output

When dispatched as a layer of the `review` or `fold-review` skill, write the
report to the findings file the dispatch prompt names
(`<outdir>/REVIEW-<n>-landmine.md`) and end it with the terminator line that
prompt asks for. Run standalone, report the findings **inline in your
response** and write no file. Either way the caller owns any GitHub posting.
**Local only — never post to GitHub yourself.** Use
the structure below as the shape of your returned report. Cite every position
repo-root-relative, never a shortened or absolute path. Tags in severity order: `**blocker:**` · `**should-fix:**` ·
`**landmine:**` · `**bug-bait:**` · `**worth confirming:**` · `**nitpick:**`.
Emoji: 🔴 blocker · 🟠 should-fix · 🟡 question · ⚪ nitpick.

```markdown
# Landmine review — PR #<n> <title>

## Direction (steel-manned)
<what it's right about; right-headed?>

## Landmines & should-fixes
### 🟠/🔴 <one-line headline naming the silent failure>
<mechanism: the causal chain> · <trigger: the realistic condition> ·
<blast radius> · <fix>  (path/from/repo/root.py:LINE)

## Decisions made (confirm each was intentional)
- <changed default / dropped fallback / new dependency / ordering / scope cut>

## Limitations worth flagging in the PR description
<caps, defaults, unsupported environments/inputs>

## Verified safe — do not re-flag
<suspected landmines that are actually fine, with why>

## Verdict
<approve / comment> — 🔴 N · 🟠 N · 🟡 N · ⚪ N

Checked: <files, functions and paths you actually examined>
```

## Recurring landmines for THIS repo (prime on these)

The classes above are domain-agnostic and travel everywhere. A repository's own
repeat offenders do not live in this file, so that no codebase's internals are
baked into a skill that reviews every codebase. If the environment provides a
repository-specific pitfall list (a tool, or a file the repo's agent docs point
to), read it before Pass 1. What comes back is a list of priors, not findings:
check each by reflex when the diff touches its area, and say nothing about the
ones that do not apply. If none is available, or it is empty, review with the
domain-agnostic classes alone and do not invent repo specifics to fill the gap.

## When to run

Highest yield on changes that write data, do multi-step/transactional work, hold
locks/connections, add migrations, cross a system/PR boundary, parse external
input, or set a deploy default. Low yield on pure refactors/docs/single-pure-
function changes — say so and keep it short rather than manufacturing landmines.
