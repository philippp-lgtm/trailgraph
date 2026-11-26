"""What publishing does to the entries a catalog grant names (catalog callers plan CA7, Q8).

A catalog grant (``level: catalog`` or ``catalog.actions`` on a read grant) names functions,
actions and views of one graph. It names them, not their content: whoever may publish the
graph's catalog decides what a granted name reads or writes. A publish therefore warns, never
refuses, when it touches an entry an installed grant names, so a maintainer sees what the
publish does to the tokens behind the grant:

- ``catalog.granted_entry_removed``: the published catalog had the entry and the new one has
  not. The grant now gives nothing; a name the old entry gave as ``replaced_by`` is named in
  the warning but never inherits the grant (a successor may read and write otherwise), so the
  policy has to name it;
- ``catalog.granted_entry_changed``: the entry stays but its hash changes (the sources, the
  model it resolves against or its status). The callers of the grants run the new definition.

Pure over the installed policy and two slots; ``graph functions|actions|views publish`` and
``graph push`` call it with the published slot and the one they would publish, also on a dry
run. An entry the catalog did not hold before is not warned about (the grant gave nothing
and ``policy.entry_unknown`` said so). ``published_entries`` and ``published_hash`` read what
the graph publishes now for ``policy explain``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from graph_cli.access_control.terms import EntryGrant, entry_grants
from graph_cli.kernel.canonical import JsonObject, JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.json_access import JsonShapeError, as_object
from graph_cli.kernel.ports.catalog import GraphEntry, KineticSlot, SlotKind
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli_admin.services_admin.context import AdminContext

KIND_OF_SLOT: Final[Mapping[SlotKind, str]] = {
    "functions": "function",
    "actions": "action",
    "views": "view",
}


def warnings(
    policy: PolicySet,
    entry: GraphEntry,
    kind: SlotKind,
    current: KineticSlot | None,
    new: KineticSlot | None,
) -> tuple[WarningRecord, ...]:
    """The warnings of replacing the published ``kind`` catalog ``current`` (``None``: nothing
    published) by ``new`` (``None``: nothing published afterwards) for the grants of
    ``policy``, sorted by entry name. Nothing is judged for a name no grant of the graph
    names."""
    singular = KIND_OF_SLOT[kind]
    granted = entry_grants(policy.documents, entry.id)[singular]
    if not granted:
        return ()
    before, after = _entries(current), _entries(new)
    found: list[WarningRecord] = []
    for name, grants in granted.items():
        old = before.get(name)
        if old is None:
            continue
        now = after.get(name)
        if now is None:
            found.append(_removed(entry, singular, name, grants, old))
        elif now.get("hash") != old.get("hash"):
            found.append(_changed(entry, singular, name, grants, old, now))
    return tuple(found)


def published_entries(
    ctx: AdminContext, graph: GraphEntry, kind: SlotKind
) -> dict[str, JsonObject]:
    """The entries the graph's published ``kind`` catalog holds, by name; a catalog that was
    never published, or that cannot be read, holds none."""
    try:
        return _entries(_read(ctx, graph, kind))
    except (GraphCliError, JsonShapeError):
        return {}


def _read(ctx: AdminContext, graph: GraphEntry, kind: SlotKind) -> KineticSlot | None:
    if kind == "functions":
        return ctx.catalog.read_functions(graph.id)
    if kind == "actions":
        return ctx.catalog.read_actions(graph.id)
    return ctx.catalog.read_views(graph.id)


def _entries(slot: KineticSlot | None) -> dict[str, JsonObject]:
    if slot is None:
        return {}
    items = slot.compiled.get(slot.spec.list_key)
    found: dict[str, JsonObject] = {}
    for item in items if isinstance(items, list) else ():
        row = as_object(item, slot.spec.list_key)
        name = row.get("name")
        if isinstance(name, str):
            found[name] = row
    return found


def _who(grants: Sequence[EntryGrant]) -> str:
    return ", ".join(
        g.grant + (f" ({', '.join(f'{k} {v}' for k, v in g.principal)})" if g.principal else "")
        for g in grants
    )


def _details(
    entry: GraphEntry, kind: str, name: str, grants: Sequence[EntryGrant]
) -> dict[str, JsonValue]:
    return {
        "graph": entry.name,
        "kind": kind,
        "name": name,
        "grants": [{"grant": g.grant, "principal": dict(g.principal)} for g in grants],
    }


def _removed(
    entry: GraphEntry, kind: str, name: str, grants: Sequence[EntryGrant], old: JsonObject
) -> WarningRecord:
    successor = old.get("replaced_by")
    successor = successor if isinstance(successor, str) else None
    follow = (
        f"; it names {successor} as its replacement, which the grant does not cover (a grant"
        " names entries, a replacement never inherits it): name it in the policy to give the"
        " callers its successor"
        if successor is not None
        else "; the callers lose it (name another entry in the policy to give them one)"
    )
    return WarningRecord(
        "catalog.granted_entry_removed",
        f"the publish removes {kind} {name} from graph {entry.name}, which {_who(grants)}"
        f" names; the grant gives nothing once it is gone{follow}",
        {**_details(entry, kind, name, grants), "replaced_by": successor},
    )


def _changed(
    entry: GraphEntry,
    kind: str,
    name: str,
    grants: Sequence[EntryGrant],
    old: JsonObject,
    new: JsonObject,
) -> WarningRecord:
    return WarningRecord(
        "catalog.granted_entry_changed",
        f"the publish changes {kind} {name} of graph {entry.name}, which {_who(grants)} names:"
        " the callers of the grant run the new definition, and what it reads or writes is the"
        " new definition's, not the one the grant was written for; check it before the callers"
        " use it",
        {
            **_details(entry, kind, name, grants),
            "hash_before": old.get("hash"),
            "hash_after": new.get("hash"),
        },
    )
