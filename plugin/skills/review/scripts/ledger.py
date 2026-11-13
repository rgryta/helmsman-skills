import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import _verdict  # noqa: E402
import verify_citations  # noqa: E402

FULL_REHUNT_EVERY = 5
CHECKED = re.compile(r"^[\s>#*_`-]*checked[*_`]*\s*:?[*_`]*\s*(?P<rest>.*)$", re.I)
NAMED = re.compile(r"`[^`]+`|\w\w/|/\w\w|\w\w\.[A-Za-z]")  # \w\w keeps e.g./i.e. out
STATUSES = ["open", "fixed", "regressed", "wontfix"]
UPSERT_FIELDS = ("id", "title", "file", "line", "severity", "provenance", "status")
FINDING = re.compile(
    r"^(?:#+|[-*>]|\d+[.)])\W*(?:blocker|should[- ]fix|landmine|bug[- ]bait"
    r"|worth confirming|question|nitpick)[*_`]*\s*[:\]]",
    re.I,
)


def _path(args):
    d = Path(args.dir).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    owner, name = args.repo.split("/", 1)
    return d / f"{owner}__{name}__pr{args.pr}.json"


def _load(p, a):
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError) as e:
            aside = p.with_name(f"{p.name}.corrupt-{int(time.time())}")
            os.replace(p, aside)
            print(
                f"ERROR: ledger {p} unreadable ({type(e).__name__}); moved to {aside}, starting fresh",
                file=sys.stderr,
            )
    return {"repo": a.repo, "pr": a.pr, "rounds": [], "findings": []}


def _write_atomic(p, data):
    tmp = p.with_name(p.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, p)


def _lists_checked(lines):
    for i, line in enumerate(lines):
        m = CHECKED.match(line)
        if m and NAMED.search(" ".join([m["rest"], *lines[i + 1 :]])):
            return True
    return False


def _classify(path):
    try:
        text = Path(path).read_text()
    except OSError:
        return "MALFORMED"
    verdict, detail = _verdict.classify(text)
    if verdict == "COMPLETE":
        body = [line.strip() for line in text.splitlines() if line.strip()][:-1]
        body.append(detail)
        if not (
            verify_citations.collect(text)
            or any(FINDING.match(line) for line in body)
            or _lists_checked(body)
        ):
            return "STUB"
        return "COMPLETE"
    if verdict == "PARTIAL":
        return f"PARTIAL {detail}".rstrip()
    return "MALFORMED"


def _finding_id(entry):
    """A finding without an id is keyed by file and title, so distinct findings never merge."""
    if entry.get("id"):
        return entry["id"]
    key = f"{entry.get('file') or ''}|{(entry.get('title') or '').strip().lower()}"
    return "h" + hashlib.sha1(key.encode()).hexdigest()[:10]


def _upsert(data, entry, sha):
    entry = {**entry, "id": _finding_id(entry)}
    f = next((x for x in data["findings"] if x["id"] == entry["id"]), None)
    rec = {k: entry.get(k) for k in UPSERT_FIELDS} | {"last_seen_sha": sha}
    if f:
        f.update({k: v for k, v in rec.items() if v is not None})
    else:
        rec["status"] = rec["status"] or "open"
        rec["first_seen_sha"] = sha
        data["findings"].append(rec)


def _set_status(data, entry):
    f = next((x for x in data["findings"] if x["id"] == entry["id"]), None)
    if f is None:
        return f"no finding with id {entry['id']}"
    f["status"] = entry["status"]
    return None


