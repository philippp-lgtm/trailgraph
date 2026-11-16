"""Read-time mapping: old revisions under the names of the pinned model (migration plan M7).

Three pieces, all opened by ``ReadService.open``:

- ``NameMaps`` is the cache the read service owns: the ``NameMap`` of a graph at its pin,
  built from the pinned versions of the graph's audit stream (``audit.pins``), the installed
  ``compiled.json`` of each version and the installed ``migration.yaml``
  (``temporal_knowledge.name_map``). It is built once per (graph, pin), and only when a
  revision of another major than the pin is read or an unknown term needs its hint. A server
  builds its read service for every request, so it hands all of them one ``NameMapCache``:
  a map that is complete (every file of its chain was readable) outlives the request. A pin
  names an installed release for good and the pin history only grows, so a cached map never
  goes stale; the cache is bounded.
- ``GraphNames`` is one graph at one pin for one request: ``map`` returns a revision of the
  pinned major as it is, without building anything.
- ``MappedSnapshot`` implements the ``Snapshot`` port over a raw snapshot (layered raw store
  -> mapped -> ``VisibleSnapshot``, so read limits judge the mapped type). **Current
  revisions are never mapped** (``current=False``, every head read): after a major release
  every current revision fits the pin whatever its ``ontology_version``. Past revisions
  (``get_revision``, ``history``) are mapped. An ``--as-of`` state whose names changed since
  (``current=True``) maps its current revisions too, and answers ``query``, ``neighbors`` and
  ``incoming`` with the reference evaluator over the mapped revisions (the replay path ``--as-of``
  already takes; the mapped revisions are read once per snapshot). Queries are never
  translated: they name current terms and meet mapped revisions.

A mapped revision keeps its id, rev, tx, ``ontology_version`` and every identity field; its
content hash is recomputed (as for a revision a read limit stripped). ``mapped`` tells which
revisions a snapshot handed out mapped (``names_mapped`` of ``--full``), ``unmapped`` which it
handed out under names the map cannot vouch for (``NameMap.unmapped``: behind an unreadable hop
or of a version outside the chain), which a reader limited to some attributes must not trust.
"""

from __future__ import annotations

import re
import sys
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import replace
from functools import cached_property
from itertools import pairwise
from typing import Final

from graph_cli.audit.pins import pinned_versions
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ids import EntityId, GraphId, TermRef
from graph_cli.kernel.ports.audit import AuditSink, graph_stream
from graph_cli.kernel.ports.catalog import Catalog, GraphEntry
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.query_ast import NeighborQuery, Query, QueryPage
from graph_cli.kernel.query_eval import run_neighbors, run_query
from graph_cli.kernel.records import (
    RECORD_STATUSES,
    EdgeRecord,
    OntologyPin,
    Revision,
    RevisionMeta,
    TxRecord,
    revision_content_hash,
    revision_meta,
)
from graph_cli.kinetic.params import Problem
from graph_cli.ontology_layer.diff import derive_renames
from graph_cli.ontology_layer.migration import MigrationDoc, parse_migration
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.ontology_layer.semver import Version, bump_level
from graph_cli.services.models import ModelCache, installed_migration
from graph_cli.temporal_knowledge.name_map import (
    KINDS,
    Hop,
    NameMap,
    RenameHint,
    TermKind,
    other_major,
)

_ALL: Final = Query(record_status=frozenset(RECORD_STATUSES), limit=sys.maxsize)
_CACHE_LIMIT: Final = 16  # (graph, pin) -> name map

_MapKey = tuple[GraphId, OntologyPin]


class NameMapCache:
    """Complete name maps by (graph, pin), shared by the read services of one process
    (threads of a server); the oldest entry goes when ``limit`` is reached."""

    def __init__(self, limit: int = _CACHE_LIMIT) -> None:
        self._limit = limit
        self._lock = threading.Lock()
        self._maps: dict[_MapKey, NameMap] = {}

    def get(self, key: _MapKey) -> NameMap | None:
        with self._lock:
            return self._maps.get(key)

    def put(self, key: _MapKey, name_map: NameMap) -> None:
        with self._lock:
            self._maps.pop(key, None)
            if len(self._maps) >= self._limit:
                del self._maps[next(iter(self._maps))]
            self._maps[key] = name_map

    def __len__(self) -> int:
        with self._lock:
            return len(self._maps)


