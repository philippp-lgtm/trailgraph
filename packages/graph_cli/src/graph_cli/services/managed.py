"""Managed classes: entities of derived file metadata are written by their extractor only
(file metadata plan M9).

A record (a subclass of ``files:Metadata``) and its parts (``files:MetadataPart``) are derived
from the bytes of a file; a hand-edited one would be overwritten silently by the next run. No
policy rule can say "only this code may write this type", so the write service refuses every op
whose subject is such an entity unless the request is an extraction run: its source names the
extractor, carries the reserved kind ``extractor`` (set in process, never parsed from a document,
R-K10) and the entity's ``key`` starts with that name, the namespace the reconcile planner
composes (``<source>/<item key>``).

Subjects are the entities whose revision an op changes: a link or unlink changes its source, so
an edge pointing *at* a record, written by someone else's entity, is not a write to the record.
"""

from __future__ import annotations

from collections.abc import Set
from typing import NamedTuple
from uuid import UUID

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.changeset import CreateOp
from graph_cli.kernel.findings import Finding, Severity, sort_findings
from graph_cli.kernel.ids import EntityId, LocalRef
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.records import SOURCE_KINDS, SourceRef
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.ontology_layer.vocabulary import METADATA_PART_TYPE, METADATA_TYPE
from graph_cli.services.refs import Resolution, entity_of, subject_refs

CODE = "data.semantic.managed_class"
EXTRACTOR_KIND = "extractor"
assert EXTRACTOR_KIND in SOURCE_KINDS


def managed_types(model: CompiledModel) -> frozenset[str]:
    """The classes whose entities only an extractor writes: subclasses of the two bases (empty
    for a graph that does not import the module ``files`` at a version that has them)."""
    return frozenset(
        curie
        for curie in model.classes
        if model.is_a(curie, METADATA_TYPE) or model.is_a(curie, METADATA_PART_TYPE)
    )


def managed_findings(
    model: CompiledModel,
    resolution: Resolution,
    snapshot: Snapshot,
    source: SourceRef | None,
    placeholders: Set[UUID] = frozenset(),
) -> list[Finding]:
    """One ``data.semantic.managed_class`` finding per op subject that is a managed entity the
    request does not own; empty for an extraction run on its own entities and for every graph
    without managed classes. Decided from the op's own names before normalize, like the
    attribute view (an entity this request creates is named by its local only; ``placeholders``
    are the ids of earlier dry-run documents)."""
    managed = managed_types(model)
    if not managed or not any(type_curie in managed for _, type_curie in resolution.pairs):
        return []
    cs = resolution.changeset
    refs = resolution.refs
    owner = f"{source.name}/" if source is not None and source.kind == EXTRACTOR_KIND else None
    declared = {op.local: op for op in cs.ops if isinstance(op, CreateOp) and op.local}
    bound = {entity: key[1:] for key, entity in refs.items() if key.startswith("@")}
    found: dict[tuple[object, str], Finding] = {}
    for index, op in enumerate(cs.ops):
        subjects: list[_Subject] = []
        if isinstance(op, CreateOp):
            identity = ("local", op.local) if op.local is not None else ("op", index)
            subjects.append(_Subject(identity, op.type, _key_of(op), None, op.local))
        for ref in subject_refs(op):
            if isinstance(ref, LocalRef) and ref.name in declared:
                created = declared[ref.name]
                identity = ("local", ref.name)
                subjects.append(_Subject(identity, created.type, _key_of(created), None, ref.name))
                continue
            entity = entity_of(ref, refs)
            revision = None if entity is None else snapshot.get(entity)
            if entity is None or revision is None:
                continue  # resolution refuses an op on a missing entity
            key = revision.attrs.get("key")
            subjects.append(_Subject(entity, revision.type, key, entity, bound.get(entity)))
        for subject in subjects:
            if subject.type not in managed:
                continue
            if owner is not None and isinstance(subject.key, str) and subject.key.startswith(owner):
                continue
            target: dict[str, JsonValue] = {}
            if subject.entity is not None and subject.entity not in placeholders:
                target["id"] = str(subject.entity)
            target.update(type=subject.type, path=f"ops.{index}")
            if subject.local is not None:
                target["local"] = subject.local
            found.setdefault(
                (subject.identity, subject.type),
                Finding(
                    Severity.ERROR,
                    CODE,
                    target,
                    f"{subject.type} entities are written only by graph_cli's extractors; "
                    f"a {op.OP} by anyone else is refused",
                ),
            )
    return sort_findings(found.values())


class _Subject(NamedTuple):
    identity: object  # what makes one subject: an entity, a local or an anonymous create
    type: str
    key: JsonValue | None
    entity: EntityId | None
    local: str | None


def _key_of(op: CreateOp) -> JsonValue | None:
    return op.attrs.get("key", op.attrs_text.get("key"))
