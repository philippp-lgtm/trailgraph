"""How reads show entities, edges and query values as JSON."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Final

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.clock import format_timestamp
from graph_cli.kernel.ids import EdgeId, EntityId
from graph_cli.kernel.records import EdgeRecord, Revision
from graph_cli.query.cypher.values import Node, Rel, Value, plain_json
from graph_cli.query.plan import ALWAYS_SHOWN

# An entity's validity, shown by default only when set; the other system fields only with
# ``full`` or by name in ``fields``.
VALIDITY: Final = ("valid_from", "valid_to")


def entity_json(
    revision: Revision,
    fields: Sequence[str] = (),
    *,
    full: bool = False,
    names_mapped: bool = False,
) -> dict[str, JsonValue]:
    """An entity as reads show it. By default its content: the always shown keys, ``attrs``,
    ``edges`` without their ids, and ``valid_from``/``valid_to`` when set. With ``full``
    every revision field and edge id, explicit nulls (C.6 envelope rule), and
    ``names_mapped: true`` for a past revision shown under the pin's names (migration plan
    M7; the key is absent otherwise).

    With ``fields`` (checked by ``query.plan.check_fields``) only the always shown
    keys, ``attrs`` with the named attributes (null when absent) and the named
    entity parts and system fields.
    """
    whole = _entity_json(revision, edge_ids=full)
    if fields:
        shown: dict[str, JsonValue] = {key: whole[key] for key in ALWAYS_SHOWN}
        shown["attrs"] = {n: revision.attrs.get(n) for n in fields if n not in whole}
        shown.update({n: whole[n] for n in fields if n in whole and n not in ALWAYS_SHOWN})
        return shown
    if full:
        if names_mapped:
            whole["names_mapped"] = True
        return whole
    lean = {key: whole[key] for key in (*ALWAYS_SHOWN, "attrs", "edges")}
    lean.update({key: whole[key] for key in VALIDITY if whole[key] is not None})
    return lean


def edge_json(edge: EdgeRecord, *, full: bool = False) -> dict[str, JsonValue]:
    """An edge as reads show it: without its id unless ``full``."""
    data = edge.to_json()
    if not full:
        del data["edge_id"]
    return data


def _entity_json(revision: Revision, *, edge_ids: bool) -> dict[str, JsonValue]:
    return {
        "id": str(revision.id),
        "alias": revision.alias,
        "type": revision.type,
        "rev": revision.rev,
        "record_status": revision.record_status,
        "attrs": dict(revision.attrs),
        "edges": [edge_json(edge, full=edge_ids) for edge in revision.edges],
        "valid_from": revision.valid_from,
        "valid_to": revision.valid_to,
        "created_at": format_timestamp(revision.created_at),
        "recorded_at": format_timestamp(revision.recorded_at),
        "tx_seq": revision.tx_seq,
        "ontology_version": revision.ontology_version,
        "content_hash": revision.content_hash,
    }


def value_json(value: Value) -> JsonValue:
    """A query value as reads show it: an entity as ``get`` does, an edge with its source."""
    if isinstance(value, Node):
        return entity_json(value.revision)
    if isinstance(value, Rel):
        return {**value.edge.to_json(), "source": str(value.source)}
    if isinstance(value, list):
        return [value_json(v) for v in value]
    if isinstance(value, dict):
        return {k: value_json(v) for k, v in value.items()}
    return plain_json(value)


def entities_in(rows: Sequence[Sequence[Value]]) -> list[EntityId]:
    """The entities rows show (nodes, and both ends of relationships), first seen first."""
    found: dict[EntityId, None] = {}
    for value in _graph_values(rows):
        if isinstance(value, Node):
            found.setdefault(value.id)
        else:
            found.setdefault(value.source)
            found.setdefault(value.target)
    return list(found)


def edges_in(rows: Sequence[Sequence[Value]]) -> list[EdgeId]:
    """The edges rows show as relationships, first seen first."""
    return list(dict.fromkeys(v.edge.edge_id for v in _graph_values(rows) if isinstance(v, Rel)))


def _graph_values(rows: Sequence[Sequence[Value]]) -> Iterator[Node | Rel]:
    """Every node and relationship in rows, also inside lists and maps, in order."""

    def walk(value: Value) -> Iterator[Node | Rel]:
        if isinstance(value, Node | Rel):
            yield value
        elif isinstance(value, list):
            for item in value:
                yield from walk(item)
        elif isinstance(value, dict):
            for item in value.values():
                yield from walk(item)

    for row in rows:
        for value in row:
            yield from walk(value)
