"""The migration item of ``history``: what a migration tx did to one entity."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Final

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.services.names import MappedSnapshot
from graph_cli.services.read.graph_read import GraphRead
from graph_cli.services.visibility import VisibleSnapshot
from graph_cli.temporal_knowledge.name_map import NameMap, TermKind


def migration_item(
    view: GraphRead, entity: EntityId, rev: int, seq: int, full: CompiledModel | None = None
) -> dict[str, JsonValue] | None:
    """``history``'s ``migration`` of a revision a migration tx wrote (migration plan M7):
    the versions and summary of its changeset and the renames and steps that changed this
    entity. Both sides of the step show under current names, so without it the history
    would hide a change of type. What applied is found by running the release's transform
    on the stored revision before (``RevisionTransform.applied``); for a type limited reader
    without the edges it cannot see, so nothing names a relation it does not reach. ``full`` is
    the model without read limits, given for any limited reader: an effect that names a term
    of ``full`` the reader cannot see (a retype out of a hidden type, a rename of a hidden
    class) is left out, and when the type the entity had before is hidden none is shown (the lists
    stay, empty). For a reader limited to some attributes the effects on attributes follow
    their attribute (R4, ``attribute_effects``); ``before`` keeps its attributes (its keys
    are old names) and the effects are filtered afterwards."""
    mapping = view.mapping
    record = mapping.migration_record(seq) if mapping is not None else None
    if mapping is None or record is None:
        return None
    old, new = record.get("from"), record.get("to")
    item: dict[str, JsonValue] = {"from": old, "to": new}
    summary = record.get("summary")
    if isinstance(summary, str):
        item["summary"] = summary
    applied: list[JsonValue] = []
    before = mapping.raw.get_revision(entity, rev - 1) if rev > 1 else None
    hop = None
    if before is not None and isinstance(old, str) and isinstance(new, str):
        try:
            hop = mapping.names.name_map.hop(old, new)
        except GraphCliError:
            hop = None
    if before is not None and hop is not None:
        if isinstance(view.snapshot, VisibleSnapshot):
            before = view.snapshot.strip_edges(before)
        applied = hop.migrating.applied(before)
        if full is not None:
            # an entity that was of a hidden type shows no effects at all (the schema keeps the
            # two lists, empty)
            shown = _visible_effects(view, full, mapping.names.name_map, before.type, applied)
            applied = shown if shown is not None else []
        if view.read_view.attributes_limited:
            applied = attribute_effects(view, mapping, entity, rev, applied)
    elif not view.read_view.limited:
        applied = _recorded_effects(record)  # the release files are gone: the whole record
    item["renames"] = [e for e in applied if _kind(e).startswith("rename_")]
    item["steps"] = [e for e in applied if not _kind(e).startswith("rename_")]
    return item


def _visible_effects(
    view: GraphRead,
    full: CompiledModel,
    name_map: NameMap,
    before_type: str,
    applied: list[JsonValue],
) -> list[JsonValue] | None:
    """``applied`` without the effects that name a term the reader cannot see, or ``None``
    when the type the entity had is hidden. A term is
    hidden when it, or a current name it was given later, is a term of ``full`` and not of the
    reader's model: an old name that no current term carries stays visible."""
    shown = view.model

    def hidden(kind: TermKind, term: object) -> bool:
        if not isinstance(term, str):
            return False
        current = {term} | {h.current for h in name_map.hints(kind, term)}
        if kind == "relation":
            return any(n in full.relations and n not in shown.relations for n in current)
        return any(n in full.classes and n not in shown.classes for n in current)

    if hidden("class", before_type):
        return None  # nothing of what changed is for this reader

    def names(effect: JsonValue) -> list[tuple[TermKind, object]]:
        if not isinstance(effect, Mapping):
            return []
        found: list[tuple[TermKind, object]] = []
        kind = _kind(effect)
        if kind == "rename_relation":
            found += [("relation", effect.get("from")), ("relation", effect.get("to"))]
        elif kind in ("rename_class", "retype"):
            found += [("class", effect.get("from")), ("class", effect.get("to"))]
        found += [("class", effect.get("class")), ("class", effect.get("type"))]
        found.append(("relation", effect.get("relation")))
        return found

    return [e for e in applied if not any(hidden(kind, term) for kind, term in names(e))]


