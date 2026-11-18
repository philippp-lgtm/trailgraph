"""What an extraction run does, decided without I/O (file metadata plan, sections 3 and 5).

Pure over a ``Snapshot``, the compiled model and the extractor registry: which extractors a
graph has enabled, what each file's current records say, whether an extractor runs, is current
or no longer applies, and the reconcile document that writes one (file, extractor) result.

A record is one reconcile item of its class, keyed ``<extractor>/<file id>``; the parts it owns
are the items of a second, complete scope keyed ``<extractor>/<file id>/<part name>``. The
planner of ``graph_cli.sources`` diffs both against the stored state under the graph lock, so
the part diff of M8, the unset of what a version no longer produces (M4) and the retract of
what is gone are its work, not this module's.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from graph_cli.extractors.model import STATUS_FAILED, STATUS_OK, ExtractionResult, Extractor
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.changeset import (
    MAX_RECONCILE_OPS,
    ChangeSet,
    ReconcileEdge,
    ReconcileItem,
    ReconcileScope,
    ValueRef,
)
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, TermRef, parse_id
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.query_ast import Cmp, Query
from graph_cli.kernel.records import Revision, SourceRef
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.ontology_layer.semver import Version
from graph_cli.services.managed import EXTRACTOR_KIND

STATUS_NOT_APPLICABLE: Final = "not_applicable"
METADATA_OF: Final = "files:metadata_of"
KEY: Final = "key"
FILE_NOT_ACTIVE: Final = "file not active"
_PAGE: Final = 1000

Decision = Literal["run", "current", "not_applicable", "already_not_applicable", "newer_version"]


@dataclass(frozen=True, slots=True)
class ModuleOutdated:
    """An extractor whose module the graph imports at a version below the one it writes to:
    its older records stay, a release that takes the module's current version enables it."""

    extractor: str
    module: str
    have: str
    need: str

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "extractor": self.extractor,
            "module": self.module,
            "have": self.have,
            "need": self.need,
        }


@dataclass(frozen=True, slots=True)
class RecordState:
    """The stored record of one (file, extractor): what the decision needs of it."""

    entity: EntityId
    key: str
    extractor_version: int
    status: str
    attrs: Mapping[str, JsonValue]


def enabled_extractors(
    model: CompiledModel, registry: Mapping[str, Extractor]
) -> tuple[list[Extractor], list[ModuleOutdated]]:
    """The extractors whose module the graph imports at a version that has their classes, in
    registry order (``format`` first), and those it imports too old."""
    versions = {m.id: m.version for m in model.modules}
    enabled: list[Extractor] = []
    outdated: list[ModuleOutdated] = []
    for extractor in registry.values():
        have = versions.get(extractor.module.id)
        if have is None:
            continue
        if Version.parse(have) < Version.parse(extractor.module.min_version):
            outdated.append(
                ModuleOutdated(
                    extractor.name, extractor.module.id, have, extractor.module.min_version
                )
            )
            continue
        if extractor.record_class not in model.classes:
            continue
        enabled.append(extractor)
    return enabled, outdated


def decide(
    extractor: Extractor,
    detected: str,
    recorded: str,
    record: RecordState | None,
    *,
    retry_failed: bool = False,
) -> Decision:
    """The step of the reconciler for one extractor on one file (plan section 5).

    ``detected`` comes from the format record, ``recorded`` is the type the file was added with.
    A record of a NEWER version than this install's extractor is never touched
    (``newer_version``): an older install must not downgrade what a newer one wrote, and two
    installs would flip-flop. Otherwise an extractor that does not accept the file writes
    nothing for a file it never applied to and turns an older result into ``not_applicable``;
    one that accepts runs when it has no record, when the record is of an older version, when
    the record says ``not_applicable`` (an earlier detection ruled the extractor out, the file
    is accepted now, so the record is stale) or when it is ``failed`` and ``retry_failed`` is
    set; else the record is ``current``.
    """
    if record is not None and record.extractor_version > extractor.version:
        return "newer_version"
    if not extractor.accepts(detected, recorded):
        if record is None or record.status == STATUS_NOT_APPLICABLE:
            return "already_not_applicable"
        return "not_applicable"
    if record is None or record.status == STATUS_NOT_APPLICABLE:
        return "run"
    if record.extractor_version < extractor.version:
        return "run"
    if retry_failed and record.status == STATUS_FAILED:
        return "run"
    return "current"


