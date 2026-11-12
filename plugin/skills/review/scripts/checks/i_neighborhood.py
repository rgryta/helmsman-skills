import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import _diff  # noqa: E402

MAX_BLOCKS = 12
MAX_HUNKS_PER_FILE = 4
MAX_CALLS_EACH_SIDE = 8
MAX_REBINDS = 6
MAX_CALLERS = 5
CALLER_NAME_TOO_COMMON = 25
WINDOW = 40
HUNK = re.compile(r"^@@ -\S+ \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")
TEST_PATH = re.compile(r"(^|/)(tests?|conftest)(/|\.py$)|(^|/)test_[^/]*\.py$")
_SCOPE_NOTE = (
    "scope: non-test changed python files, insertions into existing functions "
    "first (a wholly new function has no execution-order neighbours); calls are those "
    f"within {WINDOW} lines of a changed hunk, bindings are other assignments to "
    "names the changed lines read"
)


def hunks(d, worktree, base, path):
    code, out = d.run_cmd(
        ["git", "diff", "-U0", base, "HEAD", "--", path], cwd=worktree, timeout=60
    )
    if code != 0:
        return []
    spans = []
    for line in out.splitlines():
        m = HUNK.match(line)
        if not m:
            continue
        start = int(m.group("start"))
        count = 1 if m.group("count") is None else int(m.group("count"))
        if count:
            spans.append((start, start + count - 1))
    return _merge(spans)


def _merge(spans):
    out = []
    for start, end in sorted(spans):
        if out and start <= out[-1][1] + WINDOW // 2:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def _call_name(node):
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _enclosing(tree, lineno):
    best = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.lineno <= lineno <= (node.end_lineno or node.lineno):
            if best is None or node.lineno > best.lineno:
                best = node
    return best


def _is_insertion(tree, span):
    start, end = span
    fn = _enclosing(tree, start)
    if fn is None:
        return False
    return not (fn.lineno >= start and (fn.end_lineno or fn.lineno) <= end)


def _read_names(tree, start, end):
    names = set()
    for node in ast.walk(tree):
        if not (start <= getattr(node, "lineno", -1) <= end):
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            names.add(node.id)
    return names


def _targets(node):
    if isinstance(node, ast.Assign):
        return node.targets
    if isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        return [node.target]
    return []


def _rebinds(tree, names, spans):
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            continue
        if any(a <= node.lineno <= b for a, b in spans):
            continue
        for t in _targets(node):
            if isinstance(t, ast.Name) and t.id in names:
                out.append((node.lineno, t.id))
    return sorted(set(out))


def _window_calls(tree, start, end):
    before, after = [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if not name:
            continue
        ln = node.lineno
        if start - WINDOW <= ln < start:
            before.append((ln, name))
        elif end < ln <= end + WINDOW:
            after.append((ln, name))
    before = sorted(set(before))[-MAX_CALLS_EACH_SIDE:]
    after = sorted(set(after))[:MAX_CALLS_EACH_SIDE]
    return before, after


def _callers(d, worktree, name, path):
    code, out = d.run_cmd(
        ["git", "grep", "-n", "--", f"{name}(", "--", "*.py"], cwd=worktree, timeout=60
    )
    if code != 0:
        return []
    hits = []
    for line in out.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 3 or parts[0] == path or f"def {name}(" in parts[2]:
            continue
        hits.append(f"{parts[0]}:{parts[1]}")
    return hits


def _hunk_block(d, worktree, path, tree, spans, span, before, after):
    start, end = span
    fn = _enclosing(tree, start)
    where = f"in `{fn.name}`" if fn else "at module level"
    body = [f"### {path}:{start}-{end} {where}"]
    if before:
        body.append(
            "runs BEFORE the change (ungated by anything the change adds): "
            + ", ".join(f"{n}@{ln}" for ln, n in before)
        )
    if after:
        body.append(
            "runs AFTER the change (still runs unless the change returns first): "
            + ", ".join(f"{n}@{ln}" for ln, n in after)
        )
    reb = _rebinds(tree, _read_names(tree, start, end), spans)
    if reb:
        shown = reb[:MAX_REBINDS]
        body.append(
            "other bindings of names the changed lines read (each is another shape "
            "reaching this code): "
            + ", ".join(f"{n}@{ln}" for ln, n in shown)
            + (f" (+{len(reb) - len(shown)} more)" if len(reb) > len(shown) else "")
        )
    if fn:
        callers = _callers(d, worktree, fn.name, path)
        if len(callers) > CALLER_NAME_TOO_COMMON:
            body.append(
                f"entry points from other files: `{fn.name}` is too common a name to "
                f"attribute by grep ({len(callers)} hits) - not listed"
            )
        elif callers:
            shown = callers[:MAX_CALLERS]
            body.append(
                "entry points from other files: "
                + ", ".join(shown)
                + (
                    f" (+{len(callers) - len(shown)} more)"
                    if len(callers) > len(shown)
                    else ""
                )
            )
    return "\n".join(body) if len(body) > 1 else None


def run(worktree, base):
    files = [
        f for f in _diff.changed_py_files(worktree, base) if not TEST_PATH.search(f)
    ]
    if not files:
        return f"OK: no non-test python changes ({_SCOPE_NOTE})"
    blocks = []
    candidates = []
    for path in files:
        if not Path(worktree, path).exists():
            continue
        spans = hunks(_diff, worktree, base, path)
        if not spans:
            continue
        try:
            tree = ast.parse(Path(worktree, path).read_text())
        except (OSError, SyntaxError) as e:
            blocks.append(f"### {path}\nSKIPPED: unparseable ({type(e).__name__})")
            continue
        for span in spans:
            before, after = _window_calls(tree, *span)
            candidates.append(
                (
                    _is_insertion(tree, span),
                    len(before) + len(after),
                    path,
                    tree,
                    spans,
                    span,
                    before,
                    after,
                )
            )
    candidates.sort(key=lambda c: (not c[0], -c[1], c[2], c[5]))
    per_file = {}
    ranked = 0
    for _, neighbours, path, tree, spans, span, before, after in candidates:
        if not neighbours or len(blocks) >= MAX_BLOCKS:
            continue
        if per_file.get(path, 0) >= MAX_HUNKS_PER_FILE:
            continue
        block = _hunk_block(_diff, worktree, path, tree, spans, span, before, after)
        if block:
            per_file[path] = per_file.get(path, 0) + 1
            blocks.append(block)
            ranked += 1
    if not blocks:
        return f"OK: no changed hunks with surrounding code to map ({_SCOPE_NOTE})"
    skipped = sum(1 for c in candidates if c[1]) - ranked
    tail = f"\n\n({skipped} further changed hunk(s) not shown)" if skipped > 0 else ""
    return (
        "Boundary facts: what runs around each changed hunk, what else binds the "
        "names it reads, and which other files call in. A guard is only as good as "
        "the shapes that reach it and the work that runs around it.\n\n"
        + "\n\n".join(blocks)
        + tail
        + f"\n\n{_SCOPE_NOTE}"
    )


if __name__ == "__main__":
    try:
        print(run(sys.argv[1], sys.argv[2]))
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
    sys.exit(0)
