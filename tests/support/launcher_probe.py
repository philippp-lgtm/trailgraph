"""A stand-in program for the launcher's own tests (``tests/test_harness.py``)."""

import json
import os
import signal
import sys
import time

RUNS = []  # state a run leaves behind; a later run must not see it


def main() -> int:
    action = sys.argv[1]
    RUNS.append(action)
    if action == "echo":
        print(
            json.dumps(
                {
                    "argv": sys.argv,
                    "cwd": os.getcwd(),
                    "env": os.environ.get("PROBE"),
                    "stdin": sys.stdin.read(),
                    "runs": RUNS,
                    "path0": sys.path[0],
                }
            )
        )
        print("to stderr", file=sys.stderr)
        return int(sys.argv[2]) if len(sys.argv) > 2 else 0
    if action == "raise":
        raise ValueError("boom")
    if action == "exit_text":
        sys.exit("a message")
    if action == "sleep":
        time.sleep(30)
    if action == "kill":
        os.kill(os.getpid(), signal.SIGKILL)
    return 0
