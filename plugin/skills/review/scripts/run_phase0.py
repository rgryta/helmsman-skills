import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

PHASE0 = Path(__file__).with_name("phase0.py")
SIGNAL_GRACE_S = 10


def _write_atomic(path, text):
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(text)
    os.replace(tmp, path)


def _exit_description(returncode):
    if returncode >= 0:
        return f"exit {returncode}"
    signum = -returncode
    try:
        return f"signal {signum} ({signal.Signals(signum).name})"
    except ValueError:
        return f"signal {signum}"


def _diagnostic(stdout, stderr):
    stderr_text = " | ".join(line.strip() for line in stderr if line.strip())[-300:]
    stdout_text = " | ".join(line.strip() for line in stdout if line.strip())[-180:]
    parts = []
    if stderr_text:
        parts.append(f"stderr: {stderr_text}")
    if stdout_text:
        parts.append(f"stdout: {stdout_text}")
    return "; ".join(parts) or "no diagnostic output"


def _shell_status(returncode):
    return returncode if returncode >= 0 else 128 - returncode


def _signal_group(pid, signum):
    try:
        os.killpg(pid, signum)
    except ProcessLookupError:
        pass


def _stop_group(pid, already_signalled=False):
    if not already_signalled:
        _signal_group(pid, signal.SIGTERM)
    deadline = time.monotonic() + SIGNAL_GRACE_S
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    _signal_group(pid, signal.SIGKILL)


def _tee(stream, sink, tail):
    for line in stream:
        tail.append(line)
        sink.write(line)
        sink.flush()


def main(argv):
    parser = argparse.ArgumentParser(prog="run_phase0.py")
    parser.add_argument("worktree")
    parser.add_argument("out_file", type=Path)
    parser.add_argument(
        "--base", help="ref the PR merges into, e.g. origin/<baseRefName>"
    )
    args = parser.parse_args(argv)
    worktree, out_file = args.worktree, args.out_file
    out_file.unlink(missing_ok=True)
    base_ref = [args.base] if args.base else []
    proc_ref = []
    received_signal = []

    def _forward_signal(signum, _frame):
        if not received_signal:
            received_signal.append(signum)
        if proc_ref:  # checks restore their mutants on SIGTERM only
            _signal_group(proc_ref[0].pid, signal.SIGTERM)

    handled = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        handled.append(signal.SIGHUP)
    previous = {signum: signal.getsignal(signum) for signum in handled}
    for signum in handled:
        signal.signal(signum, _forward_signal)
    try:
        proc = subprocess.Popen(
            [sys.executable, str(PHASE0), worktree, str(out_file), *base_ref],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
        proc_ref.append(proc)
        if received_signal:
            _signal_group(proc.pid, signal.SIGTERM)
        stdout_tail = deque(maxlen=3)
        stderr_tail = deque(maxlen=3)
        stdout_thread = threading.Thread(
            target=_tee, args=(proc.stdout, sys.stdout, stdout_tail), daemon=True
        )
        stderr_thread = threading.Thread(
            target=_tee, args=(proc.stderr, sys.stderr, stderr_tail), daemon=True
        )
        stdout_thread.start()
        stderr_thread.start()
        returncode = proc.wait()
        if received_signal or returncode != 0:
            _stop_group(proc.pid, already_signalled=bool(received_signal))
        stdout_thread.join()
        stderr_thread.join()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)

    status = 128 + received_signal[0] if received_signal else _shell_status(returncode)
    if out_file.is_file():
        return status
    ended = (
        f"guard received {_exit_description(-received_signal[0])}"
        if received_signal
        else f"child ended with {_exit_description(returncode)}"
    )
    failure = (
        f"Phase 0 unavailable: {ended} before producing facts; "
        f"{_diagnostic(stdout_tail, stderr_tail)}"
    )
    try:
        _write_atomic(out_file, failure + "\n")
    except OSError as exc:
        print(f"failed to write Phase 0 fallback: {exc}", file=sys.stderr)
    print(failure, file=sys.stderr)
    return status or 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
