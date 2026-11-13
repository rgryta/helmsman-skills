import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import _diff  # noqa: E402

RULES = Path(__file__).resolve().parents[1] / "semgrep-rules"
_LOCAL_RULE_ID_RE = re.compile(r"^\s*-\s*id:\s*(\S+)", re.MULTILINE)
COMMUNITY_CONFIGS = ("p/python", "p/security-audit")
MAX_PER_GROUP = 20
COMMUNITY_TIMEOUT_S = 90  # generous vs the ~5s observed cost; still leaves LOCAL_TIMEOUT_S room under phase0.PER_CHECK_S
LOCAL_TIMEOUT_S = 120


def _run_semgrep(d, configs, files, worktree, timeout):
    for tool in (["semgrep"], ["uvx", "semgrep"]):
        args = list(tool)
        for c in configs:
            args += ["--config", c]
        args += ["--json", "--quiet", *files]
        code, out = d.run_cmd(args, cwd=worktree, timeout=timeout)
        if code == -2:
            continue
        return code, out
    return -2, "MISSING"


def _parse(out):
    try:
        return json.loads(out[out.index("{") :])
    except (ValueError, json.JSONDecodeError):
        return None


def _registry_unreachable(payload):
    if payload.get("paths", {}).get("scanned", []):
        return False
    # semgrep aborts the whole run (nothing scanned) if any --config pack fails to
    # download, and this is its exact wording for that failure (v1.176.0, verified live)
    return any(
        "semgrep.dev/c/" in str(e.get("message", "")) for e in payload.get("errors", [])
    )


def _local_rule_ids():
    ids = set()
    for f in RULES.glob("*.y*ml"):
        ids.update(_LOCAL_RULE_ID_RE.findall(f.read_text(errors="ignore")))
    return ids


def _attribute(check_id, local_ids):
    # a local --config dir makes semgrep prefix the bare rule id with its
    # (possibly absolute) filesystem path turned into dots, so classify by the
    # bare id, not by id shape, and never render the path itself
    bare = check_id.rsplit(".", 1)[-1]
    if check_id in local_ids or bare in local_ids:
        return f"local:{bare}"
    return f"community:{check_id.rsplit('/', 1)[-1]}"


_NOT_ADDED_NOTE = (
    "- these did not land on a line this diff added. Still worth reading: a guard "
    "that moved can break a call that did not. Triage, do not auto-dismiss."
)
_UNKNOWN_NOTE = (
    "- provenance could not be determined for these files, so they are neither "
    "confirmed new nor confirmed pre-existing. Read them as if new."
)


def _render(payload, code, files, scope, local_ids, note=None, added_by_path=None):
    hits = payload.get("results", [])
    errors = payload.get("errors", [])
    paths = payload.get("paths", {})
    scanned, skipped = paths.get("scanned", []), paths.get("skipped", [])
    if code not in (0, 1) and not hits:
        msg = f"SKIPPED: semgrep exited {code} with no results"
    elif not hits:
        if errors or skipped:
            detail = "; ".join(str(e.get("message", e))[:160] for e in errors[:5])
            msg = (
                f"SKIPPED: semgrep scanned {len(scanned)} file(s), skipped "
                f"{len(skipped)} - not clean, rulebook could not fully scan"
                + (f" ({detail})" if detail else "")
            )
        else:
            msg = f"OK: {scope} clean on {len(files)} changed files"
    else:
        added_by_path = added_by_path or {}
        groups = {"new": [], "old": [], "unknown": []}
        for h in hits:
            row = (
                f"- {h['path']}:{h['start']['line']} "
                f"[{_attribute(h['check_id'], local_ids)}] {h['extra']['message'][:120]}"
            )
            added = added_by_path.get(h["path"], None)
            if added is None:
                groups["unknown"].append(row)
            else:
                span = range(h["start"]["line"], h.get("end", h["start"])["line"] + 1)
                groups["new" if any(ln in added for ln in span) else "old"].append(row)
        parts = [f"FINDINGS: {scope}"]
        # capped per group, not before the split: a shared cap spends itself on
        # whichever group sorts first and silently drops the other entirely
        for key, title, group_note in (
            ("new", "### added by this diff", None),
            ("unknown", "### provenance unknown", _UNKNOWN_NOTE),
            ("old", "### not on a line this diff added", _NOT_ADDED_NOTE),
        ):
            rows = groups[key]
            if not rows:
                continue
            parts.append(title)
            parts.extend(rows[:MAX_PER_GROUP])
            if len(rows) > MAX_PER_GROUP:
                parts.append(f"- (+{len(rows) - MAX_PER_GROUP} more not shown)")
            if group_note:
                parts.append(group_note)
        msg = "\n".join(parts)
    if note:
        msg += f"\n- NOTE: {note}"
    return msg


def run(worktree, base):
    files = _diff.changed_py_files(worktree, base)
    if not files:
        return "OK: no python changes"
    added_by_path = _diff.added_lines_by_path(worktree, base, files)

    code, out = _run_semgrep(
        _diff, [*COMMUNITY_CONFIGS, str(RULES)], files, worktree, COMMUNITY_TIMEOUT_S
    )
    if code == -2:
        return "SKIPPED: semgrep unavailable"

    local_ids = _local_rule_ids()
    payload = None if code in (-1, -3) else _parse(out)
    if payload is not None and not _registry_unreachable(payload):
        return _render(
            payload,
            code,
            files,
            "community packs (p/python, p/security-audit) + local rulebook",
            local_ids,
            added_by_path=added_by_path,
        )

    if code == -1:
        reason = f"community-pack attempt timed out after {COMMUNITY_TIMEOUT_S}s (registry fetch and/or scan)"
    elif code == -3:
        reason = f"registry fetch errored ({out.removeprefix('ERROR ')[:160]})"
    elif payload is None:
        reason = f"registry attempt produced unparseable output (exit {code})"
    else:
        detail = "; ".join(
            str(e.get("message", ""))[:160]
            for e in payload.get("errors", [])
            if "semgrep.dev/c/" in str(e.get("message", ""))
        )
        reason = detail or f"registry configs did not load (exit {code})"

    code2, out2 = _run_semgrep(_diff, [str(RULES)], files, worktree, LOCAL_TIMEOUT_S)
    if code2 == -2:
        return (
            f"SKIPPED: semgrep unavailable (community packs also unavailable: {reason})"
        )
    payload2 = _parse(out2)
    if payload2 is None:
        return (
            f"SKIPPED: semgrep output unparseable (exit {code2}); "
            f"community packs unavailable ({reason})"
        )
    return _render(
        payload2,
        code2,
        files,
        "local rulebook only",
        local_ids,
        note=f"community packs unavailable ({reason}) - coverage is narrower than usual",
        added_by_path=added_by_path,
    )


if __name__ == "__main__":
    try:
        print(run(sys.argv[1], sys.argv[2]))
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
    sys.exit(0)
