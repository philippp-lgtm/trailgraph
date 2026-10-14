"""Real audit events for cross-component tests (P1 item 4).

Storage tests keep ``support.drafts`` (storage treats bodies as opaque).
Tests that verify streams need events built by ``graph_cli.audit``; this
module supplies the inputs the services will supply in item 6: allow
decisions per op, the graph entry, the env and the before hashes.
"""

import dataclasses
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Final

from graph_cli.audit import events
from graph_cli.kernel.actions import Action
from graph_cli.kernel.actor import ActorContext, ClientInfo, Entrypoint
from graph_cli.kernel.authz import AuthorizedTx, Decision
from graph_cli.kernel.canonical import hash_value, jsonl_line
from graph_cli.kernel.ids import EntityId, EventId, GraphId
from graph_cli.kernel.json_access import as_object, req_str
from graph_cli.kernel.ports.audit import AuditEventDraft
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.records import EnvInfo, OntologyPin, RequestMeta
from graph_cli.storage.common.chain import GENESIS, ChainLink, link_of, seal
from support.txbuilder import actor

POLICY_HASH: Final = hash_value({"policy": "test"})
ENV: Final = EnvInfo(data_root="<data root>", root_source="default")
REQUEST: Final = RequestMeta("test", hash_value({"args": []}))
_OP_ACTIONS: Final = {
    "create": "write.create",
    "update": "write.update",
    "link": "write.link",
    "unlink": "write.unlink",
    "supersede": "write.supersede",
    "retract": "write.retract",
    "set_alias": "write.alias",
}


def graph_entry(graph_id: GraphId, pin: OntologyPin, name: str = "example_graph") -> GraphEntry:
    return GraphEntry(graph_id, name, "active", pin, {}, ())


def allow(action: str, resource: str) -> Decision:
    return Decision("allow", Action(action), resource, "test.allow", POLICY_HASH)


def deny(action: str, resource: str, code: str = "access.denied") -> Decision:
    return Decision("deny", Action(action), resource, "test.deny", POLICY_HASH, code=code)


def authorized(tx: AuthorizedTx) -> AuthorizedTx:
    """``tx`` with one allow decision per op action, as ``services.write`` will attach."""
    ops = tx.changeset.get("ops", [])
    assert isinstance(ops, list)
    actions = sorted({_OP_ACTIONS[req_str(as_object(op, "op"), "op")] for op in ops})
    resource = f"graph:{tx.graph_id}"
    return dataclasses.replace(tx, decisions=tuple(allow(a, resource) for a in actions))


def before_hashes(snapshot: Snapshot, tx: AuthorizedTx) -> dict[EntityId, str]:
    hashes = {}
    for revision in tx.revisions:
        if revision.rev > 1:
            previous = snapshot.get(revision.id)
            assert previous is not None
            hashes[revision.id] = previous.content_hash
    return hashes


def intent(
    tx: AuthorizedTx, *, before: Mapping[EntityId, str] | None = None, name: str = "example_graph"
) -> AuditEventDraft:
    """The real write intent for a ``TxBuilder`` tx."""
    return events.write_intent(
        authorized(tx), graph=graph_entry(tx.graph_id, tx.ontology, name), env=ENV, before=before
    )


def denial(event_id: EventId, ts: datetime, *, principal: str = "test:1") -> AuditEventDraft:
    """An app request for a maintenance action, refused by the ceiling."""
    return events.refusal(
        status="denied",
        code="access.ceiling",
        actor=actor(principal),
        decisions=[deny("maint.repair", "installation", "access.ceiling")],
        request=REQUEST,
        ts=ts,
        event_id=event_id,
        env=ENV,
    )


def maint_actor() -> ActorContext:
    return dataclasses.replace(actor(), client=ClientInfo("graph-admin", "0.1.0", Entrypoint.MAINT))


def sealed_lines(drafts: Sequence[AuditEventDraft], start: ChainLink = GENESIS) -> list[bytes]:
    """Chain drafts of one stream the way storage does, for pure verifier tests."""
    lines, link = [], start
    for draft in drafts:
        body = dict(draft.body)
        document, line = seal(body, link)
        assert line == jsonl_line(document)
        link = link_of(document)
        lines.append(line)
    return lines
