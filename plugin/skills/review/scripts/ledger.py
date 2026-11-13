import argparse
import fcntl
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import _verdict  # noqa: E402

FULL_REHUNT_EVERY = 5


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


def _classify(path):
    try:
        verdict, detail = _verdict.classify(Path(path).read_text())
    except OSError:
        return "MALFORMED"
    if verdict == "COMPLETE":
        return "COMPLETE"
    if verdict == "PARTIAL":
        return f"PARTIAL {detail}".rstrip()
    return "MALFORMED"


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
    ap.add_argument(
        "--status", choices=["open", "fixed", "regressed", "wontfix"], default=None
    )
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
    if a.cmd == "set-status" and a.status is None:
        print("set-status requires --status", file=sys.stderr)
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
            f = next((x for x in data["findings"] if x["id"] == a.id), None)
            rec = {
                "id": a.id,
                "title": a.title,
                "file": a.file,
                "line": a.line,
                "severity": a.severity,
                "provenance": a.provenance,
                "status": a.status,
                "last_seen_sha": a.sha,
            }
            if f:
                f.update({k: v for k, v in rec.items() if v is not None})
            else:
                rec["status"] = rec["status"] or "open"
                rec["first_seen_sha"] = a.sha
                data["findings"].append(rec)
        elif a.cmd == "set-status":
            f = next((x for x in data["findings"] if x["id"] == a.id), None)
            if f is None:
                print(f"no finding with id {a.id}", file=sys.stderr)
                return 1
            f["status"] = a.status
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
