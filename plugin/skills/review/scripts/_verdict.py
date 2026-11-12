import re

_TERMINATOR = re.compile(
    r"^[\s>#*_`\-]*(?P<verdict>COMPLETED|COMPLETE|PARTIALLY|PARTIAL)(?![A-Za-z])(?P<rest>.*)$"
)
_TRIM = " \t:.,;-–—*_`"


def classify(text):
    """Return (verdict, detail) for a layer findings file's terminator line.

    verdict is COMPLETE, PARTIAL, or None; detail carries a PARTIAL's reason,
    or why no terminator was found. Layers routinely decorate the line they
    were told to write - `COMPLETE: <what I covered>`, `**COMPLETE**`,
    `COMPLETED` - and every one of those means the layer finished.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None, "empty"
    match = _TERMINATOR.match(lines[-1])
    if not match:
        return None, "no COMPLETE/PARTIAL terminator"
    verdict = "COMPLETE" if match["verdict"].startswith("COMPLETE") else "PARTIAL"
    return verdict, match["rest"].strip(_TRIM)