class NameMaps:
    """The name maps of the graphs one service reads, per (graph, pin). ``shared`` is the
    process-wide cache a server passes in; without one the maps last as long as this object."""

    def __init__(
        self,
        catalog: Catalog,
        audit: AuditSink,
        models: ModelCache,
        shared: NameMapCache | None = None,
    ) -> None:
        self._catalog = catalog
        self._audit = audit
        self._models = models
        self._shared = shared
        self._maps: dict[_MapKey, NameMap] = {}

    def of(self, entry: GraphEntry) -> NameMap:
        key = (entry.id, entry.pin)
        found = self._maps.get(key)
        if found is not None:
            return found
        found = self._shared.get(key) if self._shared is not None else None
        if found is None:
            found, complete = self._build(entry)
            if complete and self._shared is not None:
                self._shared.put(key, found)
        if len(self._maps) >= _CACHE_LIMIT:
            del self._maps[next(iter(self._maps))]
        self._maps[key] = found
        return found

    def chain(self, entry: GraphEntry) -> tuple[list[str], bool]:
        """The pinned versions (the last is the pin) and whether the pin history was
        readable; reads the audit stream only, no release file."""
        return self._chain(entry)

    def build(self, entry: GraphEntry) -> tuple[NameMap, bool]:
        """The map and whether it is complete, built afresh (not cached): the release check
        of reused marked names refuses an incomplete one (fail closed)."""
        return self._build(entry)

    def _build(self, entry: GraphEntry) -> tuple[NameMap, bool]:
        """The map, and whether every file and line it draws on was readable: only a
        complete map is shared, so a request that met a damaged file does not hide the
        names from the next one."""
        chain, complete = self._chain(entry)
        hops: dict[int, Hop] = {}
        unreadable: list[int] = []
        for index, (old, new) in enumerate(pairwise(chain)):
            hop, readable = self._hop(entry, old, new)
            complete = complete and readable
            if hop is not None:
                hops[index] = hop
            if not readable:
                unreadable.append(index)
        return NameMap(chain, hops, unreadable), complete

    def _chain(self, entry: GraphEntry) -> tuple[list[str], bool]:
        """The pinned versions; the last is the current pin, also while a migration is
        committed but not pinned yet (the catalog already follows it then)."""
        complete = True
        try:
            lines = (line.data for line in self._audit.read_lines(graph_stream(str(entry.id))))
            chain = pinned_versions(lines)
        except GraphCliError:
            chain, complete = [], False  # no stream to read: nothing is mapped
        if not chain or chain[-1] != entry.pin.version:
            chain.append(entry.pin.version)
        return chain, complete

    def _hop(
        self, entry: GraphEntry, old_version: str, new_version: str
    ) -> tuple[Hop | None, bool]:
        """The hop of a major pin switch (``None`` for any other step), and whether it could
        be read: when the release files of either side cannot, those revisions keep their
        names."""
        try:
            if bump_level(Version.parse(old_version), Version.parse(new_version)) != "major":
                return None, True
        except ValueError:
            return None, True
        try:
            old = self._models.load_release(entry.id, old_version)
            new = self._models.load_release(entry.id, new_version)
            migration = self._migration(entry, old_version, new_version)
        except GraphCliError:
            return None, False
        hop = Hop(
            old_version,
            new_version,
            old,
            new,
            derive_renames(old, new),
            migration,
            entry.language,
        )
        return hop, True

    def _migration(
        self, entry: GraphEntry, old_version: str, new_version: str
    ) -> MigrationDoc | None:
        """The installed migration of ``new_version`` when it migrated from ``old_version``."""
        data = installed_migration(
            lambda name: self._catalog.read_release_file(entry.id, new_version, name)
        )
        if data is None:
            return None
        doc = parse_migration(data).doc
        return doc if doc is not None and doc.from_version == old_version else None


