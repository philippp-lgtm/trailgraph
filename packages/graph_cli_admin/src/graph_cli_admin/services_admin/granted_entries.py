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
  A granted view also changes when a function its ``rows`` sections run changes or is gone
  (the view names the function, never its hash, and runs whatever the graph publishes under that
  name): ``details.functions`` lists them (``row_function_warnings``).

A project admin (an admin token bound to a project, which may publish the graph's catalog but
may not read the installation policy) is told which grants name the entry (``grant.<n>``) and
the facts of the principal only for a grant bound to its own project: a grant of another
project, or one no project binds, is its id alone (``only_project``).

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
    *,
    only_project: str | None = None,
) -> tuple[WarningRecord, ...]:
    """The warnings of replacing the published ``kind`` catalog ``current`` (``None``: nothing
    published) by ``new`` (``None``: nothing published afterwards) for the grants of
    ``policy``, sorted by entry name. Nothing is judged for a name no grant of the graph
    names. ``only_project``: the project of a project admin, which sees principal facts of its
    own project's grants only."""
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
            found.append(_removed(entry, singular, name, grants, old, only_project))
        elif now.get("hash") != old.get("hash"):
            found.append(_changed(entry, singular, name, grants, old, now, only_project))
    return tuple(found)


def only_project(ctx: AdminContext) -> str | None:
    """The project whose grants the caller may see the principal of: its own, for a project
    admin; ``None`` for a maintainer, which reads the installation policy anyway."""
    return (ctx.actor.project.id or "") if ctx.project_admin else None


def row_function_warnings(
    policy: PolicySet,
    entry: GraphEntry,
    views: KineticSlot | None,
    current: KineticSlot | None,
    new: KineticSlot | None,
    *,
    skip: frozenset[str] = frozenset(),
    only_project: str | None = None,
) -> tuple[WarningRecord, ...]:
    """The granted views of ``views`` (the view catalog the graph will have) that run a function
    that ``new`` (the function catalog afterwards) changes or drops against ``current`` (the
    published one): ``catalog.granted_entry_changed`` for the view, with ``details.functions``
    (``name``, ``hash_before`` and ``hash_after``, ``null`` when the function is gone). A view
    names its row functions and runs what is published under the name, so a token of the grant
    reads what the new function reads. ``skip`` are the views already warned about."""
    granted = entry_grants(policy.documents, entry.id)["view"]
    if not granted or views is None:
        return ()
    before, after, rows = _entries(current), _entries(new), _entries(views)
    found: list[WarningRecord] = []
    for name, grants in granted.items():
        if name in skip or name not in rows:
            continue
        touches = rows[name].get("touches")
        names = touches.get("functions") if isinstance(touches, dict) else None
        changed: list[JsonValue] = []
        for function in names if isinstance(names, list) else ():
            old, now = before.get(str(function)), after.get(str(function))
            if old is None or (now is not None and now.get("hash") == old.get("hash")):
                continue
            changed.append(
                {
                    "name": str(function),
                    "hash_before": old.get("hash"),
                    "hash_after": now.get("hash") if now is not None else None,
                }
            )
        if changed:
            found.append(_rows_changed(entry, name, grants, changed, only_project))
    return tuple(found)


def published_entries(
    ctx: AdminContext, graph: GraphEntry, kind: SlotKind, *, soft: bool = False
) -> dict[str, JsonObject]:
    """The entries the graph's published ``kind`` catalog holds, by name; a catalog that was
    never published holds none. A catalog that cannot be read raises (``functions.slot_invalid``
    and the like), so ``policy explain`` never says an entry is not published when it could
    not look; ``soft`` (the live view) shows none instead."""
    try:
        return _entries(_read(ctx, graph, kind))
    except (GraphCliError, JsonShapeError):
        if soft:
            return {}
        raise


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


def _facts(grant: EntryGrant, only_project: str | None) -> dict[str, list[str]] | None:
    """The principal of ``grant`` as the warning shows it; ``None`` (no principal key) for a
    project admin and a grant that is not bound to its own project."""
    facts = {facet: list(values) for facet, values in grant.principal}
    if only_project is not None and facts.get("project") != [only_project]:
        return None
    return facts


def _who(grants: Sequence[EntryGrant], only_project: str | None) -> str:
    def one(grant: EntryGrant) -> str:
        facts = _facts(grant, only_project)
        if not facts:
            return grant.grant
        shown = ", ".join(f"{facet} {', '.join(values)}" for facet, values in facts.items())
        return f"{grant.grant} ({shown})"

    return ", ".join(one(g) for g in grants)


def _details(
    entry: GraphEntry,
    kind: str,
    name: str,
    grants: Sequence[EntryGrant],
    only_project: str | None,
) -> dict[str, JsonValue]:
    listed: list[JsonValue] = []
    for grant in grants:
        facts = _facts(grant, only_project)
        principal: dict[str, JsonValue] = (
            {}
            if facts is None
            else {"principal": {k: list[JsonValue](v) for k, v in facts.items()}}
        )
        listed.append({"grant": grant.grant, **principal})
    return {"graph": entry.name, "kind": kind, "name": name, "grants": listed}


def _removed(
    entry: GraphEntry,
    kind: str,
    name: str,
    grants: Sequence[EntryGrant],
    old: JsonObject,
    only_project: str | None,
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
        f"the publish removes {kind} {name} from graph {entry.name}, which"
        f" {_who(grants, only_project)} names; the grant gives nothing once it is gone{follow}",
        {**_details(entry, kind, name, grants, only_project), "replaced_by": successor},
    )


def _changed(
    entry: GraphEntry,
    kind: str,
    name: str,
    grants: Sequence[EntryGrant],
    old: JsonObject,
    new: JsonObject,
    only_project: str | None,
) -> WarningRecord:
    return WarningRecord(
        "catalog.granted_entry_changed",
        f"the publish changes {kind} {name} of graph {entry.name}, which"
        f" {_who(grants, only_project)} names:"
        " the callers of the grant run the new definition, and what it reads or writes is the"
        " new definition's, not the one the grant was written for; check it before the callers"
        " use it",
        {
            **_details(entry, kind, name, grants, only_project),
            "hash_before": old.get("hash"),
            "hash_after": new.get("hash"),
        },
    )


def _rows_changed(
    entry: GraphEntry,
    name: str,
    grants: Sequence[EntryGrant],
    changed: list[JsonValue],
    only_project: str | None,
) -> WarningRecord:
    names = ", ".join(str(c["name"]) for c in changed if isinstance(c, dict))
    return WarningRecord(
        "catalog.granted_entry_changed",
        f"the publish changes {names}, which the rows of view {name} of graph {entry.name} run;"
        f" the view stays but {_who(grants, only_project)} names it: the callers of the grant read"
        " what the new function reads, not what the grant was written for; check it before the"
        " callers use the view",
        {**_details(entry, "view", name, grants, only_project), "functions": changed},
    )