# The steps that name one attribute (of the new version) of the entity they change.
ATTRIBUTE_STEPS: Final = frozenset({"fill", "map_values", "convert"})


# The effects that carry no attribute name or value: classes, relations, qualifiers and edges
# (a qualifier cannot be marked). Any other kind, also one a later version adds, is left out
# for a reader limited to some attributes unless ``attribute_effects`` judges it.
ATTRIBUTE_FREE_EFFECTS: Final = frozenset(
    {
        "rename_class",
        "rename_relation",
        "rename_qualifier",
        "map_qualifier_values",
        "drop_qualifier",
        "drop_edges",
    }
)


def attribute_effects(
    view: GraphRead, mapping: MappedSnapshot, entity: EntityId, rev: int, applied: list[JsonValue]
) -> list[JsonValue]:
    """``applied`` as a reader limited to some attributes may see it (R4), by an allow list:
    the effects that carry no attribute (``ATTRIBUTE_FREE_EFFECTS``) stay; an attribute named
    by a rename, ``fill``, ``map_values``, ``convert`` or a retype's attribute map is judged
    under its current name on the entity's current type (a probe of the revision the migration
    wrote, mapped like any old revision) against the view, and the effect (or the entry of the
    map) is left out when it is hidden; every ``drop`` is left out (its name has no current
    counterpart to judge), and so is an effect of any other kind."""
    written = mapping.raw.get_revision(entity, rev)

    def shown(attribute: object) -> bool:
        if written is None or not isinstance(attribute, str):
            return False
        probe = replace(written, attrs={attribute: True})
        if mapping.names.unmapped(probe):
            return False
        current = mapping.names.map(probe)
        keep = view.read_view.keeps(current.type)
        return keep is None or any(name in keep for name in current.attrs)

    found: list[JsonValue] = []
    for effect in applied:
        if not isinstance(effect, Mapping):
            continue
        kind = _kind(effect)
        if kind in ATTRIBUTE_FREE_EFFECTS:
            found.append(effect)
        elif kind == "rename_attribute":
            if shown(effect.get("to")):
                found.append(effect)
        elif kind in ATTRIBUTE_STEPS:
            if shown(effect.get("attribute")):
                found.append(effect)
        elif kind == "retype":
            pairs = effect.get("attributes")
            pairs = pairs if isinstance(pairs, Mapping) else {}
            found.append({**effect, "attributes": {o: n for o, n in pairs.items() if shown(n)}})
    return found


def _kind(effect: JsonValue) -> str:
    kind = effect.get("kind") if isinstance(effect, Mapping) else None
    return kind if isinstance(kind, str) else ""


def _recorded_effects(record: Mapping[str, JsonValue]) -> list[JsonValue]:
    """The renames and steps of a changeset's ``migration`` record, in the form of
    ``RevisionTransform.applied``."""
    found: list[JsonValue] = []
    renames = record.get("renames")
    renames = renames if isinstance(renames, Mapping) else {}
    for key, kind in (("classes", "rename_class"), ("relations", "rename_relation")):
        pairs = renames.get(key)
        if isinstance(pairs, Mapping):
            found += [{"kind": kind, "from": o, "to": n} for o, n in sorted(pairs.items())]
    attributes = renames.get("attributes")
    for entry in attributes if isinstance(attributes, list) else []:
        if isinstance(entry, Mapping):
            found.append({"kind": "rename_attribute", **entry})
    steps = record.get("steps")
    for index, step in enumerate(steps if isinstance(steps, list) else []):
        if isinstance(step, Mapping) and len(step) == 1:
            ((name, body),) = step.items()
            if isinstance(body, Mapping):
                found.append({"kind": name, **body, "index": index})
    return found
