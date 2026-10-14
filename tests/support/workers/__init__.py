"""Worker processes for P and K tests. Run as ``python -m support.workers.<name> '<json>'``."""

import json
import sys
from typing import Any


def args() -> dict[str, Any]:
    data = json.loads(sys.argv[1])
    assert isinstance(data, dict)
    return data


def emit(data: dict[str, Any]) -> None:
    print(json.dumps(data), flush=True)
