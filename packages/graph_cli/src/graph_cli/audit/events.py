"""Builders for ``graph_cli.audit/1`` events (contracts B5.2, B5.4-B5.8, B5.11).

Pure: every input comes from the caller, nothing is read from the clock, the
environment or storage. A builder returns an ``AuditEventDraft`` routed to its
stream (B5.2); storage adds ``seq``, ``prev_hash`` and ``hash``. Events carry
ids, types, hashes and reason text only, never attribute values (B5.8).
Every builder checks its own output against ``audit.schema``; a violation is
``internal.error``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, Protocol
from uuid import UUID

from graph_cli.audit.schema import (
    AUDIT_SCHEMA,
    INSTALLATION,
    MAX_RESULT_FINDINGS,
    event_problems,
    is_ext_state_action,
)
from graph_cli.kernel.actions import Action, require_known_action
from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.authz import AuthorizedTx, Decision
from graph_cli.kernel.canonical import JsonValue, canonical_bytes
from graph_cli.kernel.clock import format_timestamp
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.findings import Finding, sort_findings
from graph_cli.kernel.ids import EntityId, EventId, format_id
from graph_cli.kernel.ports.audit import (
    INSTALLATION_STREAM,
    AuditEventDraft,
    AuditStream,
    graph_stream,
)
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.records import BlobInfo, EnvInfo, OntologyPin, RequestMeta

RefusalStatus = Literal["denied", "invalid", "conflict", "error"]


class Anchorable(Protocol):
    """A position in a graph stream: an ``AuditReceipt`` or a verified stream head."""

    @property
    def stream(self) -> AuditStream: ...

    @property
    def seq(self) -> int: ...

    @property
    def hash(self) -> str: ...


@dataclass(frozen=True, slots=True)
class StreamHead:
    """The last event of a graph stream, for ``audit verify`` and ``audit anchor``."""

    stream: AuditStream
    seq: int
    hash: str


# B5.2: installation-scoped maintenance; every other maintenance action with an
# event is graph-scoped (graph stream, then an installation event with an anchor).
INSTALLATION_MAINT: Final = frozenset(
    Action(a)
    for a in (
        "maint.graph_create",
        "maint.policy_change",
        "maint.audit_verify",
        "maint.audit_anchor",
    )
)
# B5.15: a policy change is installation-scoped for the installation policy and
# graph-scoped (W.e) for a graph's own policy.
_EITHER_SCOPE: Final = frozenset({Action("maint.policy_change")})
# B5.10: reads leave no chained event.
_NO_EVENT: Final = frozenset({Action("maint.audit_read")})


def write_intent(
    tx: AuthorizedTx,
    *,
    graph: GraphEntry,
    env: EnvInfo,
    before: Mapping[EntityId, str] | None = None,
    blobs: Sequence[BlobInfo] = (),
) -> AuditEventDraft:
    """The intent appended before ``tx`` commits (W.12). ``before`` maps every updated
    entity (``rev > 1``) to the content hash of its previous revision; ``blobs`` are the
    files the request stored (W.11b), named by hash and size only (B5.8).
    ``changeset_size`` is the byte length of the canonical changeset, the form
    ``changeset_hash`` covers."""
    if graph.id != tx.graph_id:
        raise _internal("the graph entry is not the tx's graph")
    entities: list[JsonValue] = []
    for revision in sorted(tx.revisions, key=lambda r: r.id):
        entry: dict[str, JsonValue] = {
            "id": str(revision.id),
            "rev": revision.rev,
            "after": revision.content_hash,
        }
        if revision.rev > 1:
            if before is None or revision.id not in before:
                raise _internal(f"no before hash for updated entity {revision.id}")
            entry["before"] = before[revision.id]
        entities.append(entry)
    body = _body(
        stream=graph_stream(str(tx.graph_id)),
        event_id=tx.event_id,
        ts=tx.recorded_at,
        actor=tx.actor,
        decisions=tx.decisions,
        request=tx.request,
        env=env,
        result={"status": "ok"},
        graph=_graph(graph, tx.ontology),
        reason=tx.reason,
        target={
            "entities": [str(r.id) for r in sorted(tx.revisions, key=lambda r: r.id)],
            "types": sorted({r.type for r in tx.revisions}),
        },
    )
    change: dict[str, JsonValue] = {
        "tx_id": str(tx.tx_id),
        "tx_seq": tx.seq,
        "changeset_hash": tx.changeset_hash,
        "changeset_size": changeset_size(tx.changeset),
        "entities": entities,
    }
    if blobs:
        change["blobs"] = blob_list(blobs)
    body["change"] = change
    return _checked(body)


def changeset_size(changeset: Mapping[str, JsonValue]) -> int:
    """Bytes of the canonical changeset (C.2 form): what the request wrote, without files."""
    return len(canonical_bytes(dict(changeset)))


def blobs_removed(blobs: Sequence[BlobInfo], staging: int) -> dict[str, JsonValue]:
    """``change`` of a ``maint.repair`` intent that removes stored files (``files gc``)."""
    return {"blobs_removed": blob_list(blobs) if blobs else [], "staging_removed": staging}


def blob_list(blobs: Sequence[BlobInfo]) -> list[JsonValue]:
    """``[{hash, size}]`` by hash, each once: what an event may say about stored files."""
    unique = {b.hash: b.size for b in blobs}
    return [{"hash": h, "size": unique[h]} for h in sorted(unique)]


def refusal(
    *,
    status: RefusalStatus,
    code: str,
    actor: ActorContext,
    decisions: Sequence[Decision],
    request: RequestMeta,
    ts: datetime,
    event_id: EventId,
    env: EnvInfo,
    graph: GraphEntry | None = None,
    findings: Iterable[Finding] = (),
    reason: str | None = None,
    target: Mapping[str, Sequence[str]] | None = None,
) -> AuditEventDraft:
    """A refused request (B5.2): ``denied`` goes to the installation stream, the
    others to the graph stream. ``invalid`` keeps the first 20 findings (B5.8)."""
    if status == "denied" or graph is None:
        if status in ("invalid", "conflict"):
            raise _internal(f"a {status} event needs its graph")
        stream = INSTALLATION_STREAM
    else:
        stream = graph_stream(str(graph.id))
    result: dict[str, JsonValue] = {"status": status, "code": code}
    if status == "invalid":
        result["findings"] = [
            {"code": f.code, "target": dict(f.target)}
            for f in sort_findings(findings)[:MAX_RESULT_FINDINGS]
        ]
    body = _body(
        stream=stream,
        event_id=event_id,
        ts=ts,
        actor=actor,
        decisions=decisions,
        request=request,
        env=env,
        result=result,
        graph=_graph(graph) if graph is not None else None,
        reason=reason,
        target=target,
    )
    return _checked(body)


def maintenance(
    *,
    actor: ActorContext,
    decisions: Sequence[Decision],
    request: RequestMeta,
    ts: datetime,
    event_id: EventId,
    env: EnvInfo,
    graph: GraphEntry | None = None,
    change: Mapping[str, JsonValue] | None = None,
    reason: str | None = None,
    target: Mapping[str, Sequence[str]] | None = None,
    anchors: Sequence[Anchorable] = (),
) -> AuditEventDraft:
    """An ``ok`` maintenance event, routed by its actions (B5.2).

    Graph-scoped actions go to ``graph``'s stream; the caller then appends an
    installation event for the same action carrying the anchor of the graph
    event (``anchors``). Installation-scoped actions may carry anchors directly.
    ``maint.policy_change`` is graph-scoped when ``graph`` is given (a graph's own
    policy), else installation-scoped.
    """
    actions = _actions(decisions)
    if not actions or not all(a.startswith("maint.") for a in actions):
        raise _internal("maintenance events carry maint.* actions only")
    if actions & _NO_EVENT:
        raise _internal("maint.audit_read is a read and leaves no chained event (B5.10)")
    graph_policy = graph is not None and actions <= _EITHER_SCOPE
    if (actions <= INSTALLATION_MAINT and not graph_policy) or anchors:
        stream = INSTALLATION_STREAM
    elif graph is None:
        raise _internal("a graph-scoped maintenance event needs its graph")
    else:
        stream = graph_stream(str(graph.id))
    body = _body(
        stream=stream,
        event_id=event_id,
        ts=ts,
        actor=actor,
        decisions=decisions,
        request=request,
        env=env,
        result={"status": "ok"},
        graph=_graph(graph) if graph is not None else None,
        reason=reason,
        target=target,
    )
    if change is not None:
        body["change"] = dict(change)
    if anchors:
        body["anchors"] = [anchor(receipt) for receipt in anchors]
    return _checked(body)


def graph_genesis(
    *,
    actor: ActorContext,
    decisions: Sequence[Decision],
    request: RequestMeta,
    ts: datetime,
    event_id: EventId,
    env: EnvInfo,
    graph: GraphEntry,
) -> AuditEventDraft:
    """Seq 1 of a new graph stream (``maint.graph_create``, W.f)."""
    if _actions(decisions) != {Action("maint.graph_create")}:
        raise _internal("a genesis event carries maint.graph_create only")
    body = _body(
        stream=graph_stream(str(graph.id)),
        event_id=event_id,
        ts=ts,
        actor=actor,
        decisions=decisions,
        request=request,
        env=env,
        result={"status": "ok"},
        graph=_graph(graph),
        reason=None,
        target=None,
    )
    return _checked(body)


def release_change(version: str, artifact_hash: str, release_id: UUID) -> dict[str, JsonValue]:
    """``change`` of a ``maint.ontology_release`` intent: the release it installs."""
    return {
        "release": {
            "version": version,
            "artifact_hash": artifact_hash,
            "release_id": format_id(release_id),
        }
    }


def pin_change(old: OntologyPin, new: OntologyPin) -> dict[str, JsonValue]:
    """``change`` of a ``maint.ontology_pin`` intent: the manifest pin before and after.

    ``from`` is read under the graph lock, so consecutive pin intents chain
    (``audit.verify`` walks them against the manifest, B5.12).
    """
    return {"pin": {"from": old.to_json(), "to": new.to_json()}}


def skill_change(old_hash: str | None, new_hash: str) -> dict[str, JsonValue]:
    """``change`` of a ``maint.graph_configure`` intent that publishes the graph skill
    (D15 Q2): the tree hash before (``None`` for the first publish) and after."""
    return {"skill": {"from": old_hash, "to": new_hash}}


def policy_change(old_hash: str | None, new_hash: str) -> dict[str, JsonValue]:
    """``change`` of a ``maint.policy_change`` intent (B5.15): the compiled policy's hash
    before (``None`` when none was installed) and after."""
    return {"policy": {"from": old_hash, "to": new_hash}}


def installation_policy_change(old_hash: str | None, new_hash: str) -> dict[str, JsonValue]:
    """``change`` of the mirror of an installation policy change in a graph stream (B5.15):
    from then on, that graph's decisions are made under ``new_hash``."""
    return {"installation_policy": {"from": old_hash, "to": new_hash}}