def record_key(extractor: str, file_id: EntityId | str) -> str:
    """The stored key of the record of one (file, extractor): ``<extractor>/<file id>``."""
    return f"{extractor}/{file_id}"


def file_id_of(key: str) -> str | None:
    """The file id a record or part key names (``<extractor>/<file id>[/<part>]``)."""
    parts = key.split("/", 2)
    return parts[1] if len(parts) >= 2 else None


def read_records(
    snapshot: Snapshot, model: CompiledModel, extractor: Extractor
) -> dict[str, RecordState]:
    """The active records of ``extractor`` by file id (text)."""
    types = concrete_subtypes(model, extractor.record_class)
    found: dict[str, RecordState] = {}
    offset = 0
    where = Cmp(KEY, "prefix", f"{extractor.name}/")
    while True:
        page = snapshot.query(Query(types=types, where=where, limit=_PAGE, offset=offset))
        for revision in page.items:
            state = _state(revision)
            file_id = file_id_of(state.key) if state is not None else None
            if state is not None and file_id is not None:
                found[file_id] = state
        offset += _PAGE
        if offset >= page.total:
            return found


def _state(revision: Revision) -> RecordState | None:
    key = revision.attrs.get(KEY)
    version = revision.attrs.get("extractor_version")
    status = revision.attrs.get("status")
    if not (isinstance(key, str) and isinstance(version, int) and isinstance(status, str)):
        return None
    return RecordState(revision.id, key, version, status, revision.attrs)


def active_files(snapshot: Snapshot, types: Sequence[TermRef]) -> Iterable[Revision]:
    """Every active file of the family, in id order."""
    offset = 0
    while True:
        page = snapshot.query(Query(types=tuple(types), limit=_PAGE, offset=offset))
        yield from page.items
        offset += _PAGE
        if offset >= page.total:
            return


def concrete_subtypes(model: CompiledModel, curie: str) -> tuple[TermRef, ...]:
    """The class and its concrete subclasses (a graph may refine a record class)."""
    return tuple(TermRef(t) for t in model.subtypes(curie) if not model.classes[t].abstract)


def stale_file_ids(
    snapshot: Snapshot,
    model: CompiledModel,
    extractor: Extractor,
    records: Mapping[str, RecordState],
) -> list[str]:
    """File ids whose record exists but whose file is not active (superseded, retracted, or
    gone): their records and parts are retracted (M13)."""
    stale: list[str] = []
    for file_id in sorted(records):
        try:
            entity = EntityId(parse_id(file_id))
        except ValueError:
            continue
        revision = snapshot.get(entity)
        if revision is None or revision.record_status != "active":
            stale.append(file_id)
    return stale


def _source(extractor: Extractor, run: str | None) -> SourceRef:
    return SourceRef(extractor.name, str(extractor.version), run, EXTRACTOR_KIND)


def _fields(model: CompiledModel, curie: str) -> tuple[str, ...]:
    """Every attribute of the class but its key: all of them are the extractor's to set, so a
    value a version does not produce is unset (M4)."""
    return tuple(sorted(name for name in model.classes[curie].attributes if name != KEY))


def _record_scope(
    model: CompiledModel,
    extractor: Extractor,
    file_id: EntityId | str,
    attrs: Mapping[str, JsonValue],
    parts: Sequence[str],
) -> ReconcileScope:
    edges = [ReconcileEdge(TermRef(METADATA_OF), _entity(file_id))]
    if extractor.part_class is not None and extractor.part_relation is not None:
        part_type = TermRef(extractor.part_class)
        relation = TermRef(extractor.part_relation)
        for name in parts:
            value = f"{record_key(extractor.name, file_id)}/{name}"
            edges.append(ReconcileEdge(relation, ValueRef(part_type, KEY, value)))
    return ReconcileScope(
        type=TermRef(extractor.record_class),
        key=KEY,
        under=str(file_id),
        items=(ReconcileItem(key=str(file_id), attrs=attrs, edges=tuple(edges)),),
        fields=_fields(model, extractor.record_class),
        edges=_relations(extractor),
    )


def _relations(extractor: Extractor) -> tuple[TermRef, ...]:
    names = [METADATA_OF] + ([extractor.part_relation] if extractor.part_relation else [])
    return tuple(sorted(TermRef(name) for name in names))


