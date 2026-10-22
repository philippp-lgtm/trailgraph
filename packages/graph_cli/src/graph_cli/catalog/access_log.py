"""The access log: ``<state_root>/logs/access.jsonl`` (contracts B5.10, B12.7).

Opt-in with ``[audit] access_log``. One line per ``graph`` command that
opened the installation, reads and writes alike: when, who, which command
on which graph state, the outcome, and how many bytes the caller received
on stdout. Never a value, an argument, an entity id or a message (B5.8):
the size of a read is recorded, not what was read.

Not chained and not audited: anyone who can write the state dir can edit
it, so it is a usage record, not evidence (the audit streams are). Best
effort like the operational log: a failure to log never fails the command,
whose envelope is already written. Owner-only (0600): it names actors.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Final

from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.canonical import JsonValue, canonical_bytes
from graph_cli.kernel.clock import format_timestamp

ACCESS_SCHEMA: Final = "graph_cli.access/1"
ACCESS_FILE: Final = "access.jsonl"


def access_record(
    *,
    ts: datetime,
    actor: ActorContext,
    document: Mapping[str, JsonValue],
    json_mode: bool,
    stdout_bytes: int,
) -> dict[str, JsonValue]:
    """One line of the access log, from the envelope the command wrote (B9.2)."""
    graph = document.get("graph")
    error = document.get("error")
    result: dict[str, JsonValue] = {"status": "ok"}
    if isinstance(error, Mapping):
        result = {"status": "error", "code": error.get("code")}
    return {
        "schema": ACCESS_SCHEMA,
        "ts": format_timestamp(ts),
        "command": document.get("command"),
        "actor": actor.to_json(),
        "graph": (
            {k: graph.get(k) for k in ("id", "name", "seq")} if isinstance(graph, Mapping) else None
        ),
        "result": result,
        "output": {"format": "json" if json_mode else "text", "bytes": stdout_bytes},
    }


class AccessLogFile:
    def __init__(self, state_root: Path) -> None:
        self.path = state_root / "logs" / ACCESS_FILE

    def append(self, record: Mapping[str, JsonValue]) -> None:
        line = canonical_bytes(dict(record)) + b"\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o600)
            try:
                os.write(fd, line)  # one write per line: O_APPEND keeps lines whole
            finally:
                os.close(fd)
        except OSError:
            pass
