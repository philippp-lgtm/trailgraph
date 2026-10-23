"""Large JSON answers go to a file instead of stdout (``[output] spill_bytes``).

Agent harnesses cut or fully keep long tool output, so an answer larger than
the limit is written whole, pretty-printed for line-wise reading, to a file
the program's ``SpillTarget`` chooses. stdout then gets the same envelope
with ``data: null``, the warning ``output.spilled {path, bytes, limit}`` and,
on success, a first hint to ask for less or to read only the needed part of
the file with the agent's file tools (no command: a shell program such as
``jq`` is what restricted harnesses refuse); ``ok``,
``error``, ``page`` and the other warnings and hints stay, so the exit code
and the next page still read from stdout. When the file cannot be written
the answer is printed whole with ``output.spill_failed {bytes, limit,
reason}``: an answer is never lost. Text mode never spills.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from graph_cli.cli_kit.envelope import render_json
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import WarningRecord


class SpillTarget(Protocol):
    def limit(self) -> int:
        """Bytes of a compact JSON envelope allowed on stdout; 0 never spills."""
        ...

    def write(self, graph: str | None, command: str | None, content: bytes) -> str:
        """Store ``content`` in a new file and return its path; ``OSError`` on failure."""
        ...


@dataclass(frozen=True, slots=True)
class Rendered:
    document: Mapping[str, JsonValue]
    stdout: str
    spilled_bytes: int | None = None


def spill_json(document: Mapping[str, JsonValue], stdout: str, target: SpillTarget) -> Rendered:
    size = len(stdout.encode())
    limit = target.limit()
    if not limit or size <= limit:
        return Rendered(document, stdout)
    graph = document.get("graph")
    name = graph.get("name") if isinstance(graph, Mapping) else None
    command = document.get("command")
    content = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    warnings = document.get("warnings")
    assert isinstance(warnings, list)
    try:
        path = target.write(
            name if isinstance(name, str) else None,
            command if isinstance(command, str) else None,
            content.encode(),
        )
    except OSError as exc:
        reason = exc.strerror or str(exc)
        failed = WarningRecord(
            "output.spill_failed",
            f"the answer has {size} bytes, more than {limit}, and could not go to a file: {reason}",
            {"bytes": size, "limit": limit, "reason": reason},
        )
        whole = {**document, "warnings": [*warnings, failed.to_json()]}
        return Rendered(whole, render_json(whole))
    spilled = WarningRecord(
        "output.spilled",
        f"the answer has {size} bytes, more than {limit}; it is in {path}",
        {"path": path, "bytes": size, "limit": limit},
    )
    hints = document.get("hints")
    assert isinstance(hints, list)
    if document.get("ok"):  # an error envelope carries no hints (B9.2)
        hints = [
            {
                "text": "Ask for less (--fields, --limit, query ... RETURN), or read only "
                f"the part you need of {path} with your file read or search tool, "
                "not with a shell program",
                "command": None,
            },
            *hints,
        ]
    short = {**document, "data": None, "hints": hints, "warnings": [*warnings, spilled.to_json()]}
    return Rendered(short, render_json(short), size)
