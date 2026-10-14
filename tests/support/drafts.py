"""Minimal audit drafts for storage tests (plan Q2).

Storage treats the body as opaque JSON; it only needs ``event_id``. P1 item 4
replaces these with the real builders in ``graph_cli.audit``.
"""

from datetime import datetime

from graph_cli.kernel.authz import AuthorizedTx
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.clock import format_timestamp
from graph_cli.kernel.ids import EventId
from graph_cli.kernel.ports.audit import AuditEventDraft, AuditStream, graph_stream


def draft(
    stream: AuditStream,
    event_id: EventId,
    ts: datetime,
    *,
    actions: tuple[str, ...] = ("write.create",),
    status: str = "ok",
) -> AuditEventDraft:
    body: dict[str, JsonValue] = {
        "schema": "graph_cli.audit/1",
        "event_id": str(event_id),
        "ts": format_timestamp(ts),
        "actions": list(actions),
        "result": {"status": status},
    }
    return AuditEventDraft(stream, body)


def intent(tx: AuthorizedTx) -> AuditEventDraft:
    """The write-intent draft for ``tx`` in its graph stream."""
    return draft(graph_stream(str(tx.graph_id)), tx.event_id, tx.recorded_at)
