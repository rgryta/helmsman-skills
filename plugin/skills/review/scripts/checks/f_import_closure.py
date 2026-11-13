import ast
import importlib.metadata
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import _diff  # noqa: E402
import _env  # noqa: E402

_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_DIST_MAP_PROBE = (
    "import importlib.metadata, json; "
    "print(json.dumps(importlib.metadata.packages_distributions()))"
)


def _local_roots(worktree):
    r = subprocess.run(
        ["git", "ls-files", "--", "*__init__.py"],
        cwd=worktree,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if r.returncode != 0:
        return None
    return {Path(f).parent.name for f in r.stdout.splitlines()}


def _top_imports(path):
    try:
        tree = ast.parse(path.read_text(errors="ignore"))
    except (SyntaxError, OSError):
        return set()
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.add(node.module.split(".")[0])
    return mods


def _owning_pkg(root, path):
    p = path.parent
    while True:
        if (p / "pyproject.toml").exists():
            return p
        if p == root or p == p.parent:
            break
        p = p.parent
    return None


def _normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _req_name(req):
    if not isinstance(req, str):
        return None
    m = _REQ_NAME.match(req)
    return m.group(1) if m else None


def _declared_dependencies(pkg_dir):
    try:
        data = tomllib.loads((pkg_dir / "pyproject.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None
    if "project" not in data:
        return None
    project = data["project"]
    if "dependencies" in (project.get("dynamic") or []):
        return None
    reqs = list(project.get("dependencies") or [])
    for group in (project.get("optional-dependencies") or {}).values():
        reqs.extend(group or [])
    for group in (data.get("dependency-groups") or {}).values():
        for entry in group or []:
            if isinstance(entry, str):
                reqs.append(entry)
    names = {n for n in (_req_name(r) for r in reqs) if n}
    return {_normalize(n) for n in names}


def _resolve(mod, declared, dist_map):
    if _normalize(mod) in declared:
        return "ok"
    dists = dist_map.get(mod)
    if dists:
        if {_normalize(d) for d in dists} & declared:
            return "ok"
        return "finding"
    return "unverified"


def _dist_map(worktree):
    """Module-to-distribution map from the reviewed repo's env, else from this interpreter."""
    python, trusted, _ = _env.ensure_project_env(worktree)
    if python and trusted:
        code, out = _diff.run_cmd(
            [python, "-c", _DIST_MAP_PROBE], cwd=worktree, timeout=60
        )
        if code == 0:
            try:
                return json.loads(out), "the reviewed repo's env"
            except ValueError:
                pass
    try:
        return importlib.metadata.packages_distributions(), (
            "the interpreter running Phase 0, not the reviewed repo's environment"
        )
    except Exception:
        return {}, "nothing - no env could be read"


def _deferred_imports(d, worktree, base, files):
    added_map = d.added_lines_by_path(worktree, base, files)
    """Imports the diff added inside a function body.

    A deferred import is sometimes load-bearing - breaking a cycle, or importing
    after a fixture has patched config - so these are candidates to justify, not
    defects. Scoped to added lines because a repo can carry many deliberate ones.
    """
    out = []
    for path in files:
        full = Path(worktree, path)
        if not full.exists():
            continue
        try:
            tree = ast.parse(full.read_text(errors="ignore"))
        except (OSError, SyntaxError):
            continue
        added = added_map.get(path)
        if not added:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, (ast.Import, ast.ImportFrom)):
                    continue
                if node.lineno not in added:
                    continue
                name = getattr(node, "module", None) or ", ".join(
                    a.name for a in node.names
                )
                out.append(f"- {path}:{node.lineno} in `{fn.name}` imports `{name}`")
    return out


def run(worktree, base):
    files = _diff.changed_py_files(worktree, base)
    if not files:
        return "OK: no python changes"
    # computed here, not at the end: the closure logic returns early on several
    # paths (no third-party imports, no owning package), and a signal appended
    # only to the last return is silently absent from the common case
    deferred = _deferred_imports(_diff, worktree, base, files)
    placement = ""
    if deferred:
        placement = (
            "\n\nDeferred imports this diff ADDED (inside a function body):\n"
            + "\n".join(deferred)
            + "\n- each is a CANDIDATE, not a defect: a deferred import is right "
            "when it breaks a cycle or must run after a fixture patches config, "
            "and wrong when it is only habit. Say which this one is."
        )
    local_roots = _local_roots(worktree)
    if local_roots is None:
        return (
            "SKIPPED: could not determine local package roots (git ls-files failed)"
            + placement
        )

    root = Path(worktree)
    by_pkg, orphans = {}, set()
    for f in files:
        path = Path(worktree, f)
        siblings = {q.stem for q in path.parent.glob("*.py")}
        mods = (
            _top_imports(path) - set(sys.stdlib_module_names) - local_roots - siblings
        )
        if not mods:
            continue
        pkg = _owning_pkg(root, path)
        if pkg is None:
            orphans |= mods
        else:
            by_pkg.setdefault(pkg, set()).update(mods)

    if not by_pkg and not orphans:
        return "OK: no third-party imports in changed files" + placement
    if not by_pkg:
        return (
            "SKIPPED: no owning package (pyproject.toml) found for changed files"
            + placement
        )

    units = sorted(
        ((pkg, m) for pkg, mods in by_pkg.items() for m in mods),
        key=lambda pm: (str(pm[0]), pm[1]),
    ) + sorted(((None, m) for m in orphans), key=lambda pm: pm[1])
    total = len(units)
    probed = units[:20]

    dist_map, resolved_with = _dist_map(worktree)

    declared_cache = {}
    findings, unverified, ok_count = [], [], 0
    for pkg, m in probed:
        if pkg is None:
            unverified.append(
                f"- `{m}` - changed file has no owning package (pyproject.toml), "
                "not reported as pass or finding"
            )
            continue
        if pkg not in declared_cache:
            declared_cache[pkg] = _declared_dependencies(pkg)
        declared = declared_cache[pkg]
        rel = pkg.relative_to(root)
        if declared is None:
            unverified.append(
                f"- `{m}` (package {rel}) - dependency declaration not statically "
                "readable, not reported as pass or finding"
            )
            continue
        status = _resolve(m, declared, dist_map)
        if status == "finding":
            findings.append(
                f"- `{m}` imported in {rel} but not declared in its pyproject.toml "
                "dependencies"
            )
        elif status == "unverified":
            unverified.append(
                f"- `{m}` (package {rel}) - could not confidently map to a declared "
                "dependency name, not reported as pass or finding"
            )
        else:
            ok_count += 1

    note = (
        f"\n- NOTE: {total - len(probed)} more import(s) not evaluated"
        if total > len(probed)
        else ""
    )
    scope = (
        "SCOPE: compared against each changed file's owning package's pyproject.toml "
        "declared dependencies (dependencies/optional-dependencies/dependency-groups); "
        "not verified against which of those groups the deployment image actually "
        f"installs; module-to-distribution name mapping is resolved using {resolved_with}, "
        "so an import it cannot map is reported UNVERIFIED rather than as a finding"
    )
    if findings:
        head = "FINDINGS: import closure vs declared dependencies"
    elif deferred:
        head = f"FINDINGS: {len(deferred)} deferred import(s) added by this diff"
    elif unverified:
        head = (
            f"SKIPPED: {len(unverified)} import(s) could not be verified, "
            f"{ok_count} declared in the owning package's pyproject.toml"
        )
    else:
        head = (
            f"OK: {ok_count} third-party import(s) declared in the owning package's "
            "pyproject.toml"
        )
    body = "".join(f"\n{line}" for line in findings + unverified)
    return head + body + note + f"\n- {scope}" + placement


if __name__ == "__main__":
    try:
        print(run(sys.argv[1], sys.argv[2]))
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
    sys.exit(0)