def anchor(receipt: Anchorable) -> dict[str, JsonValue]:
    """``{graph_id, seq, hash}`` of an appended graph-stream event (B5.11)."""
    graph_id = receipt.stream.removeprefix("graph:")
    if receipt.stream == INSTALLATION or graph_id == receipt.stream:
        raise _internal("anchors point into graph streams")
    return {"graph_id": graph_id, "seq": receipt.seq, "hash": receipt.hash}


def decision_entry(decision: Decision) -> dict[str, JsonValue]:
    """The audit form of a decision: the policy hash sits once in ``authz``."""
    entry: dict[str, JsonValue] = {
        "action": decision.action,
        "resource": decision.resource,
        "effect": decision.effect,
        "rule": decision.rule_id,
    }
    if decision.code is not None:
        entry["code"] = decision.code
    return entry


def _actions(decisions: Sequence[Decision]) -> frozenset[Action]:
    return frozenset(d.action for d in decisions)


def _body(
    *,
    stream: AuditStream,
    event_id: EventId,
    ts: datetime,
    actor: ActorContext,
    decisions: Sequence[Decision],
    request: RequestMeta,
    env: EnvInfo,
    result: dict[str, JsonValue],
    graph: dict[str, JsonValue] | None,
    reason: str | None,
    target: Mapping[str, Sequence[str]] | None,
) -> dict[str, JsonValue]:
    if not decisions:
        raise _internal("an audit event needs the decisions behind its actions")
    policy_hashes = {d.policy_hash for d in decisions}
    if len(policy_hashes) != 1:
        raise _internal("the decisions of one event come from one policy")
    for action in _actions(decisions):
        if not is_ext_state_action(action):
            require_known_action(action)
    body: dict[str, JsonValue] = {
        "schema": AUDIT_SCHEMA,
        "stream": stream,
        "event_id": str(event_id),
        "ts": format_timestamp(ts),
        "actor": actor.to_json(),
        "env": env.to_json(),
        "actions": sorted(_actions(decisions)),
        "authz": {
            "policy_hash": policy_hashes.pop(),
            "decisions": [
                decision_entry(d) for d in sorted(decisions, key=lambda d: (d.action, d.resource))
            ],
        },
        "result": result,
        "request": {"command": request.command, "args_digest": request.args_digest},
    }
    if graph is not None:
        body["graph"] = graph
    if reason is not None:
        body["reason"] = reason
    if target is not None:
        body["target"] = {key: list(values) for key, values in target.items()}
    return body


def _graph(graph: GraphEntry, pin: OntologyPin | None = None) -> dict[str, JsonValue]:
    return {
        "id": str(graph.id),
        "name": graph.name,
        "ontology": (pin or graph.pin).to_json(),
    }


def _checked(body: dict[str, JsonValue]) -> AuditEventDraft:
    problems = event_problems(body, sealed=False)
    if problems:
        raise _internal(f"audit event refused: {'; '.join(problems)}")
    stream = body["stream"]
    assert isinstance(stream, str)
    return AuditEventDraft(AuditStream(stream), body)


def _internal(message: str) -> GraphCliError:
    return GraphCliError("internal.error", message)
