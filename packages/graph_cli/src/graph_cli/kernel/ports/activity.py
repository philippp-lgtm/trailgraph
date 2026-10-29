"""Activity port: a transient signal that entities were read (graph view plan, Q3).

Nothing is stored. An adapter may drop any signal and never raises, so a
read never fails or waits because of it.

A signal names one successful read command: which command, who ran it, the
entities it returned and the edges it walked (``neighbors``, relationships
in ``query`` rows). A ``run`` also names its function. The adapter decides how
much of the actor leaves the process.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.ids import EdgeId, EntityId, GraphId


@dataclass(frozen=True, slots=True)
class ReadSignal:
    graph: GraphId
    command: str  # the request's command name, as in the audit and the access log
    actor: ActorContext
    entities: Sequence[EntityId]
    edges: Sequence[EdgeId] = ()
    function: str | None = None  # the name of the function a ``run`` executed, never its values


class ActivitySink(Protocol):
    def reads(self, signal: ReadSignal) -> None: ...


class NullActivity:
    """The default: reads signal nothing (``[activity] signal_reads = false``)."""

    def reads(self, signal: ReadSignal) -> None:
        return None


NULL_ACTIVITY: Final = NullActivity()
