"""The read views of a limited caller (type read limits plan V4, V5, V9; field visibility
plan F4, F5).

A caller who may see only some types reads the graph as if the others did not exist, and one
who lacks a marking reads its attributes as if their types did not declare them. What a caller
sees is one ``ReadView`` (``kernel.read_view``) per (policy, graph, pin, actor). Three pieces,
all opened in one place (``ReadService.open``):

- ``Visibility`` is the cache the service owns, like ``FineTypes``. ``of`` answers what an
  actor sees on a graph (``ReadView``; both halves ``None``: nothing is filtered); ``model``
  hands out the filtered model for such a view. Nothing is decided on the record: the probes of
  ``effective.visibility`` and ``effective.held_markings`` are no audited decisions.
- ``VisibleSnapshot`` implements the ``Snapshot`` port over a raw snapshot: a hidden entity
  answers as a missing one, every revision handed out loses its edges to hidden entities and
  the attributes it may not see (with its content hash recomputed), a transaction shows its
  metadata only, and queries carry the view into the kernel (``Query.visible``,
  ``Query.attributes``).
- ``filter_model`` is the pure model view: a ``CompiledModel`` without the hidden types and
  everything that depends on them, so every read consumer keeps working on the same class. It
  is a read view; the write path validates against the full model.

Visibility is decided under the policy installed now and the pinned model, also for ``--as-of``
and past revisions (V9): an entity is visible when its type at the snapshot is.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable, Collection, Hashable, Iterable, Mapping, Sequence
from dataclasses import replace
from typing import Final

from graph_cli.access_control.authorize import is_project_admin
from graph_cli.access_control.documents import principal_facts, read_limited
from graph_cli.access_control.effective import held_markings, visibility
from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.canonical import JsonValue, canonical_bytes
from graph_cli.kernel.errors import EMPTY, WarningRecord
from graph_cli.kernel.ids import EntityId, GraphId, TermRef
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.query_ast import NeighborQuery, Query, QueryPage
from graph_cli.kernel.read_view import UNLIMITED, AttributeMask, ReadView
from graph_cli.kernel.records import (
    EdgeRecord,
    Revision,
    RevisionMeta,
    TxRecord,
    revision_meta,
    strip_revision,
)
from graph_cli.ontology_layer.model import (
    AttributeDef,
    ClassDef,
    CompiledModel,
    LifecycleDef,
    ModuleInfo,
    RelationDef,
)
from graph_cli.services.term_model import concrete_types

_ACTOR_CACHE_LIMIT: Final = 512  # (policy, graph, pin, actor) -> read view
_MODEL_CACHE_LIMIT: Final = 64  # (pin, read view) -> filtered model
_PIN_CACHE_LIMIT: Final = 64  # pin -> the markings its model uses

NamesRemoved = Callable[[JsonValue], bool]


# ---- the cache the read service owns -------------------------------------------------------


class Visibility:
    """What each actor sees per graph, and the filtered models, computed once and cached.

    ``read_limited`` is cached per (policy hash, graph), the markings a pinned model uses per
    pin artifact hash. The read view is cached per (policy hash, graph, pin artifact hash, actor
    key); the key holds every fact ``authorize`` reads, so two actors that no rule can tell
    apart share an answer and none shares one it must not. The caches are bounded (the oldest
    entries go first): a server sees an unbounded number of asserted session and agent ids.

    The fast path: a graph that is not read limited and whose pin uses no marking gets the
    unlimited view without a probe; a caller who holds every marking the pin uses gets no
    attribute half.
    """

    def __init__(self) -> None:
        self._limited: dict[tuple[str, GraphId], bool] = {}
        self._marked: dict[str, frozenset[str]] = {}
        self._visible: dict[tuple[Hashable, ...], ReadView] = {}
        self._models: dict[tuple[str, ReadView], CompiledModel] = {}

    def limited(self, policy: PolicySet, graph: GraphId) -> bool:
        """Whether some rule of the policy set names a read action on a type of ``graph``."""
        key = (policy.hash, graph)
        found = self._limited.get(key)
        if found is None:
            found = self._limited[key] = read_limited(policy.documents, graph)
        return found

    def marked(self, entry: GraphEntry, load: Callable[[], CompiledModel]) -> frozenset[str]:
        """The markings the graph's pinned model uses (``CompiledModel.used_markings``);
        ``load`` runs once per pin."""
        key = entry.pin.artifact_hash
        found = self._marked.get(key)
        if found is None:
            found = load().used_markings()
            _remember(self._marked, key, found, _PIN_CACHE_LIMIT)
        return found

    def of(
        self, actor: ActorContext, entry: GraphEntry, policy: PolicySet, model: CompiledModel
    ) -> ReadView:
        """What ``actor`` sees in the graph whose pinned model is ``model``."""
        return self.lazy(actor, entry, policy, lambda: model)

    def lazy(
        self,
        actor: ActorContext,
        entry: GraphEntry,
        policy: PolicySet,
        load: Callable[[], CompiledModel],
    ) -> ReadView:
        """``of`` for callers without the model at hand: ``load`` runs once per pin to learn
        its markings, and again only when the view must be computed (the graph is read limited
        or its pin uses markings, and the answer is not cached yet)."""
        types_limited = self.limited(policy, entry.id)
        marked = self.marked(entry, load)
        if not types_limited and not marked:
            return UNLIMITED
        key = (policy.hash, entry.id, entry.pin.artifact_hash, *actor_key(actor))
        found = self._visible.get(key)
        if found is not None:
            return found
        model = load()
        types = (
            visibility(actor, entry.id, policy, concrete_types(model.classes))
            if types_limited
            else None
        )
        attributes = (
            attribute_mask(model, held_markings(actor, entry.id, policy, marked))
            if marked
            else None
        )
        found = ReadView(types, attributes)
        _remember(self._visible, key, found, _ACTOR_CACHE_LIMIT)
        return found

    def types(
        self,
        actor: ActorContext,
        entry: GraphEntry,
        policy: PolicySet,
        load: Callable[[], CompiledModel],
    ) -> frozenset[str] | None:
        """The type half of ``lazy`` alone (``None``: every type): ``load`` runs only when the
        graph is read limited."""
        if not self.limited(policy, entry.id):
            return None
        return self.lazy(actor, entry, policy, load).types

    def model(self, entry: GraphEntry, model: CompiledModel, view: ReadView) -> CompiledModel:
        """The model view of ``model`` for ``view``, cached per (pin artifact hash, view);
        ``model`` itself for a view without a type limit (the attribute half does not filter
        the model yet)."""
        if view.types is None:
            return model
        key = (entry.pin.artifact_hash, view)
        found = self._models.get(key)
        if found is None:
            found = filter_model(model, view.types)
            _remember(self._models, key, found, _MODEL_CACHE_LIMIT)
        return found


def attribute_mask(model: CompiledModel, held: Collection[str]) -> AttributeMask | None:
    """The attribute half of a read view: per concrete type of ``model``, sorted by type, the
    attributes all of whose markings are in ``held`` (unmarked ones always). ``None`` when the
    caller holds every marking the model uses, so it sees every attribute."""
    kept = frozenset(held)
    if model.used_markings() <= kept:
        return None
    return tuple(
        (name, frozenset(a for a, d in cls.attributes.items() if kept.issuperset(d.markings)))
        for name, cls in sorted(model.classes.items())
        if not cls.abstract
    )


def actor_key(actor: ActorContext) -> tuple[Hashable, ...]:
    """Everything ``authorize`` reads of an actor: the asserted and the verified facts (allow
    rules match verified ones only, deny rules and conditions all) and whether the actor is
    a project admin (the ceiling)."""
    return (
        tuple(principal_facts(actor).items()),
        tuple(principal_facts(actor, verified_only=True).items()),
        is_project_admin(actor),
    )


def _remember[K, V](cache: dict[K, V], key: K, value: V, limit: int) -> None:
    if len(cache) >= limit:
        del cache[next(iter(cache))]
    cache[key] = value


# ---- the snapshot view ---------------------------------------------------------------------


class VisibleSnapshot:
    """A ``Snapshot`` that shows what ``view`` lets through (see the module docstring). One per
    request: it memoises the type of every entity it looked at, so each edge target and
    incoming source is read once.

    With a type half, a hidden entity answers exactly as the inner snapshot answers for an id
    it does not hold (no revision, no history, no incoming edges, an unknown alias). An entity
    is visible when the type of its current revision at the snapshot is; one without a current
    revision is not. Without a type half no entity is hidden and only the attribute half
    strips. Either way a transaction shows its metadata only.
    """

    def __init__(self, inner: Snapshot, view: ReadView) -> None:
        self._inner = inner
        self.view = view
        self._visible = view.types
        self._terms = None if view.types is None else frozenset(TermRef(t) for t in view.types)
        self._types: dict[EntityId, TermRef | None] = {}  # None: no current revision

    @property
    def seq(self) -> int:
        return self._inner.seq

    @property
    def warnings(self) -> tuple[WarningRecord, ...]:
        return self._inner.warnings

    # ---- what is visible -------------------------------------------------

    def sees(self, entity: EntityId) -> bool:
        """Whether ``entity`` exists at this snapshot with a visible type."""
        type_ = self._type_of(entity)
        return type_ is not None and self.view.shows_type(type_)

    def _hidden(self, entity: EntityId) -> bool:
        """Whether the type half hides ``entity`` (never without a type half)."""
        return self._visible is not None and not self.sees(entity)

    def _type_of(self, entity: EntityId) -> TermRef | None:
        if entity not in self._types:
            current = self._inner.get(entity)
            self._types[entity] = None if current is None else current.type
        return self._types[entity]

    def _prefetch(self, entities: Iterable[EntityId]) -> None:
        """Learn the type of every entity not known yet with one batched read."""
        unknown = [e for e in dict.fromkeys(entities) if e not in self._types]
        if not unknown:
            return
        found = self._inner.get_many(unknown)
        for entity in unknown:
            current = found.get(entity)
            self._types[entity] = None if current is None else current.type

    def strip_edges(self, revision: Revision) -> Revision:
        """``revision`` without its edges to hidden entities, attributes untouched and the
        content hash not recomputed: for reading it internally, never to hand it out."""
        if self._visible is None:
            return revision
        self._prefetch(edge.target for edge in revision.edges)
        return strip_revision(revision, self.sees, None, rehash=False)

    def _strip(self, revision: Revision) -> Revision:
        sees = None
        if self._visible is not None:
            self._prefetch(edge.target for edge in revision.edges)
            sees = self.sees
        return strip_revision(revision, sees, self.view.keeps(revision.type), rehash=True)

    # ---- the port --------------------------------------------------------

    def get(self, entity: EntityId) -> Revision | None:
        current = self._inner.get(entity)
        self._types[entity] = None if current is None else current.type
        if current is None or not self.view.shows_type(current.type):
            return None
        return self._strip(current)

    def get_many(self, entities: Sequence[EntityId]) -> Mapping[EntityId, Revision]:
        found = self._inner.get_many(entities)
        kept: dict[EntityId, Revision] = {}
        for entity in entities:
            current = found.get(entity)
            self._types[entity] = None if current is None else current.type
            if current is not None and self.view.shows_type(current.type):
                kept[entity] = current
        if self._visible is not None:
            self._prefetch(edge.target for revision in kept.values() for edge in revision.edges)
        return {entity: self._strip(r) for entity, r in kept.items()}

    def get_revision(self, entity: EntityId, rev: int) -> Revision | None:
        revision = self._inner.get_revision(entity, rev)
        if revision is None:
            return None
        current_type = self._type_of(entity)
        # An entity without a current revision at this snapshot is judged by the revision asked.
        if not self.view.shows_type(revision.type if current_type is None else current_type):
            return None
        return self._strip(revision)

    def history(self, entity: EntityId) -> Sequence[RevisionMeta]:
        if self._hidden(entity):
            return []
        found = []
        for meta in self._inner.history(entity):
            revision = self._inner.get_revision(entity, meta.rev)
            if revision is not None:
                stripped = self._strip(revision)
                if stripped is not revision:
                    meta = revision_meta(stripped)
            found.append(meta)
        return found

    def tx(self, seq: int) -> TxRecord | None:
        """The transaction's metadata only: what was changed is a body that names hidden
        entities and attributes, so the changeset, resolution, revisions, alias allocations and
        blobs are empty. No read command prints a tx body (a test keeps it so)."""
        record = self._inner.tx(seq)
        if record is None:
            return None
        return replace(
            record,
            changeset=EMPTY,
            resolution=EMPTY,
            revisions=(),
            alias_allocations=(),
            blobs=(),
        )

    def incoming(
        self, entity: EntityId, rel: TermRef | None = None
    ) -> Sequence[tuple[EntityId, EdgeRecord]]:
        if self._visible is None:
            return self._inner.incoming(entity, rel)
        if not self.sees(entity):
            return []
        found = self._inner.incoming(entity, rel)
        self._prefetch(source for source, _ in found)
        return [(source, edge) for source, edge in found if self.sees(source)]

    def resolve_alias(self, alias: str) -> tuple[EntityId, bool] | None:
        found = self._inner.resolve_alias(alias)
        if self._visible is None:
            return found
        return found if found is not None and self.sees(found[0]) else None

    def match_suffix(self, hex_suffix: str, *, limit: int = 10) -> Sequence[EntityId]:
        """The visible entities whose id ends with ``hex_suffix``; hidden ones never eat the
        limit, so the inner snapshot is asked without one."""
        if self._visible is None:
            return self._inner.match_suffix(hex_suffix, limit=limit)
        found = self._inner.match_suffix(hex_suffix, limit=sys.maxsize)
        self._prefetch(found)
        return [entity for entity in found if self.sees(entity)][:limit]

    def query(self, q: Query) -> QueryPage:
        return self._inner.query(
            replace(q, visible=self._narrowed(q.visible), attributes=self._masked(q.attributes))
        )

    def neighbors(self, q: NeighborQuery) -> Sequence[tuple[EntityId, EdgeRecord, Revision]]:
        return self._inner.neighbors(
            replace(q, visible=self._narrowed(q.visible), attributes=self._masked(q.attributes))
        )

    def _narrowed(self, asked: frozenset[TermRef] | None) -> frozenset[TermRef] | None:
        if self._terms is None:
            return asked
        return self._terms if asked is None else self._terms & asked

    def _masked(self, asked: AttributeMask | None) -> AttributeMask | None:
        """The attribute mask of the view, narrowed by one the query already carries."""
        own = self.view.attributes
        if own is None or asked is None:
            return asked if own is None else own
        theirs = dict(asked)
        return tuple((t, names & theirs[t]) for t, names in own if t in theirs)


# ---- the model view ------------------------------------------------------------------------


def filter_model(model: CompiledModel, visible: frozenset[str]) -> CompiledModel:
    """``model`` as if the types outside ``visible`` did not exist (a read view, never used on
    the write path):

    - Kept classes: every visible concrete class and every abstract class some visible
      concrete class is. A hidden concrete class is gone even when it has visible subclasses.
    - A kept class loses the removed ancestors (``extends`` is the first kept one); an
      attribute declared on a removed ancestor is declared on the top-most kept class of the
      chain that is a subclass of the original declarer.
    - A relation end that names a removed class names the maximal kept subclasses of it
      instead; a relation whose domain or range was named and is left empty is gone. An open end
      stays open.
    - A rule is dropped when its target is gone or its expression names a removed term; a
      guard that names one is dropped from its transition. The text match is a substring one:
      over-dropping is safe because the read side never evaluates rules.
    - Enums, lifecycles and datatypes that no kept attribute or qualifier uses are dropped,
      and a module that lost all its terms goes with its prefix. A module that has none in the
      full model either (a main module that only imports the areas) stays: nothing was hidden
      from it, and the functions and actions that name it as their module stay visible.
    """
    classes = model.classes
    visible_concrete = [c for c in classes if c in visible and not classes[c].abstract]
    kept = {
        name
        for name, cls in classes.items()
        if name in visible_concrete
        or (cls.abstract and any(model.is_a(v, name) for v in visible_concrete))
    }
    new_classes = {
        name: _kept_class(model, name, cls, kept) for name, cls in classes.items() if name in kept
    }
    new_relations = {
        name: new
        for name, relation in model.relations.items()
        if (new := _kept_relation(model, relation, kept)) is not None
    }
    used = _used_types(new_classes, new_relations)
    enums = {k: v for k, v in model.enums.items() if k in used}
    lifecycles = {k: v for k, v in model.lifecycles.items() if k in used}
    datatypes = {k: v for k, v in model.datatypes.items() if k in used}
    # Markings stay as they are: a type limit hides no attribute (field limits filter them).
    terms = {*new_classes, *new_relations, *enums, *lifecycles, *datatypes, *model.markings}
    removed = model.term_curies() - terms
    names_removed = _name_matcher(removed)
    rules = tuple(
        rule for rule in model.rules if rule.target in kept and not names_removed(rule.expr)
    )
    lifecycles = {k: _kept_guards(v, names_removed) for k, v in lifecycles.items()}
    all_terms = model.term_curies()
    modules = tuple(
        m for m in model.modules if _has_terms(m, terms) or not _has_terms(m, all_terms)
    )
    dropped = {m.prefix for m in model.modules} - {m.prefix for m in modules}
    prefixes = {p: ns for p, ns in model.prefixes.items() if p not in dropped}
    return CompiledModel(
        bundle_id=model.bundle_id,
        bundle_version=model.bundle_version,
        modules=modules,
        prefixes=prefixes,
        datatypes=datatypes,
        enums=enums,
        lifecycles=lifecycles,
        classes=new_classes,
        relations=new_relations,
        rules=rules,
        markings=model.markings,
    )


def _kept_class(model: CompiledModel, name: str, cls: ClassDef, kept: set[str]) -> ClassDef:
    ancestors = tuple(a for a in cls.ancestors if a in kept)
    chain = (name, *ancestors)  # nearest first, so the top-most is the last
    attributes = {
        attribute: a if a.declared_in in kept else _redeclared(model, a, chain)
        for attribute, a in cls.attributes.items()
    }
    return replace(
        cls,
        extends=ancestors[0] if ancestors else None,
        ancestors=ancestors,
        attributes=attributes,
    )


def _redeclared(
    model: CompiledModel, attribute: AttributeDef, chain: tuple[str, ...]
) -> AttributeDef:
    """``attribute`` declared in the top-most class of ``chain`` that is a subclass of the
    class that declared it (always at least the class itself)."""
    declarers = [c for c in chain if model.is_a(c, attribute.declared_in)]
    return replace(attribute, declared_in=declarers[-1] if declarers else chain[0])


def _kept_relation(
    model: CompiledModel, relation: RelationDef, kept: set[str]
) -> RelationDef | None:
    domain = _kept_ends(model, relation.domain, kept)
    range_ = _kept_ends(model, relation.range, kept)
    if (relation.domain and not domain) or (relation.range and not range_):
        return None
    return replace(relation, domain=domain, range=range_)


def _kept_ends(model: CompiledModel, ends: tuple[str, ...], kept: set[str]) -> tuple[str, ...]:
    """The classes of a relation end that remain: a kept class stays, a removed one is replaced
    by its maximal kept subclasses; deduplicated, in order."""
    found: dict[str, None] = {}
    for end in ends:
        if end in kept:
            found.setdefault(end)
            continue
        for name in model.classes:
            if name in kept and name != end and model.is_a(name, end):
                above = model.classes[name].ancestors
                if not any(a in kept and model.is_a(a, end) for a in above):
                    found.setdefault(name)
    return tuple(found)


def _used_types(classes: Mapping[str, ClassDef], relations: Mapping[str, RelationDef]) -> set[str]:
    """The datatypes, enums and lifecycles the kept attributes and qualifiers refer to."""
    used: set[str] = set()
    for cls in classes.values():
        used.update(a.type for a in cls.attributes.values() if a.kind != "base")
    for relation in relations.values():
        used.update(q.type for q in relation.qualifiers.values() if q.kind != "base")
    return used


def _name_matcher(removed: Iterable[str]) -> NamesRemoved:
    """A test: does the canonical JSON of an expression contain any of the ``removed`` CURIEs
    as a substring."""
    names = sorted(removed, key=len, reverse=True)
    if not names:
        return lambda expr: False
    pattern = re.compile("|".join(re.escape(n) for n in names))
    return lambda expr: pattern.search(canonical_bytes(expr).decode()) is not None


def _kept_guards(lifecycle: LifecycleDef, names_removed: NamesRemoved) -> LifecycleDef:
    transitions = tuple(
        replace(
            t,
            guards=tuple(g for g in t.guards if not names_removed(g.expr)),
        )
        for t in lifecycle.transitions
    )
    return replace(lifecycle, transitions=transitions)


def _has_terms(module: ModuleInfo, terms: Collection[str]) -> bool:
    prefix = f"{module.prefix}:"
    return any(t.startswith(prefix) for t in terms)
