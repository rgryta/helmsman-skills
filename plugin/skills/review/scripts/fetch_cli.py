"""Install the `helmsman` release binary for this plugin version and this OS/arch, print its path."""

import hashlib
import io
import json
import os
import platform
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = HERE.parents[2] / ".claude-plugin" / "plugin.json"
RELEASES = "https://github.com/rgryta/helmsman-skills/releases/download"
BUDGET_S = 20
ARCHES = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "AMD64": "x86_64",
    "arm64": "aarch64",
    "aarch64": "aarch64",
    "ARM64": "aarch64",
}
SYSTEMS = {
    "Linux": "unknown-linux-musl",
    "Darwin": "apple-darwin",
    "Windows": "pc-windows-msvc",
}
TARGETS = {f"{a}-{s}" for a in set(ARCHES.values()) for s in SYSTEMS.values()}


class FetchError(Exception):
    pass


def target(system: str, machine: str) -> str | None:
    arch, suffix = ARCHES.get(machine), SYSTEMS.get(system)
    return f"{arch}-{suffix}" if arch and suffix else None


def _reports_version(exe: Path, version: str) -> bool:
    try:
        r = subprocess.run(
            [str(exe), "--version"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and r.stdout.strip() == f"helmsman {version}"


def _download(url: str, deadline: float) -> bytes:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise FetchError(f"timed out before {url}")
    with urllib.request.urlopen(url, timeout=remaining) as resp:
        return resp.read()


def _expected_sha256(sums: str, archive: str) -> str:
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == archive:
            return parts[0].lower()
    raise FetchError(f"{archive} not listed in SHA256SUMS")


def _binary_bytes(data: bytes, archive: str, exe: str) -> bytes:
    if archive.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for info in z.infolist():
                if not info.is_dir() and Path(info.filename).name == exe:
                    return z.read(info)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
            for member in t.getmembers():
                if member.isfile() and Path(member.name).name == exe:
                    return t.extractfile(member).read()
    raise FetchError(f"{exe} not found in {archive}")


def _install(payload: bytes, dest: Path, version: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, suffix=dest.suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        os.chmod(tmp, 0o755)
        if not _reports_version(Path(tmp), version):
            raise FetchError(f"downloaded binary does not report helmsman {version}")
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        try:
            dest.parent.rmdir()
        except OSError:
            pass
        raise


def fetch() -> Path:
    deadline = time.monotonic() + BUDGET_S
    triple = target(platform.system(), platform.machine())
    if triple is None:
        raise FetchError(f"no release for {platform.system()}/{platform.machine()}")
    version = json.loads(MANIFEST.read_text())["version"]
    exe = "helmsman.exe" if triple.endswith("windows-msvc") else "helmsman"
    dest = Path.home() / ".helmsman" / "bin" / version / exe
    if dest.is_file() and _reports_version(dest, version):
        return dest
    archive = f"helmsman-{version}-{triple}" + (
        ".zip" if exe.endswith(".exe") else ".tar.gz"
    )
    base = f"{RELEASES}/v{version}"
    sums = _download(f"{base}/SHA256SUMS", deadline).decode()
    data = _download(f"{base}/{archive}", deadline)
    if hashlib.sha256(data).hexdigest() != _expected_sha256(sums, archive):
        raise FetchError(f"{archive} does not match SHA256SUMS")
    _install(_binary_bytes(data, archive, exe), dest, version)
    return dest


def main() -> int:
    try:
        path = fetch()
    except Exception as e:
        print(f"fetch_cli: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
