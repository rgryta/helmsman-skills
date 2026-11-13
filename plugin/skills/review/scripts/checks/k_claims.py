import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import _diff  # noqa: E402

MAX_BLOCKS = 12
MAX_REFERENTS_PER_CLAIM = 4
MAX_HITS_SHOWN = 3
WINDOW = 0
BACKTICKED = re.compile(r"`([^`\n]{2,60})`")
# \b never matches before a leading underscore, which would hide every private helper
NOT_WORD = r"(?<![A-Za-z0-9_])"
DOTTED = re.compile(
    NOT_WORD + r"_*[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b"
)
CONSTANT = re.compile(NOT_WORD + r"_*[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
SNAKE = re.compile(NOT_WORD + r"_*[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
PROSE_LINE = ("#", '"""', "'''")
# prose that survives the code-ish filters but names nothing in the code
STOPWORDS = frozenset(
    {
        "e.g",
        "i.e",
        "etc",
        "no_op",
        "no_ops",
        "read_only",
        "write_only",
        "up_to_date",
        "out_of_date",
        "as_is",
        "in_place",
        "on_disk",
    }
)
_SCOPE_NOTE = (
    "scope: docstrings and comments inside changed hunks of changed python files; "
    "a referent is a code-shaped token the prose names (backticked, dotted, "
    "CONSTANT_CASE or snake_case), resolved against the names the enclosing "
    "function actually references; prose that names nothing checkable is invisible "
    "here, and a referent that resolves says nothing about whether the sentence "
    "around it is true"
)
_LEAD = (
    "Claim-referent facts: things the prose in this diff NAMES, against what the "
    "code it describes actually references. Each is a CANDIDATE, not a defect - a "
    "referent the enclosing code never touches is either stale, aspirational, or "
    "describing a neighbour, and only reading the sentence tells you which."
)


def _overlaps(spans, lo, hi):
    return any(a <= hi and lo <= b for a, b in spans)


def referents(text):
    out = []
    for m in BACKTICKED.finditer(text):
        inner = m.group(1).strip()
        if inner and " " not in inner:
            out.append(inner.strip("()[]{},.:;"))
    for rx in (DOTTED, CONSTANT, SNAKE):
        out.extend(rx.findall(text))
    seen, keep = set(), []
    for r in out:
        r = r.strip("`'\"")
        if not r or r.lower() in STOPWORDS or r in seen:
            continue
        seen.add(r)
        keep.append(r)
    return keep


def _referenced_names(node):
    """Names the body uses, and its string literals minus the docstring itself.

    The docstring is a string constant in its own body, so counting it would let
    every claim resolve against the sentence making it and the check would pass
    vacuously on all input.
    """
    # by identity, not by value: get_docstring() returns cleandoc-normalised text
    # that never equals the raw constant for a multi-line docstring
    doc_node = None
    body = getattr(node, "body", None)
    if body and isinstance(body[0], ast.Expr):
        first = body[0].value
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            doc_node = first
    names, texts = set(), []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            names.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            names.add(sub.attr)
        elif isinstance(sub, ast.arg):
            names.add(sub.arg)
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            if sub is not doc_node:
                texts.append(sub.value)
    return names, texts


def _resolves(referent, names, texts):
    if referent in names:
        return True
    # prose naming a symbol the body only spells out inside a string still resolves
    if any(referent in t for t in texts):
        return True
    # a dotted referent resolves if any of its segments is referenced, since the
    # prose may spell out a path the code reaches through an alias
    parts = [p for p in re.split(r"[.\s]+", referent) if p]
    return any(p in names for p in parts)


def _comment_claims(worktree, path, spans):
    """(line, text) for comment lines inside changed hunks - docstrings handled by AST."""
    out = []
    try:
        lines = Path(worktree, path).read_text(errors="ignore").splitlines()
    except OSError:
        return out
    for i, raw in enumerate(lines, start=1):
        if not _overlaps(spans, i, i):
            continue
        stripped = raw.strip()
        if stripped.startswith("#") and len(stripped) > 8:
            out.append((i, stripped.lstrip("# ")))
    return out


def _locate(d, worktree, referent, path, prose_lines=frozenset()):
    code, out = d.run_cmd(
        ["git", "grep", "-I", "-F", "-n", "--", referent], cwd=worktree, timeout=30
    )
    if code != 0:
        return "nowhere in the repo", []
    same, other = [], []
    for line in out.splitlines():
        parts = line.split(":", 2)
        # a name inside prose is the claim restating itself, not the code having it
        if len(parts) < 3 or parts[2].lstrip().startswith(PROSE_LINE):
            continue
        if parts[0] == path:
            if int(parts[1]) not in prose_lines:
                same.append(f"{parts[0]}:{parts[1]}")
        else:
            other.append(f"{parts[0]}:{parts[1]}")
    if same:
        return "elsewhere in this same file", same[:MAX_HITS_SHOWN]
    if other:
        return "only in other files", other[:MAX_HITS_SHOWN]
    return "nowhere in the repo", []


def _prose_lines(tree, source_lines):
    """Line numbers holding docstrings or comments, which never count as a definition."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                out.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    for i, raw in enumerate(source_lines, start=1):
        if raw.strip().startswith("#"):
            out.add(i)
    return out


def _block(d, worktree, path, where, line, prose, missing, prose_lines):
    rows = []
    for referent in missing[:MAX_REFERENTS_PER_CLAIM]:
        verdict, hits = _locate(d, worktree, referent, path, prose_lines)
        tail = f" ({', '.join(hits)})" if hits else ""
        rows.append(f"- `{referent}` - not referenced by {where}; {verdict}{tail}")
    if not rows:
        return None
    snippet = " ".join(prose.split())[:150]
    return f"### {path}:{line} {where}\nsays: {snippet}\n" + "\n".join(rows)


def run(worktree, base):
    files = [f for f in _diff.changed_py_files(worktree, base)]
    if not files:
        return f"OK: no changed python files to read claims from ({_SCOPE_NOTE})"
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
            source = full.read_text(errors="ignore")
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            continue
        prose_lines = _prose_lines(tree, source.splitlines())

        claims = []
        for node in ast.walk(tree):
            if not isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                continue
            doc = ast.get_docstring(node)
            if not doc:
                continue
            end = node.end_lineno or node.lineno
            if not _overlaps(spans, node.lineno, end):
                continue
            claims.append((node.lineno, f"`{node.name}`'s own body", doc, node))

        enclosing = [
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for line, text in _comment_claims(worktree, path, spans):
            owner = None
            for n in enclosing:
                if n.lineno <= line <= (n.end_lineno or n.lineno):
                    if owner is None or n.lineno > owner.lineno:
                        owner = n
            if owner is not None:
                claims.append((line, f"`{owner.name}`'s own body", text, owner))

        for line, where, prose, node in claims:
            if len(blocks) >= MAX_BLOCKS:
                break
            names, texts = _referenced_names(node)
            missing = [r for r in referents(prose) if not _resolves(r, names, texts)]
            if not missing:
                continue
            block = _block(
                _diff, worktree, path, where, line, prose, missing, prose_lines
            )
            if block:
                blocks.append(block)

    if not blocks:
        return f"OK: every referent the changed prose names is referenced by the code it describes ({_SCOPE_NOTE})"
    return _LEAD + "\n\n" + "\n\n".join(blocks) + f"\n\n{_SCOPE_NOTE}"


if __name__ == "__main__":
    try:
        print(run(sys.argv[1], sys.argv[2]))
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
    sys.exit(0)
