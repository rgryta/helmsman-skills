import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import _diff  # noqa: E402

MAX_BLOCKS = 14
MAX_PER_FILE = 4
ENV_READERS = ("getenv", "environ")
ASSERTING_CALL = re.compile(r"^(assert|check|verify|expect)", re.IGNORECASE)
_SCOPE_NOTE = (
    "scope: python test functions whose body the diff touched; shapes only - a "
    "test can carry every shape here and still be a good test, and can carry none "
    "and still assert nothing meaningful, so each row is a question about intent"
)
_LEAD = (
    "Test-shape facts: changed tests whose form lets them pass without the "
    "behaviour they name. Each is a CANDIDATE, not a defect - the question is "
    "whether this test would still fail if the code it guards were reverted."
)


def _overlaps(spans, lo, hi):
    return any(a <= hi and lo <= b for a, b in spans)


def _call_name(node):
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def _asserts(fn):
    return [n for n in ast.walk(fn) if isinstance(n, ast.Assert)]


def _has_assertion_machinery(fn):
    """An assert statement, a raises/warns context, or an assert-shaped call."""
    if _asserts(fn):
        return True
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and ASSERTING_CALL.match(_call_name(n)):
            return True
        if isinstance(n, (ast.With, ast.AsyncWith)):
            for item in n.items:
                if isinstance(item.context_expr, ast.Call):
                    if _call_name(item.context_expr) in ("raises", "warns"):
                        return True
    return False


def _is_negative(test):
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return True
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        return isinstance(test.ops[0], (ast.NotEq, ast.NotIn, ast.IsNot))
    return False


def _is_tautology(test):
    if isinstance(test, ast.Constant) and test.value is True:
        return True
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        if isinstance(test.ops[0], (ast.Eq, ast.Is)):
            return ast.dump(test.left) == ast.dump(test.comparators[0])
    return False


def _reads_ambient_env(fn):
    reads = any(
        isinstance(n, ast.Attribute)
        and n.attr in ENV_READERS
        or isinstance(n, ast.Call)
        and _call_name(n) == "getenv"
        for n in ast.walk(fn)
    )
    if not reads:
        return False
    controlled = {"setenv", "delenv", "dict"}
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and _call_name(n) in controlled:
            return False
    return True


def _unnamed_caplog_level(fn):
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and _call_name(n) == "at_level":
            if not any(kw.arg == "logger" for kw in n.keywords) and len(n.args) < 2:
                return True
    return False


def shapes(fn):
    out = []
    asserts = _asserts(fn)
    if not _has_assertion_machinery(fn):
        out.append(
            "asserts nothing at all - no assert, no raises context, no assert_* call"
        )
    elif asserts:
        if all(_is_negative(a.test) for a in asserts):
            out.append(
                f"every assertion is negative ({len(asserts)} of {len(asserts)}) - "
                "does it also pin that the code ran and produced the right thing, or "
                "only that one wrong value is absent? the second passes on an empty "
                "result, a crash swallowed upstream, and every other wrong value"
            )
        taut = [a.lineno for a in asserts if _is_tautology(a.test)]
        if taut:
            out.append(
                "compares a value with itself at line(s) "
                + ", ".join(str(t) for t in taut)
                + " - true regardless of the code under test"
            )
    if _reads_ambient_env(fn):
        out.append(
            "reads process env without setenv/delenv - its result depends on the "
            "machine, so it can pass locally and fail in CI or vice versa"
        )
    if _unnamed_caplog_level(fn):
        out.append(
            "at_level() without a logger= argument - if the module pins its own "
            "logger level, records below it are filtered and the assertion cannot fail"
        )
    return out


def run(worktree, base):
    files = [f for f in _diff.changed_py_files(worktree, base) if _diff.is_test_path(f)]
    if not files:
        return f"OK: no changed python test files ({_SCOPE_NOTE})"
    blocks = []
    added = _diff.added_lines_by_path(worktree, base, files)
    for path in files:
        if len(blocks) >= MAX_BLOCKS:
            break
        spans = [(n, n) for n in sorted(added[path] or ())]
        if not spans:
            continue
        full = Path(worktree, path)
        if not full.exists():
            continue
        try:
            tree = ast.parse(full.read_text(errors="ignore"))
        except (OSError, SyntaxError):
            continue
        per_file = 0
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not fn.name.startswith("test"):
                continue
            end = fn.end_lineno or fn.lineno
            if not _overlaps(spans, fn.lineno, end):
                continue
            if per_file >= MAX_PER_FILE or len(blocks) >= MAX_BLOCKS:
                break
            found = shapes(fn)
            if not found:
                continue
            rows = "\n".join(f"- {s}" for s in found)
            blocks.append(f"### {path}:{fn.lineno} `{fn.name}`\n{rows}")
            per_file += 1
    if not blocks:
        return f"OK: no changed test carries a pass-anyway shape ({_SCOPE_NOTE})"
    return _LEAD + "\n\n" + "\n\n".join(blocks) + f"\n\n{_SCOPE_NOTE}"


if __name__ == "__main__":
    try:
        print(run(sys.argv[1], sys.argv[2]))
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
    sys.exit(0)
