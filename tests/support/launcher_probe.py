"""A stand-in program for the launcher's own tests (``tests/test_harness.py``)."""

import json
import os
import signal
import subprocess
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
    if action == "interrupt":
        raise KeyboardInterrupt
    if action == "dunder":
        print("via __stdout__", file=sys.__stdout__)
    if action == "leave_child":  # a child that keeps stdout open after the program exits
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.5)"])
    return 0
