import json
import shutil
import socket
import subprocess

import pytest

from .conftest import SCRIPTS, commit_all, posix_only
from .test_phase0 import load

RULES = SCRIPTS / "semgrep-rules"


def _semgrep_cmd():
    if shutil.which("semgrep"):
        return ["semgrep"]
    if shutil.which("uvx"):
        return ["uvx", "semgrep"]
    return None


@pytest.mark.skipif(_semgrep_cmd() is None, reason="semgrep unavailable")
def test_seed_rules_fire_on_bait(tmp_path):
    bait = tmp_path / "run_bait.py"
    bait.write_text(
        "def main():\n"
        "    cfg = get_config()\n"
        "    try:\n"
        "        enforce_guard(cfg)\n"
        "    except Exception:\n"
        "        pass\n"
        "    try:\n"
        "        mask_output(cfg)\n"
        "    except Exception as e:\n"
        "        log(e)\n"
        "    handlers.get(tag)(cfg)\n"
    )
    r = subprocess.run(
        [*_semgrep_cmd(), "--config", str(RULES), "--json", str(bait)],
        capture_output=True,
        encoding="utf-8",
        timeout=300,
    )
    assert r.stdout.count('"check_id"') >= 4


@pytest.mark.skipif(_semgrep_cmd() is None, reason="semgrep unavailable")
def test_broad_except_rule_ignores_reraise_but_still_catches_swallow(tmp_path):
    bait = tmp_path / "reraise_bait.py"
    bait.write_text(
        "def swallow_bare():\n"
        "    try:\n"
        "        enforce_guard(cfg)\n"
        "    except Exception:\n"
        "        pass\n"
        "\n"
        "def swallow_as():\n"
        "    try:\n"
        "        enforce_guard(cfg)\n"
        "    except Exception as e:\n"
        "        log(e)\n"
        "\n"
        "def swallow_base():\n"
        "    try:\n"
        "        enforce_guard(cfg)\n"
        "    except BaseException:\n"
        "        pass\n"
        "\n"
        "def reraise_bare():\n"
        "    try:\n"
        "        enforce_guard(cfg)\n"
        "    except Exception:\n"
        "        log('failed')\n"
        "        raise\n"
        "\n"
        "def reraise_as():\n"
        "    try:\n"
        "        enforce_guard(cfg)\n"
        "    except Exception as e:\n"
        "        log(e)\n"
        "        raise\n"
        "\n"
        "def reraise_base():\n"
        "    try:\n"
        "        enforce_guard(cfg)\n"
        "    except BaseException:\n"
        "        log('failed')\n"
        "        raise\n"
    )
    r = subprocess.run(
        [
            *_semgrep_cmd(),
            "--config",
            str(RULES / "broad-except-around-guard.yaml"),
            "--json",
            str(bait),
        ],
        capture_output=True,
        encoding="utf-8",
        timeout=300,
    )
    assert r.stdout.count('"check_id"') == 3
    for lineno in (2, 8, 14):
        assert f'"line":{lineno},' in r.stdout
    for lineno in (20, 27, 34):
        assert f'"line":{lineno},' not in r.stdout


@pytest.mark.skipif(_semgrep_cmd() is None, reason="semgrep unavailable")
def test_broad_except_rule_ignores_fail_closed_handlers_and_unrelated_names(
    tmp_path,
):
    """A success check that fails closed, or a call merely containing a guard word, is not flagged; a fail-open False is."""
    bait = tmp_path / "fail_closed_bait.py"
    bait.write_text(
        "def fail_closed():\n"
        "    try:\n"
        "        verify(sig)\n"
        "        return True\n"
        "    except Exception:\n"
        "        return False\n"
        "\n"
        "def fail_closed_as():\n"
        "    try:\n"
        "        token.verify(sig)\n"
        "        return True\n"
        "    except Exception as e:\n"
        "        log(e)\n"
        "        return False\n"
        "\n"
        "def unrelated():\n"
        "    try:\n"
        "        draw_bitmask(img)\n"
        "    except Exception:\n"
        "        pass\n"
        "\n"
        "def swallowed_method():\n"
        "    try:\n"
        "        self.redact_fields(doc)\n"
        "    except Exception:\n"
        "        pass\n"
        "\n"
        "def is_blocked(req):\n"
        "    try:\n"
        "        return guard.check(req)\n"
        "    except Exception:\n"
        "        return False\n"
    )
    r = subprocess.run(
        [
            *_semgrep_cmd(),
            "--config",
            str(RULES / "broad-except-around-guard.yaml"),
            "--json",
            str(bait),
        ],
        capture_output=True,
        encoding="utf-8",
        timeout=300,
    )
    assert r.stdout.count('"check_id"') == 2
    assert '"line":23,' in r.stdout
    assert '"line":29,' in r.stdout


