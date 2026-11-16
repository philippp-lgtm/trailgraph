"""A stand-in program whose import writes to stderr: the launcher refuses it."""

import sys

sys.stderr.write("noise at import\n")


def main() -> int:
    return 0
