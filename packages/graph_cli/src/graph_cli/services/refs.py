"""Reference resolution for one change set (contracts B1.8, B1.9, B3.8, B3.9, B6.9; plan W6).

Runs before the lock on a lock-free snapshot (authorization and early
errors, W.4) and again under the lock on the locked snapshot, because an
alias may have been renamed in between.

- **Full ids** are used as given. Op subjects (``id``, ``by``, supersede
  ``targets``) must exist (``entity.not_found``); edge targets are not
  checked here (the validator reports ``target_missing``).
- **Aliases** resolve through the snapshot; a former alias resolves with
  warning ``entity.alias_renamed``.
- **Locals** are declared by creates; a name declared twice, or already
  bound by an earlier document of the same invocation, is
  ``changeset.local_ref_duplicate``; a use without a binding is
  ``changeset.local_ref_unresolved``.
- **Abbreviations** (CLI arguments only, ``allow_abbrev``) match the
  trailing hex of ids; several matches are ``entity.ambiguous_ref`` with up
  to 10 candidates. They are replaced by full ids in the returned change
  set, which is what gets hashed and recorded.

The result also names the (action, type) pair of every touched entity for
authorization; types are immutable, so they are known before the lock. ``fine`` adds the
terms an op is decided on below its type (plan W4: ``attr`` and ``rel`` of the subject's type,
a create's inline edges as ``write.create``); an ``unlink`` by ``edge_id`` takes the relation
from the subject's revision on the same snapshot, and none when there is none (an ``@local``
of this document, an unknown edge: normalize refuses the op). A ``transition`` without
``attr`` moves the lifecycle the model names, which this module does not know:
``bare_transitions`` lists its types and the write service fills the term in. Terms outside
the name and CURIE grammar of resources are left out (validation reports them).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Literal
from uuid import UUID

from graph_cli.access_control.documents import CURIE_RE, NAME_RE
from graph_cli.kernel.actions import Action
from graph_cli.kernel.changeset import (
    ChangeSet,
    CreateOp,
    EdgeSpec,
    LinkOp,
    Op,
    RetractOp,
    SetAliasOp,
    SupersedeOp,
    TransitionOp,
    UnlinkOp,
    UpdateOp,
    op_action,
    op_attribute_names,
)
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ids import (
    AbbrevRef,
    AliasRef,
    EdgeId,
    EntityId,
    EntityRef,
    LocalRef,
    TermRef,
    parse_entity_ref,
    ref_text,
)
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kinetic.params import ParamDef
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.query.cypher.values import Value

MAX_CANDIDATES = 10
NO_BINDINGS: Mapping[str, EntityId] = MappingProxyType({})


FineTerm = tuple[Action, TermRef, str, str]  # (action, type, "attr" | "rel", name or CURIE)


def entity_of(ref: EntityRef, refs: Mapping[str, EntityId]) -> EntityId | None:
    """The existing entity a reference names; ``None`` for a create of this document."""
    match ref:
        case LocalRef(name):
            return refs.get(f"@{name}")
        case AliasRef(alias):
            return refs[alias]
    assert isinstance(ref, UUID)
    return ref


@dataclass(frozen=True, slots=True)
class Resolution:
    changeset: ChangeSet  # abbreviations replaced by full ids
    refs: Mapping[str, EntityId]  # alias text and "@name" of earlier bindings -> id (normalize)
    pairs: tuple[tuple[Action, TermRef], ...]  # sorted, unique (B6.9)
    warnings: tuple[WarningRecord, ...]
    fine: tuple[FineTerm, ...] = ()  # sorted, unique: the terms of W4, on the subject's type
    bare_transitions: tuple[TermRef, ...] = ()  # sorted, unique: types of a transition w/o attr


class _Resolver:
    def __init__(
        self,
        snapshot: Snapshot,
        bindings: Mapping[str, EntityId],
        allow_abbrev: bool,
        declared: Mapping[str, TermRef],
    ) -> None:
        self.snapshot = snapshot
        self.bindings = bindings
        self.allow_abbrev = allow_abbrev
        self.declared = declared  # local name -> type of its create
        self.refs: dict[str, EntityId] = {f"@{n}": e for n, e in bindings.items()}
        self.warnings: dict[str, WarningRecord] = {}

    def ref(self, ref: EntityRef, path: str, *, subject: bool) -> EntityRef:
        """The reference to keep in the change set; full ids for abbreviations."""
        match ref:
            case LocalRef(name):
                if name not in self.declared and name not in self.bindings:
                    raise GraphCliError(
                        "changeset.local_ref_unresolved",
                        f"{path}: @{name} is not bound",
                        details={"name": name, "path": path},
                    )
                return ref
            case AliasRef(alias):
                bound = self.snapshot.resolve_alias(alias)
                if bound is None:
                    raise _not_found(alias, path)
                entity, current = bound
                if not current:
                    revision = self.snapshot.get(entity)
                    self.warnings[alias] = WarningRecord(
                        "entity.alias_renamed",
                        f"{alias} was renamed",
                        {
                            "alias": alias,
                            "id": str(entity),
                            "current": None if revision is None else revision.alias,
                        },
                    )
                self.refs[alias] = entity
                return ref
            case AbbrevRef(suffix):
                if not self.allow_abbrev:
                    raise GraphCliError(
                        "changeset.malformed",
                        f"{path}: abbreviated ids are not allowed in change sets (B1.9)",
                        details={"path": path},
                    )
                found = self.snapshot.match_suffix(suffix, limit=MAX_CANDIDATES + 1)
                if not found:
                    raise _not_found(suffix, path)
                if len(found) > 1:
                    raise GraphCliError(
                        "entity.ambiguous_ref",
                        f"{suffix} matches {len(found)} entities",
                        details={
                            "ref": suffix,
                            "path": path,
                            "candidates": [str(e) for e in found[:MAX_CANDIDATES]],
                        },
                    )
                return found[0]
        if subject and self.snapshot.get(ref) is None:
            raise _not_found(ref_text(ref), path)
        return ref

    def entity_of(self, ref: EntityRef) -> EntityId | None:
        """The existing entity of a rewritten reference; ``None`` for a local this document
        creates."""
        if isinstance(ref, LocalRef):
            return None if ref.name in self.declared else self.bindings[ref.name]
        if isinstance(ref, AliasRef):
            return self.refs[ref.alias]
        assert not isinstance(ref, AbbrevRef)
        return ref

    def type_of(self, ref: EntityRef) -> TermRef:
        if isinstance(ref, LocalRef) and ref.name in self.declared:
            return self.declared[ref.name]
        entity = self.entity_of(ref)
        assert entity is not None
        revision = self.snapshot.get(entity)
        if revision is None:
            raise _not_found(ref_text(ref), "")
        return revision.type

    def edge_rel(self, ref: EntityRef, edge_id: EdgeId) -> TermRef | None:
        """The relation of edge ``edge_id`` on the subject's revision; ``None`` when the
        subject has none here or no such edge."""
        entity = self.entity_of(ref)
        revision = None if entity is None else self.snapshot.get(entity)
        if revision is None:
            return None
        return next((e.rel for e in revision.edges if e.edge_id == edge_id), None)


def resolve_ref(text: str, snapshot: Snapshot) -> tuple[EntityId, tuple[WarningRecord, ...]]:
    """One reference from a CLI argument: full id, alias or abbreviation (B1.9).

    A full id is returned as given (the caller checks existence where it matters).
    """
    ref = parse_entity_ref(text, allow_abbrev=True)
    if ref is None:
        raise GraphCliError(
            "usage.invalid_argument",
            f"not an entity reference: {text!r}",
            details={"expected": "a full id, an alias or 8-32 trailing hex digits", "actual": text},
        )
    resolver = _Resolver(snapshot, NO_BINDINGS, True, {})
    found = resolver.ref(ref, "", subject=False)
    assert not isinstance(found, LocalRef | AbbrevRef)
    entity = resolver.refs[found.alias] if isinstance(found, AliasRef) else found
    return entity, tuple(resolver.warnings.values())


def entity_params(
    snapshot: Snapshot,
    model: CompiledModel,
    kind: Literal["function", "action", "view"],
    name: str,
    param: ParamDef,
    bound: Mapping[str, Value],
) -> tuple[Value, list[WarningRecord]]:
    """The canonical ids for the reference texts of a ``ref`` parameter of a function, an
    action or a view, each checked to exist and to be of the declared class or a subclass (no name
    resolution). Errors name the function, action or view under ``details[kind]``;
    ``<kind>.parameter_type`` is the wrong class."""
    assert param.of_class is not None
    given = bound[param.name]
    texts = given if isinstance(given, list) else [given]
    warnings: list[WarningRecord] = []
    ids: list[Value] = []
    for text in texts:
        assert isinstance(text, str)
        entity, found = resolve_ref(text, snapshot)
        warnings += found
        revision = snapshot.get(entity)
        if revision is None:
            raise GraphCliError(
                "entity.not_found",
                f"no entity {text} (parameter {param.name!r} of {name})",
                details={"ref": text, "id": str(entity), kind: name},
            )
        if not model.is_a(revision.type, param.of_class):
            raise GraphCliError(
                f"{kind}.parameter_type",
                f"parameter {param.name!r} of {name} expects {param.of_class}, "
                f"{text} is a {revision.type}",
                details={
                    kind: name,
                    "parameter": param.name,
                    "expected": param.of_class,
                    "found": revision.type,
                },
            )
        ids.append(str(entity))
    return (ids if isinstance(given, list) else ids[0]), warnings


def _not_found(text: str, path: str) -> GraphCliError:
    return GraphCliError(
        "entity.not_found", f"no entity {text}", details={"ref": text, "path": path}
    )


def _declared_locals(cs: ChangeSet, bindings: Mapping[str, EntityId]) -> dict[str, TermRef]:
    declared: dict[str, TermRef] = {}
    for index, op in enumerate(cs.ops):
        if isinstance(op, CreateOp) and op.local is not None:
            if op.local in declared or op.local in bindings:
                raise GraphCliError(
                    "changeset.local_ref_duplicate",
                    f"ops[{index}].local: @{op.local} is bound twice",
                    details={"name": op.local, "path": f"ops[{index}].local"},
                )
            declared[op.local] = op.type
    return declared


def subjects(op: Op) -> list[EntityRef]:
    """The entities an op touches besides the ones it creates."""
    match op:
        case SupersedeOp():
            return [op.by, *op.targets]
        case RetractOp():
            return [op.id] if op.by is None else [op.id, op.by]
        case CreateOp():
            return []
    return [op.id]


def _rewrite(op: Op, fix: Callable[[EntityRef, str, bool], EntityRef], path: str) -> Op:
    def edge(spec: EdgeSpec, at: str) -> EdgeSpec:
        return replace(spec, target=fix(spec.target, f"{at}.target", False))

    match op:
        case CreateOp():
            return replace(
                op, edges=tuple(edge(e, f"{path}.edges[{i}]") for i, e in enumerate(op.edges))
            )
        case UpdateOp() | TransitionOp() | SetAliasOp():
            return replace(op, id=fix(op.id, f"{path}.id", True))
        case LinkOp():
            return replace(op, id=fix(op.id, f"{path}.id", True), edge=edge(op.edge, path))
        case UnlinkOp():
            target = None if op.target is None else fix(op.target, f"{path}.target", False)
            return replace(op, id=fix(op.id, f"{path}.id", True), target=target)
        case SupersedeOp():
            targets = tuple(fix(t, f"{path}.targets[{i}]", True) for i, t in enumerate(op.targets))
            return replace(op, by=fix(op.by, f"{path}.by", True), targets=targets)
        case RetractOp():
            by = None if op.by is None else fix(op.by, f"{path}.by", True)
            return replace(op, id=fix(op.id, f"{path}.id", True), by=by)


def resolve_refs(
    cs: ChangeSet,
    snapshot: Snapshot,
    *,
    bindings: Mapping[str, EntityId] = NO_BINDINGS,
    allow_abbrev: bool = False,
) -> Resolution:
    """Resolve every reference of ``cs`` against ``snapshot`` (B1.9 order)."""
    declared = _declared_locals(cs, bindings)
    resolver = _Resolver(snapshot, bindings, allow_abbrev, declared)

    def fix(ref: EntityRef, path: str, subject: bool) -> EntityRef:
        return resolver.ref(ref, path, subject=subject)

    ops = tuple(_rewrite(op, fix, f"ops[{i}]") for i, op in enumerate(cs.ops))
    pairs: set[tuple[Action, TermRef]] = set()
    for op in ops:
        action = op_action(op)
        if isinstance(op, CreateOp):
            pairs.add((action, op.type))
        for ref in subjects(op):
            pairs.add((action, resolver.type_of(ref)))
    fine, bare = _fine_terms(ops, resolver)
    return Resolution(
        changeset=replace(cs, ops=ops),
        refs=dict(resolver.refs),
        pairs=tuple(sorted(pairs)),
        warnings=tuple(resolver.warnings[k] for k in sorted(resolver.warnings)),
        fine=fine,
        bare_transitions=bare,
    )


def _fine_terms(
    ops: Sequence[Op], resolver: _Resolver
) -> tuple[tuple[FineTerm, ...], tuple[TermRef, ...]]:
    """The terms of W4 for rewritten ops (see the module docstring)."""
    fine: set[FineTerm] = set()
    bare: set[TermRef] = set()

    def rel(action: Action, subject: TermRef, name: TermRef | None) -> None:
        if name is not None and CURIE_RE.match(name):
            fine.add((action, subject, "rel", name))

    for op in ops:
        action = op_action(op)
        match op:
            case CreateOp():
                for edge in op.edges:
                    rel(action, op.type, edge.rel)
                subject = op.type
            case UpdateOp():
                subject = resolver.type_of(op.id)
            case LinkOp():
                rel(action, resolver.type_of(op.id), op.edge.rel)
                continue
            case UnlinkOp():
                found = op.rel if op.edge_id is None else resolver.edge_rel(op.id, op.edge_id)
                rel(action, resolver.type_of(op.id), found)
                continue
            case TransitionOp():
                subject = resolver.type_of(op.id)
                if op.attr is None:
                    bare.add(subject)
                elif NAME_RE.match(op.attr):
                    fine.add((action, subject, "attr", op.attr))
                continue
            case _:
                continue
        fine.update(
            (action, subject, "attr", n) for n in op_attribute_names(op) if NAME_RE.match(n)
        )
    return tuple(sorted(fine)), tuple(sorted(bare))
