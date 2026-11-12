import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import _diff  # noqa: E402

MAX_BLOCKS = 12
_SCOPE_NOTE = (
    "scope: python parameters clamped on one side inside a hunk this diff added; "
    "a one-sided clamp is right whenever the other side is guaranteed elsewhere, so "
    "each row is a question about who supplies the value, not a defect"
)
_LEAD = (
    "Bound facts: parameters this diff clamps at one end and not the other. Each is "
    "a CANDIDATE - the open end is whatever the caller passes, so the question is "
    "what the extreme member of that set does here."
)
_OPPOSITE = {"min": "max", "max": "min"}
_OPEN_END = {"min": "low", "max": "high"}


def _functions(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _params(fn):
    a = fn.args
    return {x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs)}


def _clamped_names(call):
    """Names this min/max binds, following only nested min/max.

    max(lo, min(p, hi)) is the canonical two-sided idiom, so p is clamped by
    both. min(n, len(p)) clamps the length, never p, so a call that is not
    itself a clamp ends the walk.
    """
    for arg in call.args:
        if isinstance(arg, ast.Name):
            yield arg.id
        elif (
            isinstance(arg, ast.Call)
            and isinstance(arg.func, ast.Name)
            and arg.func.id in _OPPOSITE
        ):
            yield from _clamped_names(arg)


def _clamps(fn, params):
    uses = {}
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in _OPPOSITE:
            continue
        for name in _clamped_names(node):
            if name in params:
                uses.setdefault(name, {}).setdefault(node.func.id, []).append(
                    node.lineno
                )
    return uses


def _blocks(path, source, added):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out = []
    for fn in _functions(tree):
        for param, kinds in _clamps(fn, _params(fn)).items():
            if len(kinds) != 1:
                continue
            kind, lines = next(iter(kinds.items()))
            fresh = [n for n in lines if added is None or n in added]
            if not fresh:
                continue
            out.append(
                f"### {path}:{fresh[0]} `{fn.name}` parameter `{param}`\n"
                f"- clamped with `{kind}()` at {', '.join(str(n) for n in fresh)}, "
                f"never with `{_OPPOSITE[kind]}()` in this function - the {_OPEN_END[kind]} "
                "end is whatever the caller passes"
            )
    return out


def run(worktree, base):
    files = _diff.changed_py_files(worktree, base)
    if not files:
        return "OK: no python changes"
    added_by_path = _diff.added_lines_by_path(worktree, base, files)
    blocks = []
    for f in files:
        path = Path(worktree, f)
        if not path.exists():
            continue
        blocks += _blocks(f, path.read_text(errors="replace"), added_by_path.get(f))
    if not blocks:
        return f"OK: no one-sided clamp added on any parameter ({_SCOPE_NOTE})"
    shown = blocks[:MAX_BLOCKS]
    more = (
        f"\n\n- NOTE: {len(blocks) - len(shown)} more not shown"
        if len(blocks) > len(shown)
        else ""
    )
    return _LEAD + "\n\n" + "\n\n".join(shown) + more + f"\n\n{_SCOPE_NOTE}"


if __name__ == "__main__":
    try:
        print(run(sys.argv[1], sys.argv[2]))
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
    sys.exit(0)
