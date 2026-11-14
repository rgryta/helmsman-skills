import hashlib
import io
import json
import sys
import tarfile
import tomllib
import zipfile

import pytest
import yaml

from .conftest import CLI_MANIFEST, SCRIPTS
from .test_cli import _fake_cli
from .test_phase0 import load

RELEASE_YML = SCRIPTS.parents[3] / ".github" / "workflows" / "release.yml"


def _version():
    return json.loads(
        (SCRIPTS.parents[2] / ".claude-plugin" / "plugin.json").read_text()
    )["version"]


def _fake_binary(version):
    return f"#!{sys.executable}\nprint('helmsman {version}')\n".encode()


def _archive(name, payload):
    buf = io.BytesIO()
    if name.endswith(".zip"):
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("helmsman.exe", payload)
    else:
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            info = tarfile.TarInfo("helmsman")
            info.size = len(payload)
            t.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def _release(tmp_path, fetch_cli, triple, payload, *, corrupt=False):
    version = _version()
    ext = ".zip" if "windows" in triple else ".tar.gz"
    name = f"helmsman-{version}-{triple}{ext}"
    data = _archive(name, payload)
    tag = tmp_path / "releases" / f"v{version}"
    tag.mkdir(parents=True)
    (tag / name).write_bytes(data + (b"x" if corrupt else b""))
    (tag / "SHA256SUMS").write_text(f"{hashlib.sha256(data).hexdigest()}  {name}\n")
    fetch_cli.RELEASES = (tmp_path / "releases").as_uri()
    return version


def _installed(tmp_path):
    return [p for p in (tmp_path / "home").rglob("*") if p.is_file()]


@pytest.fixture
def fetch_cli(tmp_path, monkeypatch):
    mod = load("fetch_cli", "fetch_cli.py")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(mod.platform, "machine", lambda: "x86_64")
    return mod


@pytest.mark.parametrize(
    ("system", "machine", "triple"),
    [
        ("Linux", "x86_64", "x86_64-unknown-linux-musl"),
        ("Linux", "amd64", "x86_64-unknown-linux-musl"),
        ("Linux", "aarch64", "aarch64-unknown-linux-musl"),
        ("Linux", "arm64", "aarch64-unknown-linux-musl"),
        ("Darwin", "x86_64", "x86_64-apple-darwin"),
        ("Darwin", "arm64", "aarch64-apple-darwin"),
        ("Windows", "AMD64", "x86_64-pc-windows-msvc"),
        ("Windows", "ARM64", "aarch64-pc-windows-msvc"),
        ("FreeBSD", "amd64", None),
        ("Linux", "riscv64", None),
    ],
)
def test_platform_maps_to_release_target(system, machine, triple):
    assert load("fetch_cli", "fetch_cli.py").target(system, machine) == triple


def test_release_workflow_builds_every_fetchable_target():
    """release.yml's build matrix and fetch_cli's target table are the same set."""
    fetch_cli = load("fetch_cli", "fetch_cli.py")
    jobs = yaml.safe_load(RELEASE_YML.read_text())["jobs"]
    built = {row["target"] for row in jobs["build"]["strategy"]["matrix"]["include"]}
    assert built == fetch_cli.TARGETS
    assert len(built) == 6


def test_cargo_version_matches_plugin_version():
    cargo = tomllib.loads(CLI_MANIFEST.read_text())["package"]["version"]
    assert cargo == _version()


@pytest.mark.parametrize(
    ("system", "triple", "exe"),
    [
        ("Linux", "x86_64-unknown-linux-musl", "helmsman"),
        ("Windows", "x86_64-pc-windows-msvc", "helmsman.exe"),
    ],
)
def test_installs_verified_binary_and_prints_path(
    fetch_cli, tmp_path, monkeypatch, capsys, system, triple, exe
):
    monkeypatch.setattr(fetch_cli.platform, "system", lambda: system)
    version = _release(tmp_path, fetch_cli, triple, _fake_binary(_version()))
    assert fetch_cli.main() == 0
    dest = tmp_path / "home" / ".helmsman" / "bin" / version / exe
    assert capsys.readouterr().out.strip() == str(dest)
    assert _installed(tmp_path) == [dest]


def test_checksum_mismatch_installs_nothing(fetch_cli, tmp_path, capsys):
    """An archive that fails SHA256SUMS exits 1 with one stderr line and leaves no file behind."""
    _release(
        tmp_path,
        fetch_cli,
        "x86_64-unknown-linux-musl",
        _fake_binary(_version()),
        corrupt=True,
    )
    assert fetch_cli.main() == 1
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "SHA256SUMS" in err
    assert _installed(tmp_path) == []


def test_binary_reporting_wrong_version_is_not_installed(fetch_cli, tmp_path):
    _release(tmp_path, fetch_cli, "x86_64-unknown-linux-musl", _fake_binary("0.0.0"))
    assert fetch_cli.main() == 1
    assert _installed(tmp_path) == []


def test_cached_binary_with_matching_version_skips_download(
    fetch_cli, tmp_path, capsys
):
    version = _version()
    dest = tmp_path / "home" / ".helmsman" / "bin" / version / "helmsman"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(_fake_binary(version))
    dest.chmod(0o755)
    fetch_cli.RELEASES = (tmp_path / "no-such-release").as_uri()
    assert fetch_cli.main() == 0
    assert capsys.readouterr().out.strip() == str(dest)


def test_unsupported_platform_exits_1(fetch_cli, monkeypatch, capsys):
    monkeypatch.setattr(fetch_cli.platform, "machine", lambda: "riscv64")
    assert fetch_cli.main() == 1
    assert "riscv64" in capsys.readouterr().err


def _fake_fetch(path, prints, exit_code):
    path.write_text(f"print({prints!r})\nraise SystemExit({exit_code})\n")
    return path


def test_phase0_fetches_when_nothing_is_cached(tmp_path, monkeypatch):
    """No HELMSMAN_CLI and no cached binary: phase0 runs the fetch once and routes to what it installed."""
    phase0 = load("phase0", "phase0.py")
    cli = _fake_cli(tmp_path / "helmsman")
    monkeypatch.setattr(phase0, "FETCH", _fake_fetch(tmp_path / "f.py", str(cli), 0))
    monkeypatch.delenv("HELMSMAN_CLI")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert phase0._cli_routes() == {"ported": str(cli)}


def test_phase0_failed_fetch_falls_back_to_python(tmp_path, monkeypatch):
    phase0 = load("phase0", "phase0.py")
    monkeypatch.setattr(phase0, "FETCH", _fake_fetch(tmp_path / "f.py", "", 1))
    monkeypatch.delenv("HELMSMAN_CLI")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert phase0._cli_routes() == {}


def test_phase0_off_never_fetches(tmp_path, monkeypatch):
    phase0 = load("phase0", "phase0.py")
    marker = tmp_path / "fetched"
    fetch = tmp_path / "f.py"
    fetch.write_text(f"open({str(marker)!r}, 'w')\n")
    monkeypatch.setattr(phase0, "FETCH", fetch)
    monkeypatch.setenv("HELMSMAN_CLI", "off")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert phase0._cli_routes() == {}
    assert not marker.exists()
