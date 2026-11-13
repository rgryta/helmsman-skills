import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import SCRIPTS, _git, commit_all

PR = 7


@pytest.fixture
def env(tmp_path, monkeypatch):
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    (seed / "a.py").write_text("x = 1\n")
    commit_all(seed, "base")
    _git(seed, "checkout", "-b", "feat")
    (seed / "a.py").write_text("x = 2\n")
    commit_all(seed, "change")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=seed, capture_output=True, text=True
    ).stdout.strip()
    _git(seed, "checkout", "-q", "main")
    origin = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))
    _git(origin, "update-ref", f"refs/pull/{PR}/head", head)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "clone", "-q", str(origin), "repo")
    view = tmp_path / "view.json"
    view.write_text(
        json.dumps(
            {
                "number": PR,
                "title": "t",
                "url": "u",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "feat",
                "baseRefName": "main",
                "headRefOid": head,
                "body": "",
                "files": [],
                "commits": [],
            }
        )
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(f'#!/bin/sh\n[ "$1" = pr ] && cat {view}\nexit 0\n')
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return {
        "checkout": work / "repo",
        "base": tmp_path / "ledger",
        "head": head,
        "worktree": work / "repo-worktrees" / f"review-{PR}",
        "origin": origin,
        "view": view,
        "seed": seed,
    }


def setup(env, *extra, pid=None, tag="a", shell=None):
    cmd = [
        sys.executable,
        str(SCRIPTS / "setup_round.py"),
        "--repo",
        "o/r",
        "--pr",
        str(PR),
        "--base-dir",
        str(env["base"]),
        "--tag",
        tag,
        *extra,
    ]
    if shell:
        cmd = [shell, "-c", shlex.join(cmd)]
    else:
        cmd += ["--owner-pid", str(pid or os.getpid())]
    r = subprocess.run(
        cmd,
        cwd=env["checkout"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return r.returncode, json.loads(r.stdout)


def _dead_pid():
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def test_prints_one_json_object_with_every_path(env):
    """A first review round yields a detached worktree at the head SHA, a fresh outdir and a full-mode ledger round."""
    code, out = setup(env)
    assert code == 0, out
    assert out["worktree"] == str(env["worktree"])
    assert out["head_sha"] == env["head"]
    assert (out["head_ref"], out["base_ref"], out["is_draft"]) == (
        "feat",
        "main",
        False,
    )
    assert out["worktree_reused"] is False
    assert Path(out["outdir"]).name.startswith("run-")
    assert Path(out["outdir"]).parent == env["base"] / f"o__r__pr{PR}"
    assert json.loads(Path(out["pr_json"]).read_text())["headRefOid"] == env["head"]
    assert (out["mode"], out["delta_since"]) == ("full", None)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=out["worktree"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head == env["head"]
    rounds = json.loads((env["base"] / f"o__r__pr{PR}.json").read_text())["rounds"]
    assert [r["mode"] for r in rounds] == ["full"]


def test_reuses_the_worktree_at_the_same_sha(env):
    """A second round by the same holder reuses the existing worktree instead of failing the add."""
    assert setup(env)[0] == 0
    code, out = setup(env)
    assert code == 0, out
    assert out["worktree_reused"] is True


def test_live_lease_held_by_another_session_blocks_setup(env):
    """Two tasks never share a worktree: a live holder with a different tag wins."""
    assert setup(env, tag="a")[0] == 0
    code, out = setup(env, tag="b")
    assert code == 3
    assert out["ok"] is False
    assert out["holder"]["tag"] == "a"
    assert out["same_session"] is True


def test_empty_lease_left_by_a_crashed_writer_is_stale(env):
    """Leases are linked into place whole, so an empty one is a crash leftover that neither setup nor release may trip on."""
    _, out = setup(env, tag="a")
    lease = Path(out["lease"])
    lease.write_text("")
    code, out = setup(env, tag="b")
    assert code == 0, out
    assert json.loads(lease.read_text())["tag"] == "b"
    lease.write_text("")
    assert _release(out["worktree"], "c")["released"] is True
    assert not lease.exists()


@pytest.mark.parametrize("shell", ["sh", "bash"])
def test_owner_pid_skips_the_shell_wrapper(env, shell):
    """Without --owner-pid the lease names the process that launched the shell, not the short-lived shell."""
    code, out = setup(env, shell=shell)
    assert code == 0, out
    assert json.loads(Path(out["lease"]).read_text())["pid"] == os.getpid()


def test_reused_review_worktree_with_tracked_edits_fails(env):
    """A review never runs on a tree carrying a tracked edit left by an earlier round."""
    _, out = setup(env)
    Path(out["worktree"], "a.py").write_text("x = 3\n")
    code, out = setup(env)
    assert code == 1
    assert "uncommitted changes" in out["error"]


def _push_feat_commit(env):
    _git(env["seed"], "checkout", "-q", "feat")
    (env["seed"] / "a.py").write_text("x = 4\n")
    commit_all(env["seed"], "more")
    _git(env["seed"], "push", "-q", str(env["origin"]), "feat")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=env["seed"], capture_output=True, text=True
    ).stdout.strip()
    _git(env["origin"], "update-ref", f"refs/pull/{PR}/head", head)
    view = json.loads(env["view"].read_text())
    view["headRefOid"] = head
    env["view"].write_text(json.dumps(view))
    return head


def test_reused_fold_worktree_fast_forwards_to_the_pr_head(env):
    """A fold reusing its worktree starts from the PR's current head, not the commit the last round left."""
    assert setup(env, "--fold")[0] == 0
    head = _push_feat_commit(env)
    code, out = setup(env, "--fold")
    assert code == 0, out
    assert out["worktree_reused"] is True
    assert out["pre_fold_sha"] == head


def test_dirty_fold_worktree_fails(env):
    """Uncommitted edits from a crashed fold are never carried into the next fold."""
    _, out = setup(env, "--fold")
    Path(out["worktree"], "a.py").write_text("x = 5\n")
    code, out = setup(env, "--fold")
    assert code == 1
    assert "uncommitted changes" in out["error"]


def test_stale_lease_is_taken_over(env):
    """A lease whose holder process is dead does not block the next session."""
    assert setup(env, pid=_dead_pid(), tag="a")[0] == 0
    code, out = setup(env, tag="b")
    assert code == 0, out
    assert json.loads(Path(out["lease"]).read_text())["tag"] == "b"


def _release(worktree, tag):
    r = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "setup_round.py"),
            "--release",
            worktree,
            "--owner-pid",
            str(os.getpid()),
            "--tag",
            tag,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(r.stdout)


def test_release_drops_the_callers_lease(env):
    """Release frees the worktree for another live session."""
    _, out = setup(env, tag="a")
    assert _release(out["worktree"], "a")["released"] is True
    assert not Path(out["lease"]).exists()
    assert setup(env, tag="b")[0] == 0


def test_prunes_stale_run_dirs_keeping_the_newest(env):
    """Only the newest three run dirs survive setup, so leftovers a blocked cleanup left behind cannot pile up."""
    pr_dir = env["base"] / f"o__r__pr{PR}"
    for epoch in (1, 2, 3):
        (pr_dir / f"run-{epoch}").mkdir(parents=True)
    (pr_dir / "fold-1").mkdir()
    _, out = setup(env)
    assert sorted(Path(p).name for p in out["pruned"]) == ["run-1"]
    assert sorted(p.name for p in pr_dir.iterdir() if p.is_dir()) == sorted(
        ["fold-1", "run-2", "run-3", Path(out["outdir"]).name]
    )


def test_fold_mode_checks_out_the_branch(env):
    """A fold worktree sits on the PR branch so it can push, and skips the review ledger."""
    code, out = setup(env, "--fold")
    assert code == 0, out
    assert out["worktree"].endswith(f"fold-{PR}")
    assert Path(out["outdir"]).name.startswith("fold-")
    assert out["pre_fold_sha"] == env["head"]
    assert "mode" not in out
    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=out["worktree"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch == "feat"


def _rev(repo, ref="HEAD"):
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=repo, capture_output=True, text=True
    ).stdout.strip()


def _republish(env, rewrite=None, extra=None):
    """Move main on, rebase the PR branch onto it (optionally rewriting or adding a commit), publish the new head."""
    seed = env["seed"]
    (seed / "b.py").write_text("y = 1\n")
    commit_all(seed, "main moves")
    _git(seed, "checkout", "-q", "feat")
    _git(seed, "rebase", "-q", "main")
    if rewrite:
        (seed / "a.py").write_text(rewrite)
        _git(seed, "commit", "-q", "-a", "--amend", "-m", "change")
    if extra:
        (seed / "c.py").write_text(extra)
        commit_all(seed, "more")
    head = _rev(seed)
    _git(seed, "checkout", "-q", "main")
    _git(seed, "push", "-q", "-f", str(env["origin"]), "main", "feat")
    _git(env["origin"], "update-ref", f"refs/pull/{PR}/head", head)
    view = json.loads(env["view"].read_text())
    view["headRefOid"] = head
    env["view"].write_text(json.dumps(view))
    return head


def _completed_first_round(env):
    assert setup(env)[0] == 0
    subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "ledger.py"),
            "round-end",
            "--repo",
            "o/r",
            "--pr",
            str(PR),
            "--dir",
            str(env["base"]),
            "--sha",
            env["head"],
            "--complete",
        ],
        check=True,
        capture_output=True,
    )


