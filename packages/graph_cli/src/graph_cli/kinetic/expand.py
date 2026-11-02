"""Running an action on a snapshot: preconditions and template expansion (actions plan A4).

Both functions are pure over a ``Snapshot``; the caller (the write service, under
the graph lock) provides the snapshot, the bound parameters and the final reason.

``check_preconditions`` evaluates every precondition of the action on the current
revision of the entity its ``on`` parameter names and refuses with every failure
in one answer. ``expand`` turns the template into a change set: it substitutes
the bound values, drops the entries of optional parameters that were not given,
and replaces each ``unlink ... all: true`` by one exact unlink per current edge.
``bound`` holds parameter values as ``kinetic.params`` binds them; a ``ref``
parameter is already resolved to the full entity id (text).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.changeset import ChangeSet, parse_changeset
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, TermRef, parse_id
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.records import EdgeRecord
from graph_cli.kinetic.action_entries import ActionEntry
from graph_cli.kinetic.action_format import (
    ABSENT,
    Absent,
    Use,
    is_dynamic_unlink,
    scan_op,
    substitute,
)
from graph_cli.kinetic.params import json_value, text_value
from graph_cli.ontology_layer.evaluate import evaluate, explain
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.query.cypher.values import Value

ACTIVE: Final = "active"


def check_preconditions(
    entry: ActionEntry, bound: Mapping[str, Value], snapshot: Snapshot, model: CompiledModel
) -> None:
    """Raise ``action.precondition_failed`` when any precondition does not hold.

    ``details``: ``{"action": name, "failed": [{"precondition", "parameter", "entity",
    "summary", "actual"}]}``; ``actual`` is the operand values of the aggregates in the
    condition (``explain``), ``None`` for a condition without any.
    """
    failed: list[JsonValue] = []
    summaries: list[str] = []
    for pre in entry.preconditions:
        entity = _entity(entry, bound, pre.on)
        revision = snapshot.get(entity)
        if revision is None:
            raise GraphCliError(
                "internal.error",
                f"precondition {pre.id!r} of {entry.name}: no entity {entity}",
            )
        if evaluate(pre.expr, revision, snapshot, model):
            continue
        actual = explain(pre.expr, revision, snapshot, model)
        summaries.append(f"{pre.id}: {pre.summary}")
        failed.append(
            {
                "precondition": pre.id,
                "parameter": pre.on,
                "entity": str(entity),
                "summary": pre.summary,
                "actual": actual,
            }
        )
    if failed:
        listed = "; ".join(summaries)
        raise GraphCliError(
            "action.precondition_failed",
            f"{entry.name} does not apply to the current state: {listed}",
            details={"action": entry.name, "failed": failed},
        )


def expand(
    entry: ActionEntry,
    bound: Mapping[str, Value],
    snapshot: Snapshot,
    *,
    reason: str | None = None,
    idempotency_key: str | None = None,
) -> ChangeSet:
    """The change set the action writes for ``bound`` in the state of ``snapshot``.

    ``action.reason_required`` without a ``reason`` when the action demands one;
    ``action.nothing_to_do`` when the expansion has no op at all (every op was an
    ``all: true`` unlink that found no edge); a template that does not assemble is
    refused as ``parse_changeset`` does (``changeset.malformed``, a defect of the
    action, which ``compile_actions`` checks).
    """
    if entry.defn.reason == "required" and not (reason and reason.strip()):
        raise GraphCliError(
            "action.reason_required",
            f"{entry.name} needs a reason; give one with --reason",
            details={"action": entry.name},
        )
    params = {p.name: p for p in entry.defn.params}

    def value_of(use: Use) -> JsonValue | Absent:
        if use.name not in bound:
            if params[use.name].optional:
                return ABSENT
            raise GraphCliError(
                "internal.error", f"{entry.name}: no value for parameter {use.name!r}"
            )
        value = bound[use.name]
        if use.text:
            return [text_value(v) for v in value] if isinstance(value, list) else text_value(value)
        return json_value(value)

    ops: list[JsonValue] = []
    for index, template in enumerate(entry.ops):
        instance = substitute(template, scan_op(template, index).uses, value_of)
        if is_dynamic_unlink(instance):
            ops.extend(_unlinks(instance, snapshot))
        else:
            ops.append(instance)
    if not ops:
        raise GraphCliError(
            "action.nothing_to_do",
            f"{entry.name} has nothing to do: it only removes edges and there are none",
            details={"action": entry.name},
        )
    document: dict[str, JsonValue] = {"ops": ops}
    if reason is not None:
        document["reason"] = reason
    if idempotency_key is not None:
        document["idempotency_key"] = idempotency_key
    return parse_changeset(document)


def _entity(entry: ActionEntry, bound: Mapping[str, Value], name: str) -> EntityId:
    value = bound.get(name)
    try:
        if not isinstance(value, str):
            raise GraphCliError("internal.error", "not a reference")
        return EntityId(parse_id(value))
    except GraphCliError:
        raise GraphCliError(
            "internal.error",
            f"{entry.name}: parameter {name!r} must be resolved to a full entity id, got {value!r}",
        ) from None


def _unlinks(instance: Mapping[str, JsonValue], snapshot: Snapshot) -> list[JsonValue]:
    """One exact unlink (``edge_id`` at the entity the edge belongs to) per active edge of the
    relation at the entity: its own edges (``out``) or those pointing at it (``in``)."""
    entity = EntityId(parse_id(str(instance["id"])))
    rel = TermRef(str(instance["rel"]))
    revision = snapshot.get(entity)
    if revision is None:
        raise GraphCliError("internal.error", f"unlink all: no entity {entity}")
    owned: list[tuple[EntityId, EdgeRecord]]
    if instance.get("direction", "out") == "in":
        owned = []
        for source, edge in snapshot.incoming(entity, rel):
            found = snapshot.get(source)
            if found is not None and found.record_status == ACTIVE:
                owned.append((source, edge))
    else:
        owned = [(entity, edge) for edge in revision.edges if edge.rel == rel]
    owned.sort(key=lambda pair: (str(pair[0]), str(pair[1].edge_id)))
    return [
        {"op": "unlink", "id": str(owner), "edge_id": str(edge.edge_id)} for owner, edge in owned
    ]
