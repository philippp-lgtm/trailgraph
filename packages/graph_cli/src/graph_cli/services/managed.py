"""Managed classes: entities of derived file metadata are written by their extractor only
(file metadata plan M9).

A record (a subclass of ``files:Metadata``) and its parts (``files:MetadataPart``) are derived
from the bytes of a file; a hand-edited one would be overwritten silently by the next run. No
policy rule can say "only this code may write this type", so the write service refuses every op
whose subject is such an entity unless the request is an extraction run: its source names the
extractor, carries the reserved kind ``extractor`` (set in process, never parsed from a document,
R-K10) and the entity's ``key`` starts with that name, the namespace the reconcile planner
composes (``<source>/<item key>``).

Ownership is the key prefix alone (no registry lookup): the entity's ``key`` is a string that
starts with ``<source name>/``. A create states its key in ``attrs``; a key given as text, a
missing one or one that is not a string counts as not owned, and a new record's ``extractor``
attribute has to equal the source name. The source checked is the one the tx and the audit
record (``request.request.source``), never the one a change set claims.

Subjects are the entities whose revision an op changes: a link or unlink changes its source, so
an edge pointing *at* a record, written by someone else's entity, is not a write to the record.
"""

from __future__ import annotations

from collections.abc import Sequence, Set
from typing import NamedTuple
from uuid import UUID

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.changeset import CreateOp, ReconcileScope
from graph_cli.kernel.findings import Finding, Severity, sort_findings
from graph_cli.kernel.ids import EntityId, LocalRef
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.records import SOURCE_KINDS, SourceRef
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.ontology_layer.vocabulary import METADATA_PART_TYPE, METADATA_TYPE
from graph_cli.services.refs import Resolution, entity_of, subjects

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
        found_subjects: list[_Subject] = []
        if isinstance(op, CreateOp):
            identity = ("local", op.local) if op.local is not None else ("op", index)
            found_subjects.append(
                _Subject(identity, op.type, _key_of(op, model, source), None, op.local)
            )
        for ref in subjects(op):
            if isinstance(ref, LocalRef) and ref.name in declared:
                created = declared[ref.name]
                identity = ("local", ref.name)
                key = _key_of(created, model, source)
                found_subjects.append(_Subject(identity, created.type, key, None, ref.name))
                continue
            entity = entity_of(ref, refs)
            revision = None if entity is None else snapshot.get(entity)
            if entity is None or revision is None:
                continue  # resolution refuses an op on a missing entity
            key = revision.attrs.get("key")
            found_subjects.append(_Subject(entity, revision.type, key, entity, bound.get(entity)))
        for subject in found_subjects:
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


def _key_of(op: CreateOp, model: CompiledModel, source: SourceRef | None) -> JsonValue | None:
    """The key a create claims, or ``None`` (not owned): a string in ``attrs``, and for a
    record also an ``extractor`` attribute equal to the source's name."""
    key = op.attrs.get("key")
    if not isinstance(key, str):
        return None
    if model.is_a(op.type, METADATA_TYPE) and (
        source is None or op.attrs.get("extractor") != source.name
    ):
        return None
    return key


def managed_scope_findings(
    model: CompiledModel, scopes: Sequence[ReconcileScope], source: SourceRef | None
) -> list[Finding]:
    """One finding per reconcile scope over a managed type from a source that is not an
    extraction run, decided by the scope's type alone, before anything is planned: whether a
    document is refused never depends on which ops its plan happens to emit (an empty plan
    would pass silently)."""
    managed = managed_types(model)
    if not managed or (source is not None and source.kind == EXTRACTOR_KIND):
        return []
    return [
        Finding(
            Severity.ERROR,
            CODE,
            {"type": scope.type, "path": f"reconcile[{index}]"},
            f"{scope.type} entities are written only by graph_cli's extractors; "
            "a reconcile of them by anyone else is refused",
        )
        for index, scope in enumerate(scopes)
        if scope.type in managed
    ]