def test_a_pure_rebase_reanchors_instead_of_rehunting(env):
    """Every commit's patch survived the rebase, so no layer needs to run again."""
    _completed_first_round(env)
    _republish(env)
    code, out = setup(env)
    assert code == 0, out
    assert (
        out["mode"],
        out["delta_since"],
        out["delta_commits"],
        out["revisit_files"],
    ) == ("rebase", env["head"], [], [])


def test_a_rebase_plus_new_commit_hunts_only_the_new_commit(env):
    """Reused patches keep their findings; only the commit with a new patch is hunted."""
    _completed_first_round(env)
    head = _republish(env, extra="z = 3\n")
    code, out = setup(env)
    assert code == 0, out
    assert (out["mode"], out["delta_commits"], out["revisit_files"]) == (
        "delta",
        [head],
        [],
    )


def test_a_rewritten_commit_after_rebase_is_delta_hunted(env):
    """An amended patch is new work even though the branch was also rebased."""
    _completed_first_round(env)
    head = _republish(env, rewrite="x = 3\n")
    code, out = setup(env)
    assert code == 0, out
    assert (out["mode"], out["delta_commits"], out["revisit_files"]) == (
        "delta",
        [head],
        ["a.py"],
    )


def test_a_dropped_commit_plus_a_new_one_revisits_the_dropped_files(env):
    """Code lost with a dropped commit is re-read even when the push also adds work."""
    _completed_first_round(env)
    seed = env["seed"]
    (seed / "b.py").write_text("y = 1\n")
    commit_all(seed, "main moves")
    _git(seed, "checkout", "-q", "-B", "feat", "main")
    (seed / "c.py").write_text("z = 3\n")
    commit_all(seed, "more")
    head = _rev(seed)
    _git(seed, "checkout", "-q", "main")
    _git(seed, "push", "-q", "-f", str(env["origin"]), "main", "feat")
    _git(env["origin"], "update-ref", f"refs/pull/{PR}/head", head)
    view = json.loads(env["view"].read_text())
    view["headRefOid"] = head
    env["view"].write_text(json.dumps(view))
    code, out = setup(env)
    assert code == 0, out
    assert (out["mode"], out["delta_commits"], out["revisit_files"]) == (
        "delta",
        [head],
        ["a.py"],
    )


