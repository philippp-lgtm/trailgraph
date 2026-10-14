"""Audit draft handling shared by every backend (contracts B5.3, B5.7; plan Q2).

Storage treats the draft body as opaque JSON built by ``graph_cli.audit``. It
only requires ``event_id``, refuses bodies that already carry chain keys or a
different ``stream``, and sets ``stream`` before sealing.
"""

from __future__ import annotations

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EventId, parse_id
from graph_cli.kernel.ports.audit import AuditEventDraft, AuditStream
from graph_cli.storage.common.chain import CHAIN_KEYS


def _refused(message: str) -> GraphCliError:
    return GraphCliError("internal.error", f"audit draft refused: {message}")


def audit_body(draft: AuditEventDraft, stream: AuditStream) -> tuple[dict[str, JsonValue], EventId]:
    """The body to seal into ``stream`` and its ``event_id``."""
    if draft.stream != stream:
        raise _refused(f"draft is for stream {draft.stream!r}, not {stream!r}")
    body = dict(draft.body)
    clash = [key for key in CHAIN_KEYS if key in body]
    if clash:
        raise _refused(f"body carries keys assigned by storage: {clash}")
    if body.setdefault("stream", stream) != stream:
        raise _refused("body stream differs from the draft stream")
    event_id = body.get("event_id")
    if not isinstance(event_id, str):
        raise _refused("body has no event_id")
    try:
        parsed = parse_id(event_id)
    except GraphCliError:
        raise _refused("event_id is not a UUID") from None
    if str(parsed) != event_id:
        raise _refused("event_id is not in canonical form")
    return body, EventId(parsed)