class GraphNames:
    """One graph at its pin, for one request: the name map is built on first need."""

    def __init__(self, maps: NameMaps, entry: GraphEntry) -> None:
        self._maps = maps
        self.entry = entry

    @cached_property
    def name_map(self) -> NameMap:
        return self._maps.of(self.entry)

    def changes(
        self, version: str, known: Callable[[TermKind, RenameHint], bool] | None = None
    ) -> bool:
        """Whether revisions written under ``version`` show other names under the pin
        (``known``: names a caller with that test may see, ``NameMap.changes``)."""
        return other_major(version, self.entry.pin.version) and self.name_map.changes(
            version, known
        )

    def unmapped_version(self, version: str) -> bool:
        """Whether revisions written under ``version`` reach the pin under names the map
        cannot vouch for (``NameMap.unmapped``); the pinned major never does."""
        return other_major(version, self.entry.pin.version) and self.name_map.unmapped(version)

    def map(self, revision: Revision) -> Revision:
        """``revision`` under the pin's names (the same object when nothing changes)."""
        if not other_major(revision.ontology_version, self.entry.pin.version):
            return revision
        return self.name_map.map(revision)

    def unmapped(self, revision: Revision) -> bool:
        """Whether ``map`` hands ``revision`` out under names the map cannot vouch for
        (``NameMap.unmapped``); a revision of the pinned major never is."""
        if not other_major(revision.ontology_version, self.entry.pin.version):
            return False
        return self.name_map.unmapped(revision.ontology_version)


class MappedSnapshot:
    """A ``Snapshot`` that hands out revisions under the pin's names (see the module
    docstring). One per request; it remembers every revision it mapped."""

    def __init__(self, inner: Snapshot, names: GraphNames, *, current: bool) -> None:
        self._inner = inner
        self.names = names
        self._current = current
        self._revisions: dict[tuple[EntityId, int], Revision] = {}
        self._mapped: set[tuple[EntityId, int]] = set()
        self._unmapped: set[tuple[EntityId, int]] = set()

    @property
    def raw(self) -> Snapshot:
        """The snapshot below: revisions and transactions as stored."""
        return self._inner

    @property
    def seq(self) -> int:
        return self._inner.seq

    @property
    def warnings(self) -> tuple[WarningRecord, ...]:
        return self._inner.warnings

    def migration_record(self, seq: int) -> Mapping[str, JsonValue] | None:
        """The ``migration`` record of tx ``seq`` when a migration wrote it, read from the raw
        tx below any read limit (``history`` shows only its versions, its summary and what
        changed the entity it lists, plan M7); ``None`` for any other tx."""
        tx = self._inner.tx(seq)
        record = tx.changeset.get("migration") if tx is not None else None
        return record if isinstance(record, Mapping) else None

    def mapped(self, entity: EntityId, rev: int) -> bool:
        """Whether this snapshot handed out revision ``rev`` of ``entity`` under mapped names."""
        return (entity, rev) in self._mapped

    def unmapped(self, entity: EntityId, rev: int) -> bool:
        """Whether this snapshot handed out revision ``rev`` of ``entity`` under names the map
        cannot vouch for (as stored, behind an unreadable hop or of a version outside the
        chain): a reader limited to some attributes sees none of its attributes (R3)."""
        return (entity, rev) in self._unmapped

    def _map(self, revision: Revision) -> Revision:
        key = (revision.id, revision.rev)
        found = self._revisions.get(key)
        if found is None:
            found = self._revisions[key] = self.names.map(revision)
            if found is not revision:
                self._mapped.add(key)
            if self.names.unmapped(revision):
                self._unmapped.add(key)
        return found

    # ---- the port --------------------------------------------------------

    def get(self, entity: EntityId) -> Revision | None:
        current = self._inner.get(entity)
        return current if current is None or not self._current else self._map(current)

    def get_many(self, entities: Sequence[EntityId]) -> Mapping[EntityId, Revision]:
        found = self._inner.get_many(entities)
        if not self._current:
            return found
        return {entity: self._map(revision) for entity, revision in found.items()}

    def get_revision(self, entity: EntityId, rev: int) -> Revision | None:
        revision = self._inner.get_revision(entity, rev)
        if revision is None:
            return None
        if not self._current:
            current = self._inner.get(entity)
            if current is not None and current.rev == rev:
                return revision  # a current revision is never mapped
        return self._map(revision)

    def history(self, entity: EntityId) -> Sequence[RevisionMeta]:
        found = []
        for meta in self._inner.history(entity):
            revision = self.get_revision(entity, meta.rev)
            if revision is not None and revision.content_hash != meta.content_hash:
                meta = revision_meta(revision)
            found.append(meta)
        return found

    def tx(self, seq: int) -> TxRecord | None:
        return self._inner.tx(seq)

    def incoming(
        self, entity: EntityId, rel: TermRef | None = None
    ) -> Sequence[tuple[EntityId, EdgeRecord]]:
        if not self._current:
            return self._inner.incoming(entity, rel)
        found = []
        for source, edge in self._inner.incoming(entity):
            revision = self.get(source)
            mapped = _edge(revision, edge) if revision is not None else edge
            if rel is None or mapped.rel == rel:
                found.append((source, mapped))
        return found

    def resolve_alias(self, alias: str) -> tuple[EntityId, bool] | None:
        return self._inner.resolve_alias(alias)

    def match_suffix(self, hex_suffix: str, *, limit: int = 10) -> Sequence[EntityId]:
        return self._inner.match_suffix(hex_suffix, limit=limit)

    def query(self, q: Query) -> QueryPage:
        if not self._current:
            return self._inner.query(q)
        return run_query(self if q.attributes is None else _Blind(self), q)

    def neighbors(self, q: NeighborQuery) -> Sequence[tuple[EntityId, EdgeRecord, Revision]]:
        if not self._current:
            return self._inner.neighbors(q)
        return run_neighbors(self if q.attributes is None else _Blind(self), q)

    # ---- the universe of the reference evaluator (current=True only) --------

    def current(self, entity: EntityId) -> Revision | None:
        return self.get(entity)

    def current_revisions(self, types: frozenset[TermRef] = frozenset()) -> Iterator[Revision]:
        """Every current revision, mapped, in id order. The type hint names current types,
        which stored revisions may not carry yet, so it is not passed down (callers filter)."""
        yield from self._universe

    @cached_property
    def _universe(self) -> tuple[Revision, ...]:
        found = self._inner.query(_ALL).items
        return tuple(self._map(r) for r in sorted(found, key=lambda r: r.id))


