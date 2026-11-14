import json
import shutil
import subprocess

import pytest

from .conftest import CLI_MANIFEST, _git, assert_parity, skip_shims_on_windows

needs_semgrep = pytest.mark.skipif(
    not shutil.which("semgrep"), reason="semgrep not on PATH"
)

REGISTRY_ERROR = {
    "message": "Failed to download configuration from https://semgrep.dev/c/p/python HTTP 503."
}
CLEAN = {"results": [], "errors": [], "paths": {"scanned": ["pkg/mod.py"]}}


def _hit(line, msg, check_id="broad-except-around-guard", path="pkg/mod.py", **kw):
    return {
        "path": path,
        "start": {"line": line},
        "check_id": check_id,
        "extra": {"message": msg},
        **kw,
    }


def _payload(hits=(), errors=(), scanned=("pkg/mod.py",), **paths):
    return {
        "results": list(hits),
        "errors": list(errors),
        "paths": {"scanned": list(scanned), **paths},
    }


def _emit(payload, code=0):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return f"cat <<'EOF'\n{text}\nEOF\nexit {code}\n"


def _split(community, local):
    return f'case "$*" in\n*p/python*)\n{community};;\nesac\n{local}'


def _bin_dir(tmp_path, monkeypatch, **tools):
    skip_shims_on_windows()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("git", "sh", "cat"):
        real = shutil.which(name)
        if real:
            (bin_dir / name).symlink_to(real)
    for name, body in tools.items():
        exe = bin_dir / name
        exe.write_text("#!/bin/sh\n" + body)
        exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    return bin_dir


SHIM_CASES = {
    "clean": {"semgrep": _emit(CLEAN)},
    "unavailable": {},
    "uvx_fallback": {"uvx": _emit(CLEAN)},
    "errors_and_skips_not_clean": {
        "semgrep": _emit(
            _payload(
                errors=[{"message": "could not parse pkg/mod.py"}],
                scanned=["pkg/other.py"],
                skipped=[{"path": "pkg/mod.py"}],
            )
        )
    },
    "error_without_message_and_non_str_message": {
        "semgrep": _emit(
            _payload(
                errors=[
                    {"code": 3, "level": "warn", "spans": [None, True]},
                    {"message": 7},
                ],
                skipped=[{"path": "x"}],
            )
        )
    },
    "long_error_messages": {
        "semgrep": _emit(_payload(errors=[{"message": "é" * 300} for _ in range(7)]))
    },
    "nonzero_exit_without_results": {
        "semgrep": _emit(_payload(errors=[{"message": "no config"}], scanned=[]), 7)
    },
    "findings_grouped_by_provenance": {
        "semgrep": _emit(
            _payload(
                hits=[
                    _hit(1, "old hit"),
                    _hit(5, "new hit"),
                    _hit(3, "spans into new", end={"line": 5}),
                    _hit(2, "unknown file", path="pkg/elsewhere.py"),
                    _hit(
                        4,
                        "community " + "x" * 200,
                        check_id="python.lang.security.audit.subprocess-shell-true",
                    ),
                    _hit(
                        6,
                        "prefixed local",
                        check_id="home.dev.semgrep-rules.swallowed-exception",
                    ),
                    _hit(6, "registry slash", check_id="r/python.some.rule"),
                ]
            ),
            1,
        )
    },
    "groups_capped_at_20": {
        "semgrep": _emit(
            _payload(
                hits=[_hit(1, f"old {n}") for n in range(25)]
                + [_hit(5, f"new {n}") for n in range(21)]
                + [_hit(1, f"u {n}", path="z.py") for n in range(20)]
            ),
            1,
        )
    },
    "noise_before_json": {"semgrep": "echo 'warning: x'\n" + _emit(CLEAN)},
    "hit_missing_key": {
        "semgrep": _emit(_payload(hits=[{"path": "pkg/mod.py", "start": {}}]), 1)
    },
    "registry_down_local_clean": {
        "semgrep": _split(
            _emit(_payload(errors=[REGISTRY_ERROR], scanned=[]), 7), _emit(CLEAN)
        )
    },
    "registry_down_local_findings": {
        "semgrep": _split(
            _emit(_payload(errors=[REGISTRY_ERROR, REGISTRY_ERROR], scanned=[]), 2),
            _emit(_payload(hits=[_hit(5, "new hit")]), 1),
        )
    },
    "registry_down_without_message_detail": {
        "semgrep": _split(
            _emit(
                _payload(errors=[{"message": "https://semgrep.dev/c/"}], scanned=[]), 7
            ),
            _emit(CLEAN),
        )
    },
    "community_unparseable_local_clean": {
        "semgrep": _split("echo 'boom'\nexit 2\n", _emit(CLEAN))
    },
    "both_unparseable": {"semgrep": "echo 'boom' 1>&2\nexit 2\n"},
    "killed_by_signal": {"semgrep": "kill -9 $$\n"},
}


@pytest.mark.parametrize("case", SHIM_CASES)
def test_rust_port_matches_python_with_shimmed_semgrep(
    case, helmsman_cli, fixture_repo, tmp_path, monkeypatch
):
    """The Rust check prints what the Python check prints for every semgrep outcome it branches on."""
    _bin_dir(tmp_path, monkeypatch, **SHIM_CASES[case])
    assert_parity(helmsman_cli, "g_semgrep", fixture_repo)


def test_rust_port_matches_python_on_git_failure(helmsman_cli, fixture_repo):
    assert_parity(helmsman_cli, "g_semgrep", fixture_repo, base="no-such-ref")


def test_rust_port_matches_python_without_python_changes(
    helmsman_cli, fixture_repo, tmp_path, monkeypatch
):
    (fixture_repo / "README.md").write_text("hi\n")
    _git(fixture_repo, "add", "-A")
    _git(fixture_repo, "commit", "-m", "docs")
    _bin_dir(tmp_path, monkeypatch, semgrep="echo SHOULD-NOT-RUN\nexit 1\n")
    assert_parity(helmsman_cli, "g_semgrep", fixture_repo, base="HEAD~1")


BAIT = (
    "def main(cfg):\n"
    "    try:\n"
    "        enforce_guard(cfg)\n"
    "    except Exception:\n"
    "        pass\n"
    "    handlers.get(tag)(cfg)\n"
)


@needs_semgrep
@pytest.mark.parametrize("bait", [BAIT, "def ok(x):\n    return x\n"])
def test_rust_port_matches_python_with_real_semgrep(bait, helmsman_cli, fixture_repo):
    (fixture_repo / "pkg" / "mod.py").write_text(bait)
    _git(fixture_repo, "add", "-A")
    _git(fixture_repo, "commit", "-m", "bait")
    assert_parity(helmsman_cli, "g_semgrep", fixture_repo)


@needs_semgrep
def test_rust_port_matches_python_on_own_history(helmsman_cli):
    base = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", "HEAD~1"],
        cwd=CLI_MANIFEST.parents[1],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not base:
        pytest.skip("shallow clone without enough history")
    assert_parity(helmsman_cli, "g_semgrep", CLI_MANIFEST.parents[1], base=base)