def test_a_dropped_commit_forces_a_full_rehunt(env):
    """Removed work can strand findings the delta would never look at again."""
    _completed_first_round(env)
    (env["seed"] / "b.py").write_text("y = 1\n")
    commit_all(env["seed"], "main moves")
    _git(env["seed"], "checkout", "-q", "-B", "feat", "main")
    head = _rev(env["seed"])
    _git(env["seed"], "checkout", "-q", "main")
    _git(env["seed"], "push", "-q", "-f", str(env["origin"]), "main", "feat")
    _git(env["origin"], "update-ref", f"refs/pull/{PR}/head", head)
    view = json.loads(env["view"].read_text())
    view["headRefOid"] = head
    env["view"].write_text(json.dumps(view))
    code, out = setup(env)
    assert code == 0, out
    assert (out["mode"], out["delta_since"], out["delta_commits"]) == (
        "full",
        None,
        None,
    )


def _publish(env, head):
    _git(env["origin"], "update-ref", f"refs/pull/{PR}/head", head)
    view = json.loads(env["view"].read_text())
    view["headRefOid"] = head
    env["view"].write_text(json.dumps(view))


def _force_push_amended_feat(env):
    seed = env["seed"]
    _git(seed, "checkout", "-q", "feat")
    (seed / "a.py").write_text("x = 9\n")
    _git(seed, "commit", "-q", "-a", "--amend", "-m", "change")
    head = _rev(seed)
    _git(seed, "checkout", "-q", "main")
    _git(seed, "push", "-q", "-f", str(env["origin"]), "feat")
    _publish(env, head)
    return head


