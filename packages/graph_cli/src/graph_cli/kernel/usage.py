"""What a command noted for its access log line beyond the envelope (contracts B5.10).

Owned per command like ``Timings``: services write it while they work, the
catalog reads it when the line is written. The envelope is not the carrier, so
the answer stays byte-identical whether or not the access log is on.

It holds only model terms, placeholders, hashes and function names (B5.10),
never a value: no literal, no parameter value, no entity id.
"""

from __future__ import annotations

from graph_cli.kernel.canonical import JsonValue


class UsageNote:
    def __init__(self) -> None:
        # The access log's ``query`` value, already in log form: the shape of an ad-hoc
        # Cypher query and the functions that cover it. ``None``: nothing was noted.
        self.query: dict[str, JsonValue] | None = None
