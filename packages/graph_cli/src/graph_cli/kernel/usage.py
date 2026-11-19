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
        # The graph a read opened (``{id, name, seq}``): names the graph of a line whose
        # envelope has none, an error after the graph was open (a failed ``run``).
        self.graph: dict[str, JsonValue] | None = None
        # The function a ``run`` found (``{name, hash}``), for a run that fails afterwards
        # with an error that does not name it (a ``ref`` parameter that finds nothing).
        self.function: dict[str, JsonValue] | None = None
        # The authored view a ``view`` call found (``{name, hash}``), for a call that fails
        # afterwards with an error that does not name it. Never a parameter value or an id.
        self.view: dict[str, JsonValue] | None = None