def _entity(file_id: EntityId | str) -> EntityId:
    return EntityId(parse_id(file_id)) if isinstance(file_id, str) else file_id


def _part_scope(
    model: CompiledModel,
    extractor: Extractor,
    file_id: EntityId | str,
    items: Sequence[ReconcileItem],
) -> ReconcileScope:
    assert extractor.part_class is not None
    return ReconcileScope(
        type=TermRef(extractor.part_class),
        key=KEY,
        under=f"{file_id}/",
        items=tuple(items),
        fields=_fields(model, extractor.part_class),
        complete=True,
        allow_empty=True,
        allow_retire=MAX_RECONCILE_OPS,
    )


def record_changeset(
    model: CompiledModel,
    extractor: Extractor,
    file_id: EntityId | str,
    result: ExtractionResult | None,
    *,
    run: str | None = None,
) -> ChangeSet:
    """The document that brings the record of (``file_id``, ``extractor``) to ``result``;
    ``None`` is the result ``not_applicable``. One document is one tx (reason ``extract
    <name>@<version>``) that names the extraction run as its source."""
    if result is not None:
        _check_output(model, extractor, result)
    attrs: dict[str, JsonValue] = {
        "extractor": extractor.name,
        "extractor_version": extractor.version,
        "status": STATUS_NOT_APPLICABLE if result is None else result.status,
    }
    parts: list[ReconcileItem] = []
    names: list[str] = []
    if result is not None and result.status == STATUS_OK:
        attrs.update(result.values)
        for part in result.parts:
            names.append(part.name)
            item_attrs: dict[str, JsonValue] = {"name": part.name, **part.values}
            parts.append(ReconcileItem(key=f"{file_id}/{part.name}", attrs=item_attrs))
    elif result is not None and result.problem is not None:
        attrs["problem"] = result.problem
    scopes = [_record_scope(model, extractor, file_id, attrs, names)]
    if extractor.part_class is not None:
        scopes.append(_part_scope(model, extractor, file_id, parts))
    return ChangeSet(
        ops=(),
        reason=f"extract {extractor.name}@{extractor.version}",
        source=_source(extractor, run),
        reconcile=tuple(scopes),
    )


def _check_output(model: CompiledModel, extractor: Extractor, result: ExtractionResult) -> None:
    """The values of an ``ok`` result are attributes of the record class and the values of each
    part are attributes of the part class: anything else is a bug of the extractor (or a module
    older than the one it writes to), refused as ``internal.error`` before a document is made."""
    if result.status != STATUS_OK:
        return
    problems: list[tuple[str, list[str]]] = []
    record = set(result.values) - set(model.classes[extractor.record_class].attributes)
    if record:
        problems.append((extractor.record_class, sorted(record)))
    if result.parts and extractor.part_class is None:
        problems.append(("no part class", sorted(part.name for part in result.parts)))
    elif extractor.part_class is not None:
        declared = set(model.classes[extractor.part_class].attributes)
        for part in result.parts:
            extra = set(part.values) - declared
            if extra:
                problems.append((extractor.part_class, sorted(extra)))
    if problems:
        names = sorted({n for _, found in problems for n in found})
        raise GraphCliError(
            "internal.error",
            f"extractor {extractor.name}@{extractor.version} produced values "
            f"that its classes do not declare: {names}",
            details={"extractor": extractor.name, "version": extractor.version, "names": names},
        )


def inactive_changeset(
    model: CompiledModel,
    extractor: Extractor,
    file_id: EntityId | str,
    *,
    run: str | None = None,
) -> ChangeSet:
    """The document that retracts the record and the parts of a file that is no longer active
    (M13): complete scopes without items, allowed to empty."""
    record = ReconcileScope(
        type=TermRef(extractor.record_class),
        key=KEY,
        under=str(file_id),
        items=(),
        fields=_fields(model, extractor.record_class),
        edges=_relations(extractor),
        complete=True,
        allow_empty=True,
    )
    scopes = [record]
    if extractor.part_class is not None:
        scopes.append(_part_scope(model, extractor, file_id, ()))
    return ChangeSet(
        ops=(),
        reason=f"extract {extractor.name}@{extractor.version}: {FILE_NOT_ACTIVE}",
        source=_source(extractor, run),
        reconcile=tuple(scopes),
    )