def _read_batch(path, fields, required):
    try:
        entries = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        return None, f"--from-json {path}: {type(e).__name__}: {e}"
    if not isinstance(entries, list):
        return None, f"--from-json {path}: expected a JSON list"
    out = []
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            return None, f"entry {i}: expected an object"
        if extra := set(e) - fields:
            return None, f"entry {i}: unknown fields {sorted(extra)}"
        if missing := [k for k in required if e.get(k) is None]:
            return None, f"entry {i}: missing {missing}"
        if e.get("status") is not None and e["status"] not in STATUSES:
            return None, f"entry {i}: invalid status {e['status']!r}"
        out.append({k: None if v is None else str(v) for k, v in e.items()})
    return out, None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "cmd",
        choices=[
            "round-start",
            "round-end",
            "upsert",
            "set-status",
            "report",
            "delta",
            "classify-report",
        ],
    )
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", required=True)
    ap.add_argument("--dir", default="~/.helmsman/review-ledger")
    ap.add_argument("--sha")
    ap.add_argument("--mode", choices=["full", "delta"], default="full")
    ap.add_argument("--full-every", type=int, default=None)
    ap.add_argument("--complete", action="store_true")
    ap.add_argument("--partial")
    ap.add_argument("--skipped")
    ap.add_argument("--id")
    ap.add_argument("--title")
    ap.add_argument("--file")
    ap.add_argument("--line")
    ap.add_argument("--severity")
    ap.add_argument("--provenance")
    ap.add_argument("--status", choices=STATUSES, default=None)
    ap.add_argument("--from-json")
    a = ap.parse_args(argv)
    if a.cmd == "classify-report":
        if not a.file:
            print("classify-report requires --file", file=sys.stderr)
            return 1
        print(_classify(a.file))
        return 0
    if a.cmd in ("round-start", "round-end") and not a.sha:
        print(f"{a.cmd} requires --sha", file=sys.stderr)
        return 1
    if a.cmd == "set-status" and a.status is None and not a.from_json:
        print("set-status requires --status", file=sys.stderr)
        return 1
    single = {
        "upsert": {k: getattr(a, k) for k in UPSERT_FIELDS},
        "set-status": {"id": a.id, "status": a.status},
    }.get(a.cmd)
    batch = [single]
    if a.from_json and single is not None:
        batch, err = _read_batch(
            a.from_json,
            set(single),
            ["id", "status"] if a.cmd == "set-status" else [],
        )
        if err:
            print(err, file=sys.stderr)
            return 1
    lock = open(str(_path(a)) + ".lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        p = _path(a)
        data = _load(p, a)
        if a.cmd == "round-start":
            data["rounds"].append(
                {
                    "sha": a.sha,
                    "ts": time.time(),
                    "complete": None,
                    "partial_reason": None,
                    "mode": a.mode,
                }
            )
        elif a.cmd == "round-end":
            r = next((x for x in reversed(data["rounds"]) if x["sha"] == a.sha), None)
            if r is None:
                print(f"no round with sha {a.sha}", file=sys.stderr)
                return 1
            r["complete"] = bool(a.complete and not a.partial)
            r["partial_reason"] = a.partial
            r["skipped_layers"] = [
                x.strip() for x in (a.skipped or "").split(",") if x.strip()
            ]
        elif a.cmd == "upsert":
            for entry in batch:
                _upsert(data, entry, a.sha)
        elif a.cmd == "set-status":
            for i, entry in enumerate(batch):
                if err := _set_status(data, entry):
                    print(f"entry {i}: {err}" if a.from_json else err, file=sys.stderr)
                    return 1
        elif a.cmd == "report":
            print(f"# Ledger {a.repo}#{a.pr}")
            for r in data["rounds"][-3:]:
                skipped = ",".join(r.get("skipped_layers") or [])
                print(
                    f"- round {(r.get('sha') or '')[:10]} complete={r['complete']}"
                    f"{f' skipped={skipped}' if skipped else ''} {r['partial_reason'] or ''}"
                )
            open_f = [f for f in data["findings"] if f["status"] == "open"]
            for f in open_f:
                print(
                    f"- [open] {f['id']} {f['severity']} {f['file']}:{f['line']} ({f['provenance']}) {f['title']}"
                )
            closed = [f for f in data["findings"] if f["status"] != "open"]
            if closed:
                by = {}
                for f in closed:
                    by[f["status"]] = by.get(f["status"], 0) + 1
                print(
                    f"- {len(closed)} closed: "
                    + ", ".join(f"{v} {k}" for k, v in sorted(by.items()))
                )
            return 0
        elif a.cmd == "delta":
            rounds = data["rounds"]
            if (
                not rounds
                or rounds[-1]["complete"] is not True
                or rounds[-1].get("skipped_layers")
                or rounds[-1].get("sha") == a.sha
            ):
                print("FULL_REHUNT")
                return 0
            threshold = a.full_every if a.full_every is not None else FULL_REHUNT_EVERY
            streak = 0
            for r in reversed(rounds):
                if r.get("mode") != "delta":
                    break
                streak += 1
            if streak >= threshold:
                print("FULL_REHUNT")
            else:
                print(f"DELTA_SINCE {rounds[-1]['sha']}")
            return 0
        _write_atomic(p, data)
        return 0
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
