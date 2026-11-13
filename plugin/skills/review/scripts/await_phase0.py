import sys
import time
from pathlib import Path

POLL_S = 5
DEADLINE_S = 590


def await_phase0(facts, deadline_s, poll_s):
    start = time.monotonic()
    while True:
        elapsed = time.monotonic() - start
        if (
            facts.is_file()
        ):  # every writer renames into place, so existing means complete
            return "READY", elapsed
        if not facts.parent.is_dir():
            return "MISSING", elapsed
        if elapsed >= deadline_s:
            return "TIMEOUT", elapsed
        time.sleep(min(poll_s, max(0.0, deadline_s - elapsed)))


def main(argv):
    if not argv:
        print(
            "usage: await_phase0.py <facts-file> [deadline_s] [poll_s]", file=sys.stderr
        )
        return 2
    facts = Path(argv[0])
    deadline_s = float(argv[1]) if len(argv) > 1 else DEADLINE_S
    poll_s = float(argv[2]) if len(argv) > 2 else POLL_S
    state, waited = await_phase0(facts, deadline_s, poll_s)
    if state == "READY":
        lines = facts.read_text(errors="replace").splitlines()
        print(f"READY - {lines[0] if lines else 'empty'}")
    elif state == "MISSING":
        print(f"MISSING - {facts.parent} is not a directory")
    else:
        print(f"TIMEOUT - no facts file at {facts}")
    print(f"WAITED: {waited:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
