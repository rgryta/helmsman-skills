import re
import sys
from pathlib import Path

MARKERS = [
    re.compile(r"\bsubagent_type\b", re.I),
    re.compile(r"\b(?:task|agent)\s+tool\b", re.I),
    re.compile(
        r"\b(?:spawn|dispatch|launch)\w*\s+"
        r"(?:a|an|one|each|its\s+own|per|parallel|\d+)?\s*"
        r"(?:sub-?agents?|agents?|tasks?)\b",
        re.I,
    ),
    re.compile(r"\bspawn\s+one\s+per\b", re.I),
    re.compile(r"\bsub-?agents?\s+in\s+parallel\b", re.I),
]
NEGATION = re.compile(
    r"\b(?:never|not|no|none|without|do\s+not|don't|does\s+not|doesn't|avoid|forbid\w*)\b",
    re.I,
)


CLAUSE_SPLIT = re.compile(r"(?<=[.;:!?])\s+|\s+[-\u2014]\s+")


def _clause(line, pos):
    start = 0
    for m in CLAUSE_SPLIT.finditer(line):
        if m.end() > pos:
            break
        start = m.end()
    return line[start:pos]


def scan(text):
    hits, suppressed = [], []
    for lineno, line in enumerate(text.splitlines(), 1):
        spans = [m.start() for mk in MARKERS for m in mk.finditer(line)]
        if not spans:
            continue
        entry = (lineno, line.strip()[:160])
        live = any(not NEGATION.search(_clause(line, pos)) for pos in spans)
        (hits if live else suppressed).append(entry)
    return hits, suppressed


def render(path):
    try:
        text = Path(path).read_text()
    except OSError as e:
        return f"FANOUT: unreadable ({type(e).__name__}) - treated as fan-out", 0
    hits, suppressed = scan(text)
    lines = []
    if hits:
        lines.append(
            f"FANOUT: {len(hits)} dispatch marker(s) - do NOT wrap in an Agent"
        )
    else:
        lines.append("FLAT: no dispatch markers - safe to wrap in an Agent")
    lines += [f"- L{n}: {t}" for n, t in hits]
    lines += [f"- (negated, ignored) L{n}: {t}" for n, t in suppressed]
    return "\n".join(lines), len(hits)


def main(argv):
    if len(argv) < 1:
        print("usage: fanout_check.py <skill_md_path>", file=sys.stderr)
        return 2
    out, _ = render(argv[0])
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
