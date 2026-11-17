"""The child process that runs one extractor (file metadata M11).

Started by ``extractors.runner`` as ``python -I -X utf8 -m graph_cli.extractors.child``: one
request as JSON on standard input, one answer as JSON on standard output, nothing else. The
parent never parses file bytes itself, so a hostile file can only hurt this process, which
runs under a CPU and an address space limit and is killed at the wall-clock timeout.

Answer: ``{"status": "ok" | "failed" | "transient", "problem": str | null, "values": {...},
"parts": [{"name": str, "values": {...}}], "detail": str}``. A ``transient`` answer says
nothing about the bytes (the path cannot be read); the parent never records it.
"""

from __future__ import annotations

import contextlib
import json
import math
import resource
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from graph_cli.extractors import registry
from graph_cli.extractors.model import (
    PROBLEM_ERROR,
    PROBLEM_MEMORY,
    PROBLEM_OUTPUT,
    PROBLEM_UNKNOWN_EXTRACTOR,
    ExtractionResult,
    RunInput,
)
from graph_cli.kernel.canonical import JsonValue

MAX_ANSWER_BYTES: Final = 1024 * 1024


def apply_limits(timeout_seconds: float, max_memory_mib: int) -> None:
    """Limit this process before it touches the file.

    No core files (they would hold file content), CPU time a second above the parent's
    wall-clock timeout (a backstop when the parent stalls) and the address space. macOS
    ignores ``RLIMIT_AS``: there the memory limit is best effort and the timeout still holds.
    """
    _limit(resource.RLIMIT_CORE, 0)
    _limit(resource.RLIMIT_CPU, math.ceil(timeout_seconds) + 1)
    _limit(resource.RLIMIT_AS, max_memory_mib * 1024 * 1024)


def _limit(which: int, value: int) -> None:
    with contextlib.suppress(ValueError, OSError):
        resource.setrlimit(which, (value, value))


def answer_for(request: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """Run the requested extractor on the path and shape the answer."""
    name = request.get("extractor")
    path = request.get("path")
    recorded = request.get("recorded_media_type")
    if not isinstance(name, str) or not isinstance(path, str) or not isinstance(recorded, str):
        return _answer(ExtractionResult.failed(PROBLEM_ERROR))
    extractor = registry.get(name)
    if extractor is None:
        return _answer(ExtractionResult.failed(PROBLEM_UNKNOWN_EXTRACTOR))
    try:
        result = extractor.run(RunInput(Path(path), recorded))
    except MemoryError:
        return _answer(ExtractionResult.failed(PROBLEM_MEMORY))
    except OSError as error:
        return {"status": "transient", "detail": f"{type(error).__name__}: {error.strerror}"}
    except Exception:
        return _answer(ExtractionResult.failed(PROBLEM_ERROR))
    return _answer(result)


def _answer(result: ExtractionResult) -> dict[str, JsonValue]:
    return {
        "status": result.status,
        "problem": result.problem,
        "values": dict(result.values),
        "parts": [{"name": part.name, "values": dict(part.values)} for part in result.parts],
    }


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
        timeout = float(request["timeout_seconds"])
        memory = int(request["max_memory_mib"])
    except (ValueError, KeyError, TypeError):
        return 2
    apply_limits(timeout, memory)
    try:
        answer = answer_for(request)
        text = json.dumps(answer, separators=(",", ":"))
    except MemoryError:
        text = json.dumps(_answer(ExtractionResult.failed(PROBLEM_MEMORY)))
    except (TypeError, ValueError):
        text = json.dumps(_answer(ExtractionResult.failed(PROBLEM_ERROR)))
    if len(text) > MAX_ANSWER_BYTES:
        text = json.dumps(_answer(ExtractionResult.failed(PROBLEM_OUTPUT)))
    sys.stdout.write(text)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
