"""Set up one review or fold round for a PR and print a single JSON object.

Review mode: detached worktree `<repo>-worktrees/review-<n>` at the head SHA,
`<base-dir>/<owner>__<repo>__pr<n>/run-<epoch>/`, ledger delta + round-start.
Fold mode (`--fold`): worktree `<repo>-worktrees/fold-<n>` on the PR branch,
`fold-<epoch>/`, no ledger calls. Both take a lease on the worktree.
`--release <worktree>` drops this caller's (or a dead holder's) lease.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

LEDGER = Path(__file__).with_name("ledger.py")
LEASE_NAME = "helmsman-lease.json"
KEEP_RUNS = 3
WRAPPERS = {"sh", "bash", "zsh", "dash", "fish", "env", "uv", "timeout", "nohup"}
PR_FIELDS = (
    "number,title,url,state,isDraft,headRefName,baseRefName,headRefOid,"
    "body,files,commits"
)


class SetupError(Exception):
    def __init__(self, message, code=1, **extra):
        super().__init__(message)
        self.code = code
        self.extra = extra


def _run(args, cwd=None, check=True):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise SetupError(
            f"{' '.join(args)} failed ({r.returncode}): {r.stderr.strip()[-400:]}"
        )
    return r


def _owner_pid():
    """The first ancestor that is not a shell or launcher: the agent session that outlives this call."""
    pid = os.getppid()
    while pid > 1:
        r = _run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], check=False)
        parts = r.stdout.split(None, 1)
        if r.returncode != 0 or len(parts) != 2:
            break
        if Path(parts[1].strip()).name.lstrip("-") not in WRAPPERS:
            return pid
        pid = int(parts[0])
    return os.getppid()


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lease_path(worktree):
    git_dir = _run(
        ["git", "rev-parse", "--path-format=absolute", "--git-dir"], cwd=worktree
    )
    return (
        Path(git_dir.stdout.strip()) / LEASE_NAME
    )  # per-worktree git dir: never tracked, never committed


def _read_lease(path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _holds(lease, pid, tag):
    return lease is not None and lease.get("pid") == pid and lease.get("tag") == tag


def take_lease(worktree, pid, tag):
    path = _lease_path(worktree)
    current = _read_lease(path)
    if current is not None and not _holds(current, pid, tag):
        if _alive(current.get("pid", 0)):
            raise SetupError(
                "worktree is leased by another live session",
                code=3,
                worktree=str(worktree),
                holder=current,
                same_session=current.get("pid") == pid,
            )
    if current is not None:
        path.unlink(missing_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        raise SetupError(
            "lost a race for the worktree lease",
            code=3,
            worktree=str(worktree),
            holder=_read_lease(path),
        ) from None
    with os.fdopen(fd, "w") as f:
        json.dump({"pid": pid, "tag": tag, "ts": time.time()}, f)
    return str(path)


def release_lease(worktree, pid, tag):
    path = _lease_path(worktree)
    current = _read_lease(path)
    if current is None:
        return {"ok": True, "released": False, "worktree": str(worktree)}
    if not _holds(current, pid, tag) and _alive(current.get("pid", 0)):
        raise SetupError(
            "lease belongs to another live session",
            code=3,
            worktree=str(worktree),
            holder=current,
            same_session=current.get("pid") == pid,
        )
    path.unlink(missing_ok=True)
    return {"ok": True, "released": True, "worktree": str(worktree)}


def prune_runs(pr_dir, prefix, keep):
    runs = sorted(
        (p for p in pr_dir.glob(f"{prefix}-*") if p.name[len(prefix) + 1 :].isdigit()),
        key=lambda p: int(p.name[len(prefix) + 1 :]),
    )
    pruned = []
    for p in runs[:-keep]:
        shutil.rmtree(p, ignore_errors=True)
        if not p.exists():
            pruned.append(str(p))
    return pruned


def _pr_view(repo, pr):
    return json.loads(
        _run(["gh", "pr", "view", str(pr), "-R", repo, "--json", PR_FIELDS]).stdout
    )


def _has_commit(checkout, sha):
    return (
        _run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=checkout, check=False
        ).returncode
        == 0
    )


def _fetch(checkout, pr, view, fold):
    _run(
        [
            "git",
            "fetch",
            "-q",
            "origin",
            f"pull/{pr}/head",
            view["baseRefName"],
            *([view["headRefName"]] if fold else []),
        ],
        cwd=checkout,
    )


def _require_clean(path, *status_args):
    dirty = _run(["git", "status", "--porcelain", *status_args], cwd=path).stdout
    if dirty.strip():
        raise SetupError(
            f"{path} has uncommitted changes from an earlier round",
            worktree=str(path),
            dirty=dirty.splitlines(),
        )


def _review_worktree(checkout, path, sha, pid, tag):
    reused = path.exists()
    if not reused:
        _run(["git", "worktree", "add", "--detach", str(path), sha], cwd=checkout)
    lease = take_lease(path, pid, tag)
    if reused:
        _require_clean(path, "--untracked-files=no")
    if reused and _run(["git", "rev-parse", "HEAD"], cwd=path).stdout.strip() != sha:
        _run(["git", "checkout", "-q", "--detach", sha], cwd=path)
    return reused, lease


def _fold_worktree(checkout, path, branch, sha, pid, tag):
    reused = path.exists()
    if not reused:
        _run(["git", "worktree", "add", str(path), branch], cwd=checkout)
    lease = take_lease(path, pid, tag)
    on = _run(["git", "branch", "--show-current"], cwd=path).stdout.strip()
    if on != branch:
        raise SetupError(
            f"{path} is on {on or 'a detached HEAD'}, not {branch}",
            worktree=str(path),
        )
    _require_clean(path)
    _run(["git", "merge", "-q", "--ff-only", f"origin/{branch}"], cwd=path)
    at = _run(["git", "rev-parse", "HEAD"], cwd=path).stdout.strip()
    if at != sha:
        raise SetupError(
            f"{path} is at {at}, not the PR head {sha}", worktree=str(path)
        )
    return reused, lease


def _ledger(args, *extra):
    return _run(
        [
            sys.executable,
            str(LEDGER),
            *extra,
            "--repo",
            args.repo,
            "--pr",
            str(args.pr),
            "--dir",
            str(args.base_dir),
        ]
    ).stdout.strip()


def _round_mode(args, worktree, head):
    delta = _ledger(
        args,
        "delta",
        "--sha",
        head,
        *(["--full-every", str(args.full_every)] if args.full_every else []),
    )
    since = delta.split()[1] if delta.startswith("DELTA_SINCE ") else None
    if since and (
        _run(
            ["git", "merge-base", "--is-ancestor", since, head],
            cwd=worktree,
            check=False,
        ).returncode
        != 0
    ):
        since = None
    mode = "delta" if since else "full"
    _ledger(args, "round-start", "--sha", head, "--mode", mode)
    return mode, since


def setup(args):
    _run(["gh", "auth", "status"])
    checkout = Path(
        _run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=args.checkout,
        ).stdout.strip()
    ).parent
    view = _pr_view(args.repo, args.pr)
    _fetch(checkout, args.pr, view, args.fold)
    if not _has_commit(checkout, view["headRefOid"]):
        view = _pr_view(args.repo, args.pr)
        _fetch(checkout, args.pr, view, args.fold)
    head = view["headRefOid"]
    kind = "fold" if args.fold else "review"
    worktree = (
        args.worktree.resolve()
        if args.worktree
        else checkout.parent / f"{checkout.name}-worktrees" / f"{kind}-{args.pr}"
    )
    if args.fold:
        reused, lease = _fold_worktree(
            checkout, worktree, view["headRefName"], head, args.owner_pid, args.tag
        )
    else:
        reused, lease = _review_worktree(
            checkout, worktree, head, args.owner_pid, args.tag
        )
    owner, name = args.repo.split("/", 1)
    pr_dir = args.base_dir / f"{owner}__{name}__pr{args.pr}"
    prefix = "fold" if args.fold else "run"
    outdir = pr_dir / f"{prefix}-{int(time.time())}"
    outdir.mkdir(parents=True, exist_ok=True)
    pruned = prune_runs(pr_dir, prefix, KEEP_RUNS)
    pr_json = outdir / "pr.json"
    pr_json.write_text(json.dumps(view, indent=1))
    out = {
        "ok": True,
        "repo": args.repo,
        "pr": args.pr,
        "title": view["title"],
        "url": view["url"],
        "state": view["state"],
        "is_draft": view["isDraft"],
        "head_ref": view["headRefName"],
        "base_ref": view["baseRefName"],
        "head_sha": head,
        "checkout": str(checkout),
        "worktree": str(worktree),
        "worktree_reused": reused,
        "lease": lease,
        "outdir": str(outdir),
        "pr_json": str(pr_json),
        "pruned": pruned,
    }
    if args.fold:
        out["pre_fold_sha"] = _run(
            ["git", "rev-parse", "HEAD"], cwd=worktree
        ).stdout.strip()
    else:
        out["mode"], out["delta_since"] = _round_mode(args, worktree, head)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo")
    ap.add_argument("--pr", type=int)
    ap.add_argument("--fold", action="store_true")
    ap.add_argument("--checkout", default=".")
    ap.add_argument(
        "--base-dir",
        type=lambda s: Path(s).expanduser(),
        default=Path("~/.helmsman/review-ledger").expanduser(),
    )
    ap.add_argument("--worktree", type=lambda s: Path(s).expanduser())
    ap.add_argument("--full-every", type=int)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--owner-pid", type=int)
    ap.add_argument("--release", metavar="WORKTREE")
    args = ap.parse_args(argv)
    if args.owner_pid is None:
        args.owner_pid = _owner_pid()
    try:
        if args.release:
            out = release_lease(Path(args.release), args.owner_pid, args.tag)
        else:
            if not args.repo or args.pr is None:
                ap.error("--repo and --pr are required unless --release is given")
            out = setup(args)
    except SetupError as e:
        print(json.dumps({"ok": False, "error": str(e), **e.extra}))
        return e.code
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
