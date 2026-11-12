import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import _verdict  # noqa: E402

POLL_S = 5
DEADLINE_S = 540


def _terminator(path):
    try:
        text = path.read_text()
    except OSError as e:
        return None, f"unreadable ({type(e).__name__})"
    verdict, detail = _verdict.classify(text)
    if verdict is None:
        return None, detail
    return (verdict if verdict == "COMPLETE" else f"PARTIAL: {detail}"), None


def poll_once(outdir, pr, layers):
    root = Path(outdir)
    if not root.is_dir():
        return {ln: ("NO_OUTDIR", f"{outdir} is not a directory") for ln in layers}
    states = {}
    for layer in layers:
        path = root / f"REVIEW-{pr}-{layer}.md"
        if not path.exists():
            states[layer] = ("MISSING", "no file written yet")
            continue
        term, why = _terminator(path)
        states[layer] = ("READY", term) if term else ("INCOMPLETE", why)
    return states


def await_layers(outdir, pr, layers, deadline_s, poll_s):
    start = time.monotonic()
    while True:
        states = poll_once(outdir, pr, layers)
        if all(s == "READY" for s, _ in states.values()):
            return states, time.monotonic() - start
        if any(s == "NO_OUTDIR" for s, _ in states.values()):
            return states, time.monotonic() - start
        if time.monotonic() - start >= deadline_s:
            return states, time.monotonic() - start
        time.sleep(min(poll_s, max(0.0, deadline_s - (time.monotonic() - start))))


def main(argv):
    if len(argv) < 3:
        print(
            "usage: await_layers.py <outdir> <pr> <layer,layer,...> "
            "[deadline_s] [poll_s]",
            file=sys.stderr,
        )
        return 2
    outdir, pr = argv[0], argv[1]
    layers = [x for x in argv[2].split(",") if x]
    deadline_s = float(argv[3]) if len(argv) > 3 else DEADLINE_S
    poll_s = float(argv[4]) if len(argv) > 4 else POLL_S
    if not layers:
        print("ALL_READY")
        return 0
    states, waited = await_layers(outdir, pr, layers, deadline_s, poll_s)
    for layer in layers:
        state, detail = states[layer]
        print(f"{layer} {state} - {detail}")
    print(f"WAITED: {waited:.0f}s")
    if any(states[ln][0] == "NO_OUTDIR" for ln in layers):
        print(f"NO_OUTDIR: {outdir}")
        return 0
    stalled = [ln for ln in layers if states[ln][0] != "READY"]
    print(f"STALLED: {','.join(stalled)}" if stalled else "ALL_READY")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