class _Blind:
    """The universe of a ``MappedSnapshot`` for a query with an attribute mask: a revision
    handed out under names the map cannot vouch for has no attributes (R3), so no predicate,
    order or answer reads them; its content hash is that of what is left."""

    def __init__(self, mapped: MappedSnapshot) -> None:
        self._mapped = mapped

    def _blind(self, revision: Revision) -> Revision:
        if not revision.attrs or not self._mapped.unmapped(revision.id, revision.rev):
            return revision
        blind = replace(revision, attrs={})
        return replace(blind, content_hash=revision_content_hash(blind))

    def current(self, entity: EntityId) -> Revision | None:
        found = self._mapped.current(entity)
        return None if found is None else self._blind(found)

    def current_revisions(self, types: frozenset[TermRef] = frozenset()) -> Iterator[Revision]:
        for revision in self._mapped.current_revisions(types):
            yield self._blind(revision)

    def incoming(
        self, entity: EntityId, rel: TermRef | None = None
    ) -> Sequence[tuple[EntityId, EdgeRecord]]:
        return self._mapped.incoming(entity, rel)


def _edge(revision: Revision, edge: EdgeRecord) -> EdgeRecord:
    """``edge`` as the mapped ``revision`` holds it (same id)."""
    return next((e for e in revision.edges if e.edge_id == edge.edge_id), edge)


# ---- hints ---------------------------------------------------------------------------------


def known_in(
    model: CompiledModel, full: CompiledModel | None = None
) -> Callable[[str, RenameHint], bool]:
    """Whether a hint may be shown to a caller whose view is ``model``: its current name is a
    term of ``model``, and no step of the chain passes through a term the caller cannot see.
    ``full`` is the model without read limits: a step's name (or owning class) that is a term
    of ``full`` and not of ``model`` is hidden from the caller, so the chain would reveal it;
    an attribute step is hidden when the owning class of ``full`` declares its name and the
    class of ``model`` does not (field visibility plan R5). Names that are no term of ``full``
    (old names on the way) are fine."""

    def hidden(kind: str, name: str | None) -> bool:
        if full is None or name is None:
            return False
        if kind == "relation":
            return name in full.relations and name not in model.relations
        return name in full.classes and name not in model.classes

    def hidden_attribute(owner: str | None, name: str) -> bool:
        if full is None or owner is None:
            return False
        declared, shown = full.classes.get(owner), model.classes.get(owner)
        return (
            declared is not None
            and name in declared.attributes
            and (shown is None or name not in shown.attributes)
        )

    def known(kind: str, hint: RenameHint) -> bool:
        for step in hint.steps:
            if kind != "attribute":
                if hidden(kind, step.to):
                    return False
            elif hidden("class", step.owner) or hidden_attribute(step.owner, step.to):
                return False
        if kind == "class":
            return hint.current in model.classes
        if kind == "relation":
            return hint.current in model.relations
        owner = hint.steps[-1].owner
        classes = model.classes.values() if owner is None else [model.classes.get(owner)]
        return any(c is not None and hint.current in c.attributes for c in classes)

    return known