def test_fold_after_a_force_push_resets_to_the_new_head(env):
    """A clean fold worktree whose commits were all pushed follows a rewritten branch instead of failing the fast-forward."""
    assert setup(env, "--fold")[0] == 0
    head = _force_push_amended_feat(env)
    code, out = setup(env, "--fold")
    assert code == 0, out
    assert out["pre_fold_sha"] == head


@pytest.mark.parametrize("message", ["local only", "sync with origin/feat"])
def test_fold_keeps_unpushed_commits_and_releases_its_lease(env, message):
    """A local commit origin never had is not discarded, whatever its message says, and the failed setup leaves no lease behind."""
    _, out = setup(env, "--fold")
    worktree, lease = Path(out["worktree"]), Path(out["lease"])
    (worktree / "d.py").write_text("d = 1\n")
    commit_all(worktree, message)
    local = _rev(worktree)
    _force_push_amended_feat(env)
    code, out = setup(env, "--fold")
    assert code == 1
    assert "never had" in out["error"]
    assert _rev(worktree) == local
    assert not lease.exists()
    assert setup(env, "--fold", tag="b")[0] == 1


def test_merging_main_into_the_pr_reanchors_instead_of_reviewing_main(env):
    """A merge from the base keeps the old head as an ancestor, but main's commits are not the PR's work."""
    _completed_first_round(env)
    seed = env["seed"]
    _git(seed, "checkout", "-q", "main")
    for name in ("m1.py", "m2.py"):
        (seed / name).write_text("m = 1\n")
        commit_all(seed, name)
    _git(seed, "checkout", "-q", "feat")
    _git(seed, "merge", "-q", "--no-edit", "main")
    head = _rev(seed)
    _git(seed, "checkout", "-q", "main")
    _git(seed, "push", "-q", str(env["origin"]), "main", "feat")
    _publish(env, head)
    code, out = setup(env)
    assert code == 0, out
    assert (out["mode"], out["delta_commits"], out["revisit_files"]) == (
        "rebase",
        [],
        [],
    )


def test_fresh_round_on_an_unchanged_head_hunts_in_full(env):
    """Re-reviewing a head that already had a complete round never narrows to an empty delta."""
    _completed_first_round(env)
    code, out = setup(env)
    assert code == 0, out
    assert (out["mode"], out["delta_since"]) == ("full", None)


def test_fold_on_a_branch_checked_out_elsewhere_fails_with_json(env):
    """A failed worktree add reports the git error as JSON instead of crashing on the missing worktree."""
    _git(env["checkout"], "checkout", "-q", "feat")
    code, out = setup(env, "--fold")
    assert code == 1
    assert out["ok"] is False
    assert "worktree add" in out["error"]


def test_a_hand_resolved_merge_from_main_is_delta_hunted(env):
    """A conflict resolution inside a merge from the base is new code, so the merge and its file are reviewed."""
    _completed_first_round(env)
    seed = env["seed"]
    (seed / "a.py").write_text("x = 5\n")
    (seed / "m.py").write_text("m = 1\n")
    commit_all(seed, "main moves")
    _git(seed, "checkout", "-q", "feat")
    subprocess.run(["git", "merge", "-q", "main"], cwd=seed, capture_output=True)
    (seed / "a.py").write_text("x = 7\n")
    commit_all(seed, "merge main")
    head = _rev(seed)
    _git(seed, "checkout", "-q", "main")
    _git(seed, "push", "-q", str(env["origin"]), "main", "feat")
    _publish(env, head)
    code, out = setup(env)
    assert code == 0, out
    assert (out["mode"], out["delta_commits"], out["revisit_files"]) == (
        "delta",
        [head],
        ["a.py"],
    )