def test_check_module_contract(fixture_repo, monkeypatch):
    mod = load("g_semgrep", "checks/g_semgrep.py")
    monkeypatch.setattr(mod, "COMMUNITY_TIMEOUT_S", 5)
    monkeypatch.setattr(mod, "LOCAL_TIMEOUT_S", 10)
    out = mod.run(str(fixture_repo), "origin/main")
    assert out.startswith(("OK:", "SKIPPED:"))


@posix_only
def test_semgrep_errors_and_skips_are_not_reported_clean(
    fixture_repo, tmp_path, monkeypatch
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git_shim = bin_dir / "git"
    git_shim.write_text("#!/bin/sh\necho pkg/mod.py\n")
    git_shim.chmod(0o755)
    sh_real = shutil.which("sh")
    if sh_real:
        (bin_dir / "sh").symlink_to(sh_real)
    fake_semgrep = bin_dir / "semgrep"
    payload = (
        '{"results": [], "errors": [{"message": "could not parse pkg/mod.py"}], '
        '"paths": {"scanned": ["pkg/other.py"], "skipped": [{"path": "pkg/mod.py"}]}}'
    )
    fake_semgrep.write_text(f"#!/bin/sh\necho '{payload}'\nexit 0\n")
    fake_semgrep.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    out = load("g_semgrep", "checks/g_semgrep.py").run(str(fixture_repo), "origin/main")
    assert not out.startswith("OK:")
    assert "could not parse pkg/mod.py" in out
    assert "scanned 1 file" in out and "skipped 1" in out


@posix_only
def test_community_pack_ids_are_passed_to_semgrep(fixture_repo, tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git_shim = bin_dir / "git"
    git_shim.write_text("#!/bin/sh\necho pkg/mod.py\n")
    git_shim.chmod(0o755)
    sh_real = shutil.which("sh")
    if sh_real:
        (bin_dir / "sh").symlink_to(sh_real)
    argv_log = tmp_path / "argv.log"
    fake_semgrep = bin_dir / "semgrep"
    fake_semgrep.write_text(
        f'#!/bin/sh\necho "$@" > {argv_log}\n'
        'echo \'{"results": [], "errors": [], "paths": {"scanned": ["pkg/mod.py"]}}\'\n'
        "exit 0\n"
    )
    fake_semgrep.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    out = load("g_semgrep", "checks/g_semgrep.py").run(str(fixture_repo), "origin/main")
    assert out.startswith("OK:")
    argv = argv_log.read_text()
    assert "p/python" in argv
    assert "p/security-audit" in argv
    assert str(RULES) in argv


def test_registry_timeout_falls_back_to_local_with_honest_note(
    fixture_repo, monkeypatch
):
    mod = load("g_semgrep", "checks/g_semgrep.py")
    calls = []

    class FakeD:
        def changed_py_files(self, worktree, base):
            return ["pkg/mod.py"]

        def added_lines_by_path(self, worktree, base, paths):
            return {q: set() for q in paths}

        def run_cmd(self, args, cwd, timeout):
            calls.append(args)
            if "p/python" in args:
                return -1, "TIMEOUT"
            return (
                0,
                '{"results": [], "errors": [], "paths": {"scanned": ["pkg/mod.py"]}}',
            )

    monkeypatch.setattr(mod, "_diff", FakeD())
    out = mod.run(str(fixture_repo), "origin/main")
    assert out.startswith("OK:")
    assert "local rulebook only" in out
    assert "community packs unavailable" in out
    assert "timed out" in out
    assert len([c for c in calls if c[0] == "semgrep"]) == 2


def test_registry_fetch_error_detected_and_falls_back(fixture_repo, monkeypatch):
    mod = load("g_semgrep", "checks/g_semgrep.py")
    calls = []

    class FakeD:
        def changed_py_files(self, worktree, base):
            return ["pkg/mod.py"]

        def added_lines_by_path(self, worktree, base, paths):
            return {q: set() for q in paths}

        def run_cmd(self, args, cwd, timeout):
            calls.append(args)
            if "p/python" in args:
                payload = {
                    "results": [],
                    "errors": [
                        {
                            "message": "Failed to download configuration from "
                            "https://semgrep.dev/c/p/python HTTP 503."
                        }
                    ],
                    "paths": {"scanned": []},
                }
                return 7, json.dumps(payload)
            return (
                0,
                '{"results": [], "errors": [], "paths": {"scanned": ["pkg/mod.py"]}}',
            )

    monkeypatch.setattr(mod, "_diff", FakeD())
    out = mod.run(str(fixture_repo), "origin/main")
    assert out.startswith("OK:")
    assert "local rulebook only" in out
    assert "HTTP 503" in out
    assert len([c for c in calls if c[0] == "semgrep"]) == 2


def test_non_registry_config_error_does_not_trigger_fallback(fixture_repo, monkeypatch):
    mod = load("g_semgrep", "checks/g_semgrep.py")
    calls = []

    class FakeD:
        def changed_py_files(self, worktree, base):
            return ["pkg/mod.py"]

        def added_lines_by_path(self, worktree, base, paths):
            return {q: set() for q in paths}

        def run_cmd(self, args, cwd, timeout):
            calls.append(args)
            payload = {
                "results": [],
                "errors": [{"message": "unable to find a config; path does not exist"}],
                "paths": {"scanned": []},
            }
            return 7, json.dumps(payload)

    monkeypatch.setattr(mod, "_diff", FakeD())
    out = mod.run(str(fixture_repo), "origin/main")
    assert out.startswith("SKIPPED: semgrep exited 7 with no results")
    assert len([c for c in calls if c[0] == "semgrep"]) == 1


def test_findings_attribute_community_vs_local(fixture_repo, monkeypatch):
    mod = load("g_semgrep", "checks/g_semgrep.py")

    class FakeD:
        def changed_py_files(self, worktree, base):
            return ["pkg/mod.py"]

        def added_lines_by_path(self, worktree, base, paths):
            return {q: set() for q in paths}

        def run_cmd(self, args, cwd, timeout):
            payload = {
                "results": [
                    {
                        "path": "pkg/mod.py",
                        "start": {"line": 3},
                        "check_id": "python.lang.security.audit.subprocess-shell-true.subprocess-shell-true",
                        "extra": {"message": "community hit"},
                    },
                    {
                        "path": "pkg/mod.py",
                        "start": {"line": 9},
                        "check_id": "broad-except-around-guard",
                        "extra": {"message": "local hit"},
                    },
                ],
                "errors": [],
                "paths": {"scanned": ["pkg/mod.py"]},
            }
            return 1, json.dumps(payload)

    monkeypatch.setattr(mod, "_diff", FakeD())
    out = mod.run(str(fixture_repo), "origin/main")
    assert out.startswith("FINDINGS:")
    assert (
        "[community:python.lang.security.audit.subprocess-shell-true.subprocess-shell-true]"
        in out
    )
    assert "[local:broad-except-around-guard]" in out


def test_local_finding_with_path_prefixed_check_id_is_attributed_correctly(
    fixture_repo, monkeypatch
):
    mod = load("g_semgrep", "checks/g_semgrep.py")

    class FakeD:
        def changed_py_files(self, worktree, base):
            return ["pkg/mod.py"]

        def added_lines_by_path(self, worktree, base, paths):
            return {q: set() for q in paths}

        def run_cmd(self, args, cwd, timeout):
            payload = {
                "results": [
                    {
                        "path": "pkg/mod.py",
                        "start": {"line": 4},
                        "check_id": (
                            "home.dev.src.skills-worktrees."
                            "review-skill-v2.plugin.skills.review.scripts."
                            "semgrep-rules.broad-except-around-guard"
                        ),
                        "extra": {"message": "broad except swallows a guard"},
                    }
                ],
                "errors": [],
                "paths": {"scanned": ["pkg/mod.py"]},
            }
            return 1, json.dumps(payload)

    monkeypatch.setattr(mod, "_diff", FakeD())
    out = mod.run(str(fixture_repo), "origin/main")
    assert out.startswith("FINDINGS:")
    assert "[local:broad-except-around-guard]" in out
    bracket = out[out.index("[") : out.index("]") + 1]
    assert "community" not in bracket
    assert "/" not in bracket
    assert "plugin/skills" not in bracket
    assert "home.dev" not in bracket
    assert "semgrep-rules" not in bracket


def _network_reachable():
    try:
        socket.create_connection(("semgrep.dev", 443), timeout=3).close()
        return True
    except OSError:
        return False


@pytest.mark.skipif(
    _semgrep_cmd() is None or not _network_reachable(),
    reason="semgrep or network unavailable",
)
def test_community_pack_catches_what_local_rules_miss(fixture_repo):
    (fixture_repo / "pkg" / "mod.py").write_text(
        "import subprocess\n\n\ndef run(cmd):\n    subprocess.call(cmd, shell=True)\n"
    )
    commit_all(fixture_repo)
    out = load("g_semgrep", "checks/g_semgrep.py").run(str(fixture_repo), "origin/main")
    assert out.startswith("FINDINGS:")
    assert "community:" in out


@pytest.mark.skipif(_semgrep_cmd() is None, reason="semgrep unavailable")
def test_absolute_promise_rule_fires_only_on_the_promising_function(tmp_path):
    """An absolute docstring promise beside a raise; the honest twin must stay quiet."""
    bait = tmp_path / "promise_bait.py"
    bait.write_text(
        "def probe(x):\n"
        '    """Report readiness. This never raises, so boot cannot be aborted."""\n'
        "    if not x:\n"
        '        raise ValueError("no")\n'
        "    return True\n"
        "\n\n"
        "def honest(x):\n"
        '    """Report readiness, raising on a bad argument."""\n'
        "    if not x:\n"
        '        raise ValueError("no")\n'
        "    return True\n"
    )
    out = subprocess.run(
        [
            *_semgrep_cmd(),
            "--config",
            str(RULES / "absolute-promise-that-can-raise.yaml"),
            "--json",
            "--quiet",
            str(bait),
        ],
        capture_output=True,
        encoding="utf-8",
        timeout=180,
    )
    results = json.loads(out.stdout)["results"]
    assert len(results) == 1
    assert results[0]["start"]["line"] == 1


def _fake_d_with_hits(mod, hits):
    """A fake that answers git-diff and semgrep calls differently.

    The older fakes in this file are args-blind, which is fine when nothing else
    shells out; provenance grouping needs a real `git diff -U0`, so this one
    dispatches on the command.
    """

    real = load("_diff_shared", "checks/_diff.py")

    class FakeD:
        def changed_py_files(self, worktree, base):
            return ["pkg/mod.py"]

        def added_lines_by_path(self, worktree, base, paths):
            return real.added_lines_by_path(worktree, base, paths)

        def run_cmd(self, args, cwd, timeout):
            payload = {
                "results": hits,
                "errors": [],
                "paths": {"scanned": ["pkg/mod.py"]},
            }
            return 1, json.dumps(payload)

    return FakeD()


def _hit(line, msg):
    return {
        "path": "pkg/mod.py",
        "start": {"line": line},
        "check_id": "broad-except-around-guard",
        "extra": {"message": msg},
    }


def test_matches_are_split_by_whether_the_diff_added_the_line(
    fixture_repo, monkeypatch
):
    """A pre-existing match in a touched file is triage, not a finding."""
    mod = load("g_semgrep", "checks/g_semgrep.py")
    monkeypatch.setattr(
        mod,
        "_diff",
        _fake_d_with_hits(mod, [_hit(1, "old hit"), _hit(5, "new hit")]),
    )
    out = mod.run(str(fixture_repo), "origin/main")
    added = out.split("### added by this diff")[1].split("### not on")[0]
    pre = out.split("### not on a line this diff added")[1]
    assert "new hit" in added and "old hit" not in added
    assert "old hit" in pre and "new hit" not in pre


def test_a_pre_existing_only_match_is_not_presented_as_a_new_finding(
    fixture_repo, monkeypatch
):
    """Touching a file must not re-report its untouched matches as this diff's."""
    mod = load("g_semgrep", "checks/g_semgrep.py")
    monkeypatch.setattr(mod, "_diff", _fake_d_with_hits(mod, [_hit(1, "old hit")]))
    out = mod.run(str(fixture_repo), "origin/main")
    assert "not on a line this diff added" in out
    assert "added by this diff" not in out


def test_a_pre_existing_match_is_never_dropped(fixture_repo, monkeypatch):
    """Filtering would hide a call broken by a guard that moved; partition instead."""
    mod = load("g_semgrep", "checks/g_semgrep.py")
    monkeypatch.setattr(mod, "_diff", _fake_d_with_hits(mod, [_hit(1, "old hit")]))
    out = mod.run(str(fixture_repo), "origin/main")
    assert "old hit" in out
    assert "guard that moved" in out


def test_a_match_is_new_when_any_line_of_its_span_was_added(fixture_repo, monkeypatch):
    """semgrep anchors try/except on `try:`, so a rewritten handler must still count."""
    mod = load("g_semgrep", "checks/g_semgrep.py")
    spanning = dict(_hit(1, "span hit"), end={"line": 5})
    monkeypatch.setattr(mod, "_diff", _fake_d_with_hits(mod, [spanning]))
    out = mod.run(str(fixture_repo), "origin/main")
    assert "added by this diff" in out
    assert "not on a line this diff added" not in out


def test_a_pure_rename_contributes_no_added_lines(tmp_path):
    """Scoping git diff to one pathspec defeats -M, so a rename read as all-new."""
    d = load("_diff_rename", "checks/_diff.py")
    root = tmp_path / "r"
    root.mkdir()
    g = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(
        [*g, "init", "-b", "main"], cwd=root, check=True, capture_output=True
    )
    (root / "a.py").write_text("def f():\n    return 1\n\n\ndef g():\n    return 2\n")
    subprocess.run([*g, "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run([*g, "commit", "-m", "b"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        [*g, "update-ref", "refs/remotes/origin/main", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [*g, "mv", "a.py", "b.py"], cwd=root, check=True, capture_output=True
    )
    subprocess.run([*g, "commit", "-m", "c"], cwd=root, check=True, capture_output=True)
    assert d.added_lines_by_path(str(root), "origin/main", ["b.py"]) == {"b.py": set()}


def test_a_file_the_diff_never_named_is_unknown_not_empty(tmp_path):
    """Empty means 'gained no lines'; unknown means 'could not tell' - not the same."""
    d = load("_diff_unknown", "checks/_diff.py")
    root = tmp_path / "r"
    root.mkdir()
    g = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(
        [*g, "init", "-b", "main"], cwd=root, check=True, capture_output=True
    )
    (root / "a.py").write_text("x = 1\n")
    subprocess.run([*g, "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run([*g, "commit", "-m", "b"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        [*g, "update-ref", "refs/remotes/origin/main", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    assert d.added_lines_by_path(str(root), "origin/main", ["ghost.py"]) == {
        "ghost.py": None
    }


def test_each_group_is_capped_separately(fixture_repo, monkeypatch):
    """A shared cap spends itself on whichever group sorts first and drops the other."""
    mod = load("g_semgrep", "checks/g_semgrep.py")
    many = [_hit(1, f"old {i}") for i in range(45)] + [_hit(5, "the new one")]
    monkeypatch.setattr(mod, "_diff", _fake_d_with_hits(mod, many))
    out = mod.run(str(fixture_repo), "origin/main")
    assert "the new one" in out, (
        "the added match must survive a flood of pre-existing ones"
    )
    assert "more not shown" in out


def test_unknown_provenance_gets_its_own_group(fixture_repo, monkeypatch):
    """Defaulting unknown into the skim group hides a real finding behind a git quirk."""
    mod = load("g_semgrep", "checks/g_semgrep.py")

    class FakeD:
        def changed_py_files(self, worktree, base):
            return ["pkg/mod.py"]

        def added_lines_by_path(self, worktree, base, paths):
            return {q: None for q in paths}

        def run_cmd(self, args, cwd, timeout):
            return 1, json.dumps(
                {
                    "results": [_hit(3, "mystery")],
                    "errors": [],
                    "paths": {"scanned": []},
                }
            )

    monkeypatch.setattr(mod, "_diff", FakeD())
    out = mod.run(str(fixture_repo), "origin/main")
    assert "### provenance unknown" in out
    assert "Read them as if new" in out

    assert "pre-existing in a file this diff touched" not in out


def _rule_hits(rule, source, tmp_path):
    bait = tmp_path / "bait.py"
    bait.write_text(source)
    out = subprocess.run(
        [
            *_semgrep_cmd(),
            "--config",
            str(RULES / rule),
            "--json",
            "--quiet",
            str(bait),
        ],
        capture_output=True,
        encoding="utf-8",
        timeout=180,
    )
    return sorted(r["start"]["line"] for r in json.loads(out.stdout)["results"])


@pytest.mark.skipif(_semgrep_cmd() is None, reason="semgrep unavailable")
def test_swallowed_exception_fires_only_on_the_silent_handler(tmp_path):
    """A handler that logs or re-raises is not a swallow; a bare except is ruff's E722."""
    hits = _rule_hits(
        "swallowed-exception.yaml",
        "import logging\n\nlog = logging.getLogger(__name__)\n\n\n"
        "def swallows(c):\n    try:\n        risky(c)\n    except Exception:\n        pass\n\n\n"
        "def logs_it(c):\n    try:\n        risky(c)\n    except Exception:\n        log.warning('x')\n\n\n"
        "def reraises(c):\n    try:\n        risky(c)\n    except Exception:\n        raise\n\n\n"
        "def bare(c):\n    try:\n        risky(c)\n    except:\n        pass\n",
        tmp_path,
    )
    assert hits == [7], "only the silent handler; bare except belongs to ruff E722"


@pytest.mark.skipif(_semgrep_cmd() is None, reason="semgrep unavailable")
def test_swallowed_exception_also_catches_a_bare_none_return(tmp_path):
    """`return None` discards the error exactly as `pass` does."""
    hits = _rule_hits(
        "swallowed-exception.yaml",
        "def f(c):\n    try:\n        risky(c)\n    except Exception:\n        return None\n",
        tmp_path,
    )
    assert hits == [2]


@pytest.mark.skipif(_semgrep_cmd() is None, reason="semgrep unavailable")
def test_sensitive_value_in_log_catches_logging_and_print(tmp_path):
    """print() must be covered too, and a word-boundary keeps token_count clean."""
    hits = _rule_hits(
        "sensitive-value-in-log.yaml",
        "import logging\n\nlog = logging.getLogger(__name__)\n\n\n"
        "def leaks(api_key, host):\n    log.info('with %s', api_key)\n\n\n"
        "def leaks_print(password):\n    print('pw', password)\n\n\n"
        "def safe(host, token_count):\n    log.info('to %s', host)\n"
        "    log.info('retry %s', token_count)\n",
        tmp_path,
    )
    assert hits == [7, 11], "api_key and password only; host and token_count are clean"


@pytest.mark.skipif(_semgrep_cmd() is None, reason="semgrep unavailable")
def test_sensitive_value_rule_requires_a_word_boundary(tmp_path):
    """Without \\b, `token_count` and `mytoken` both match and the rule is noise."""
    hits = _rule_hits(
        "sensitive-value-in-log.yaml",
        "import logging\n\nlog = logging.getLogger(__name__)\n\n\n"
        "def f(token_count, mytoken, api_key):\n"
        "    log.info('a %s', token_count)\n"
        "    log.info('b %s', mytoken)\n"
        "    log.info('c %s', api_key)\n",
        tmp_path,
    )
    assert hits == [9], "only the real api_key; token_count and mytoken are not secrets"


@pytest.mark.skipif(_semgrep_cmd() is None, reason="semgrep unavailable")
def test_every_local_rule_file_parses():
    """An unparseable rule makes semgrep print results:[] - clean, and checking nothing."""
    out = subprocess.run(
        [*_semgrep_cmd(), "--config", str(RULES), "--validate", "--json"],
        capture_output=True,
        encoding="utf-8",
        timeout=180,
    )
    assert out.returncode == 0, f"a rule file is invalid:\n{out.stderr[:600]}"


@pytest.mark.skipif(_semgrep_cmd() is None, reason="semgrep unavailable")
def test_sensitive_value_matches_context_prefixed_secret_names(tmp_path):
    """The common naming style: `\\b` cannot match inside user_password at all."""
    hits = _rule_hits(
        "sensitive-value-in-log.yaml",
        "import logging\n\nlog = logging.getLogger(__name__)\n\n\n"
        "def f(user_password, oauth_token, db_secret, aws_api_key):\n"
        "    log.info('1 %s', user_password)\n"
        "    log.info('2 %s', oauth_token)\n"
        "    log.info('3 %s', db_secret)\n"
        "    log.info('4 %s', aws_api_key)\n",
        tmp_path,
    )
    assert hits == [7, 8, 9, 10], "all four context-prefixed secrets must match"


@pytest.mark.skipif(_semgrep_cmd() is None, reason="semgrep unavailable")
def test_sensitive_value_still_rejects_keyword_as_prefix(tmp_path):
    """token_count counts secrets; tokenizer is not one. Both share a keyword."""
    hits = _rule_hits(
        "sensitive-value-in-log.yaml",
        "import logging\n\nlog = logging.getLogger(__name__)\n\n\n"
        "def f(token_count, mytoken, secretary_name, tokenizer, host):\n"
        "    log.info('1 %s', token_count)\n"
        "    log.info('2 %s', mytoken)\n"
        "    log.info('3 %s', secretary_name)\n"
        "    log.info('4 %s', tokenizer)\n"
        "    log.info('5 %s', host)\n",
        tmp_path,
    )
    assert hits == [], "keyword-as-prefix names are not secrets"


@pytest.mark.skipif(_semgrep_cmd() is None, reason="semgrep unavailable")
def test_sensitive_value_catches_the_extra_keyword_form(tmp_path):
    """The measured real-world miss: logger.warning(..., extra={"token": token})."""
    hits = _rule_hits(
        "sensitive-value-in-log.yaml",
        "import logging\n\nlog = logging.getLogger(__name__)\n\n\n"
        "def f(token):\n    log.warning('not found', extra={'token': token})\n",
        tmp_path,
    )
    assert hits == [7]


@pytest.mark.skipif(_semgrep_cmd() is None, reason="semgrep unavailable")
def test_swallowed_exception_covers_continue_and_bare_return(tmp_path):
    """`continue` is the corpus archetype; a bare `return` discards identically."""
    hits = _rule_hits(
        "swallowed-exception.yaml",
        "def a(c):\n    try:\n        risky(c)\n    except Exception:\n        pass\n\n\n"
        "def b(c):\n    for x in c:\n        try:\n            risky(x)\n"
        "        except Exception:\n            continue\n\n\n"
        "def d(c):\n    try:\n        risky(c)\n    except Exception:\n        return\n\n\n"
        "def e(c):\n    try:\n        risky(c)\n    except BaseException:\n        pass\n",
        tmp_path,
    )
    assert hits == [2, 10, 17, 24], "pass, continue, bare return, BaseException"


@pytest.mark.skipif(_semgrep_cmd() is None, reason="semgrep unavailable")
def test_swallowed_exception_message_survives_the_render_cap(tmp_path):
    """_render truncates at 120 chars, which silently cut the escape-hatch clause."""
    import yaml

    rule = yaml.safe_load((RULES / "swallowed-exception.yaml").read_text())
    msg = rule["rules"][0]["message"]
    assert len(msg) <= 120, f"message is {len(msg)} chars and would be truncated"
    assert "nosemgrep" in msg[:120]


def test_fallback_note_survives_when_there_are_hits():
    """The community-packs-unavailable note must be printed alongside findings, not only on a clean run."""
    mod = load("g_semgrep", "checks/g_semgrep.py")
    payload = {"results": [_hit(5, "new hit")], "errors": [], "paths": {}}
    out = mod._render(
        payload,
        1,
        ["pkg/mod.py"],
        "local rulebook only",
        set(),
        note="community packs unavailable (timed out)",
        added_by_path={"pkg/mod.py": {5}},
    )
    assert out.startswith("FINDINGS:")
    assert "NOTE: community packs unavailable (timed out)" in out


def _rule_lines(rule, tmp_path, source):
    bait = tmp_path / "bait.py"
    bait.write_text(source)
    out = subprocess.run(
        [
            *_semgrep_cmd(),
            "--config",
            str(RULES / rule),
            "--json",
            "--quiet",
            str(bait),
        ],
        capture_output=True,
        encoding="utf-8",
        timeout=180,
    )
    return sorted(r["start"]["line"] for r in json.loads(out.stdout)["results"])


@pytest.mark.skipif(_semgrep_cmd() is None, reason="semgrep unavailable")
def test_swallowed_exception_catches_the_named_form_too(tmp_path):
    """`except Exception as e: pass` swallows exactly like the unnamed form; a logged or narrow except does not."""
    lines = _rule_lines(
        "swallowed-exception.yaml",
        tmp_path,
        "def f():\n"
        "    try:\n"
        "        x()\n"
        "    except Exception as e:\n"
        "        pass\n"
        "    try:\n"
        "        x()\n"
        "    except BaseException:\n"
        "        return None\n"
        "    try:\n"
        "        x()\n"
        "    except Exception as e:\n"
        "        log(e)\n"
        "    try:\n"
        "        x()\n"
        "    except ValueError:\n"
        "        pass\n",
    )
    assert lines == [2, 6]


@pytest.mark.skipif(_semgrep_cmd() is None, reason="semgrep unavailable")
def test_sensitive_log_rule_sees_attributes_and_fstrings_but_not_page_tokens(tmp_path):
    """Attribute access and f-string interpolation of a secret are flagged; pagination tokens and counts are not.
    A `!r` conversion is a known blind spot that the rule message names."""
    lines = _rule_lines(
        "sensitive-value-in-log.yaml",
        tmp_path,
        'log.info(f"key {api_key}")\n'
        "log.info(self.api_key)\n"
        "log.info(access_token)\n"
        "log.info(next_page_token)\n"
        'log.info(f"count {token_count}")\n'
        'log.info("token refreshed")\n'
        'log.info(f"key {api_key!r}")\n',
    )
    assert lines == [1, 2, 3]


@pytest.mark.skipif(_semgrep_cmd() is None, reason="semgrep unavailable")
def test_broad_except_rule_ignores_translated_raise(tmp_path):
    """Raising a new exception, chained or not, surfaces the failure and is not a swallow."""
    lines = _rule_lines(
        "broad-except-around-guard.yaml",
        tmp_path,
        "def f():\n"
        "    try:\n"
        "        enforce_guard(cfg)\n"
        "    except Exception as e:\n"
        '        raise GuardError("x") from e\n'
        "    try:\n"
        "        enforce_guard(cfg)\n"
        "    except Exception:\n"
        '        raise GuardError("x")\n'
        "    try:\n"
        "        enforce_guard(cfg)\n"
        "    except BaseException as e:\n"
        "        log(e)\n",
    )
    assert lines == [10]


@pytest.mark.skipif(_semgrep_cmd() is None, reason="semgrep unavailable")
def test_runner_rule_covers_main_guard_modules_at_top_level(tmp_path):
    """Top-level and guard-block config calls in a __main__ module are flagged; helpers and try-wrapped calls are not."""
    lines = _rule_lines(
        "runner-call-outside-exit-guard.yaml",
        tmp_path,
        "CFG = load_config()\n"
        "\n"
        "def helper():\n"
        "    c = get_config()\n"
        "\n"
        "try:\n"
        "    d = get_config()\n"
        "except Exception:\n"
        "    sys.exit(2)\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    cfg = read_config()\n",
    )
    assert lines == [1, 12]
    guarded = (
        "def main():\n"
        "    cfg = load_config()\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    try:\n"
        "        main()\n"
        "    except Exception:\n"
        "        sys.exit(2)\n"
    )
    assert _rule_lines("runner-call-outside-exit-guard.yaml", tmp_path, guarded) == []
    unguarded = guarded.replace(
        "    try:\n        main()\n    except Exception:\n        sys.exit(2)\n",
        "    main()\n",
    )
    assert _rule_lines("runner-call-outside-exit-guard.yaml", tmp_path, unguarded) == [
        2
    ]
