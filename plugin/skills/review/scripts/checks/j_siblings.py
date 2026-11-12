import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import _diff  # noqa: E402

MAX_BLOCKS = 14
MAX_ITEMS_PER_FILE = 3
MAX_HITS_SHOWN = 4
MAX_PROBES = 60
TOO_COMMON = 20
MIN_LINE_CHARS = 12
MIN_STRING_CHARS = 6
MIN_NUMBER_DIGITS = 3
MAX_DIFF_LINES = 4000

SKIP_PATH = re.compile(
    r"(^|/)(node_modules|\.venv|venv|dist|build|vendor|__snapshots__)/"
    r"|(\.lock|-lock\.json|\.min\.js|\.map|\.svg|\.png|\.jpg|\.ico|\.woff2?)$"
    r"|\.(md|rst|txt)$"
)
IMPORTish = re.compile(r"^\s*(import|from|#include|using|require\()\b")
PUNCT_ONLY = re.compile(r"^[\s)\]}>,;:'\"`|+\\-]*$")
NUMBERS = re.compile(r"(?<![\w.])(\d[\d_]*\.?\d*)(?![\w.])")
_SCOPE_NOTE = (
    "scope: lines the diff REMOVED that still survive verbatim elsewhere, and "
    "distinctive literals in changed lines that also occur in files this diff did "
    "not touch; text search over tracked code and config files (prose files are "
    "excluded), so a twin that was reworded rather than copied is not found"
)
_LEAD = (
    "Sibling-site facts: places that still carry the shape this diff changed. Each "
    "is a CANDIDATE, not a defect - the question to answer is whether the reasoning "
    "behind the change applies there too, and if not, why not."
)


def changed_files(d, worktree, base):
    code, out = d.run_cmd(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", base, "HEAD"],
        cwd=worktree,
        timeout=60,
    )
    if code != 0:
        return []
    return [f for f in out.splitlines() if f and not SKIP_PATH.search(f)]


def diff_lines(d, worktree, base, path):
    code, out = d.run_cmd(
        ["git", "diff", "-U0", base, "HEAD", "--", path], cwd=worktree, timeout=60
    )
    if code != 0:
        return [], []
    removed, added = [], []
    for line in out.splitlines()[:MAX_DIFF_LINES]:
        if line.startswith("-") and not line.startswith("---"):
            removed.append(line[1:])
        elif line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
    return removed, added


def _distinctive_lines(removed):
    out = []
    for raw in removed:
        text = raw.strip()
        if (
            len(text) < MIN_LINE_CHARS
            or PUNCT_ONLY.match(text)
            or IMPORTish.match(text)
        ):
            continue
        out.append(text)
    # longest first: a longer surviving line is a stronger twin signal than a short one
    return sorted(dict.fromkeys(out), key=len, reverse=True)


def _quoted(line, quote):
    parts = line.split(quote)
    # an odd count means an unterminated quote, where pairing is guesswork
    if len(parts) < 3 or len(parts) % 2 == 0:
        return []
    return parts[1::2]


def _literals(lines):
    out = []
    for raw in lines:
        for quote in ('"', "'"):
            out.extend(t for t in _quoted(raw, quote) if len(t) >= MIN_STRING_CHARS)
        for m in NUMBERS.finditer(raw):
            tok = m.group(1)
            if sum(c.isdigit() for c in tok) >= MIN_NUMBER_DIGITS and not tok.endswith(
                "."
            ):
                out.append(tok)
    return sorted(dict.fromkeys(out), key=len, reverse=True)


def _grep(d, worktree, needle):
    code, out = d.run_cmd(
        ["git", "grep", "-I", "-F", "-n", "--", needle], cwd=worktree, timeout=30
    )
    if code != 0:
        return []
    hits = []
    for line in out.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 3 or SKIP_PATH.search(parts[0]):
            continue
        hits.append((parts[0], parts[1]))
    return hits


def _render(hits):
    shown = hits[:MAX_HITS_SHOWN]
    text = ", ".join(f"{p}:{ln}" for p, ln in shown)
    if len(hits) > len(shown):
        text += f" (+{len(hits) - len(shown)} more)"
    return text


def _full(blocks, probes):
    return len(blocks) >= MAX_BLOCKS or probes >= MAX_PROBES


def _abbrev(text, width=88):
    return text if len(text) <= width else text[: width - 1] + "…"


def run(worktree, base):
    files = changed_files(_diff, worktree, base)
    if not files:
        return f"OK: no changed files to match ({_SCOPE_NOTE})"
    changed_set = set(files)
    blocks = []
    probes = 0
    per_file = {}
    pending = {}
    for path in files:
        removed, added = diff_lines(_diff, worktree, base, path)
        if removed or added:
            pending[path] = (removed, added)

    # surviving-line probes run to completion before any literal probe, so the
    # weaker signal can never starve the stronger one out of the shared budget
    for path, (removed, added) in pending.items():
        # a line the diff also re-adds was moved, not abandoned, so its surviving
        # copy is the same code in a new place rather than an untouched twin
        moved = {t.strip() for t in added}
        for text in _distinctive_lines(removed):
            if _full(blocks, probes) or per_file.get(path, 0) >= MAX_ITEMS_PER_FILE:
                break
            if text in moved:
                continue
            probes += 1
            hits = _grep(_diff, worktree, text)
            if not hits or len(hits) > TOO_COMMON:
                continue
            blocks.append(
                f"### {path} - a line this diff removed still exists elsewhere\n"
                f"`{_abbrev(text)}`\n"
                f"still at: {_render(hits)}"
            )
            per_file[path] = per_file.get(path, 0) + 1

    for path, (removed, added) in pending.items():
        for lit in _literals(removed + added):
            if _full(blocks, probes) or per_file.get(path, 0) >= MAX_ITEMS_PER_FILE:
                break
            probes += 1
            hits = _grep(_diff, worktree, lit)
            outside = [h for h in hits if h[0] not in changed_set]
            if not outside or len(hits) > TOO_COMMON:
                continue
            blocks.append(
                f"### {path} - a literal this diff touched also lives in files it did not\n"
                f"`{_abbrev(lit)}`\n"
                f"also at: {_render(outside)}"
            )
            per_file[path] = per_file.get(path, 0) + 1

    if not blocks:
        return f"OK: no surviving sibling shapes found ({_SCOPE_NOTE})"
    tail = ""
    if probes >= MAX_PROBES:
        tail = "\n\n(probe budget reached - this list is not exhaustive)"
    return (
        _LEAD + "\n\n" + "\n\n".join(blocks[:MAX_BLOCKS]) + tail + f"\n\n{_SCOPE_NOTE}"
    )


if __name__ == "__main__":
    try:
        print(run(sys.argv[1], sys.argv[2]))
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
    sys.exit(0)
