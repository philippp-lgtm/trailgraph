"""Activity port: a transient signal that entities were read (graph view plan, Q3).

Nothing is stored. An adapter may drop any signal and never raises, so a
read never fails or waits because of it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Protocol

from graph_cli.kernel.ids import EntityId, GraphId


class ActivitySink(Protocol):
    def reads(self, graph: GraphId, ids: Sequence[EntityId]) -> None: ...


class NullActivity:
    """The default: reads signal nothing (``[activity] signal_reads = false``)."""

    def reads(self, graph: GraphId, ids: Sequence[EntityId]) -> None:
        return None


NULL_ACTIVITY: Final = NullActivity()