def term_hints(
    name_map: NameMap, kind: str, term: str, known: Callable[[str, RenameHint], bool]
) -> tuple[RenameHint, ...]:
    """The rename hints for an unknown term of a ``term.unknown`` ``kind``; none for a kind
    that names no class, relation or attribute."""
    term_kind = KINDS.get(kind)
    if term_kind is None or not term:
        return ()
    return name_map.hints(term_kind, term, known=lambda hint: known(term_kind, hint))


def hinted_message(message: str, hints: Sequence[RenameHint]) -> str:
    return message + "; " + "; ".join(h.text() for h in hints)


def hints_json(hints: Sequence[RenameHint]) -> list[JsonValue]:
    return [h.to_json() for h in hints]


# The function and action problems that name an unknown term, by their code; a
# ``term.unknown`` carries its own ``kind``.
_PROBLEM_KINDS: Final = {
    "action.unknown_type": "type",
    "action.unknown_relation": "relation",
    "action.unknown_attribute": "attribute",
    "action.unknown_class": "type",
    "function.unknown_class": "type",
}


def hinted_problems(
    problems: Sequence[Problem],
    name_map: NameMap,
    model: CompiledModel,
    full: CompiledModel | None = None,
) -> list[Problem]:
    """``problems`` of a function or action folder checked against ``model``, those naming an
    old term with its current name (in the message and ``details.renamed``), as
    ``term.unknown`` gets it on input."""
    found = []
    for problem in problems:
        kind = problem.details.get("kind", _PROBLEM_KINDS.get(problem.code))
        term = problem.details.get(
            "term", problem.details.get("name", problem.details.get("curie"))
        )
        named = problem.code == "term.unknown" or problem.code in _PROBLEM_KINDS
        hints = (
            term_hints(name_map, kind, term, known_in(model, full))
            if named and isinstance(kind, str) and isinstance(term, str)
            else ()
        )
        if hints:
            problem = replace(
                problem,
                message=hinted_message(problem.message, hints),
                details={**problem.details, "renamed": hints_json(hints)},
            )
        found.append(problem)
    return found


_RELATION_MESSAGE: Final = re.compile(r"^relation (\S+) does not exist")


def unknown_term_of(finding: JsonValue) -> tuple[str, str] | None:
    """``(kind, term)`` of a write finding that names an unknown type, attribute or relation
    (``data.structural.unknown_type``, ``unknown_attribute``, ``data.referential.
    unknown_relation``), else ``None``."""
    if not isinstance(finding, Mapping):
        return None
    code, target, message = finding.get("code"), finding.get("target"), finding.get("message")
    path = target.get("path") if isinstance(target, Mapping) else None
    if code == "data.structural.unknown_type":
        actual = finding.get("actual")
        return ("type", actual) if isinstance(actual, str) else None
    if code == "data.structural.unknown_attribute" and isinstance(path, str):
        name = path.removeprefix("attrs.")
        return ("attribute", name) if name != path else None
    if code == "data.referential.unknown_relation" and isinstance(message, str):
        found = _RELATION_MESSAGE.match(message)
        return ("relation", found.group(1)) if found else None
    return None


def hinted_findings(
    error: GraphCliError,
    name_map: NameMap,
    model: CompiledModel,
    full: CompiledModel | None = None,
) -> GraphCliError:
    """A ``write.invalid`` whose findings name old terms, with their current names as the
    findings' ``hint`` (one that has a hint already keeps it)."""
    findings = error.details.get("findings")
    if not isinstance(findings, list):
        return error
    changed = False
    shown: list[JsonValue] = []
    for finding in findings:
        named = unknown_term_of(finding)
        if named is not None and isinstance(finding, Mapping) and "hint" not in finding:
            hints = term_hints(name_map, named[0], named[1], known_in(model, full))
            if hints:
                finding = {**finding, "hint": "; ".join(h.text() for h in hints)}
                changed = True
        shown.append(finding)
    if not changed:
        return error
    return GraphCliError(
        error.code,
        error.message,
        details={**error.details, "findings": shown},
        warnings=error.warnings,
    )


def with_hints(error: GraphCliError, hints: Sequence[RenameHint]) -> GraphCliError:
    """``error`` with the hints in its message and in ``details.renamed``."""
    if not hints:
        return error
    return GraphCliError(
        error.code,
        hinted_message(error.message, hints),
        details={**error.details, "renamed": hints_json(hints)},
        warnings=error.warnings,
    )
