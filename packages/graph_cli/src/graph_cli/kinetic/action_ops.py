"""The op checks of one action: what each op kind names, and the footprint it leaves.

``action_compile`` hands every op to ``OpChecks.dispatch`` after it has counted
the placeholder uses. The checks resolve the types, relations and entities an op
names (a ``$ref`` parameter or an ``@local`` of a create), type the values and the
one-position placeholders with ``action_slots`` and report to the ``CheckContext``.
The types, relations and ``write.*`` needs the ops (and the preconditions of the
driver) touch are recorded in one ``Footprint`` the driver reads for ``touches``
and ``needs``.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from dataclasses import dataclass

from graph_cli.kernel.canonical import JsonObject, JsonValue
from graph_cli.kernel.changeset import OP_ACTIONS
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kinetic.action_context import CheckContext
from graph_cli.kinetic.action_format import OpScan, is_dynamic_unlink, path_text
from graph_cli.kinetic.action_slots import SlotChecks
from graph_cli.ontology_layer.connecting import connecting
from graph_cli.ontology_layer.model import AttributeDef, CompiledModel
from graph_cli.query.plan import resolve_term


@dataclass(frozen=True, slots=True)
class _End:
    """The entity an op names: the classes it can have and the class it is declared as."""

    classes: frozenset[str]
    subject: str  # the class the write is authorized as
    label: str


class Footprint:
    """What the ops and preconditions of one action touch: types, relations, needs."""

    def __init__(self) -> None:
        self.types: set[str] = set()
        self.relations: set[str] = set()
        self.needs: set[tuple[str, str]] = set()

    def need(self, op_kind: str, subject: str) -> None:
        self.needs.add((OP_ACTIONS[op_kind], subject))
        self.types.add(subject)


class OpChecks:
    """The checks of the ops of one action; they report to the shared context."""

    def __init__(self, ctx: CheckContext, slots: SlotChecks, footprint: Footprint) -> None:
        self.ctx = ctx
        self.slots = slots
        self.footprint = footprint
        self.locals: dict[str, str] = {}  # local name -> create type (CURIE)

    # Locals

    def prepare_locals(self) -> None:
        """The type each ``@local`` of a create stands for (errors are reported at the op)."""
        seen: set[str] = set()
        for index, op in enumerate(self.ctx.defn.ops):
            local = op.get("local")
            if op.get("op") != "create" or not isinstance(local, str):
                continue
            if local in seen:
                self.ctx.add(
                    "action.local_duplicate",
                    f"ops[{index}].local",
                    f"@{local} is bound twice",
                    name=local,
                )
                continue
            seen.add(local)
            kind = op.get("type")
            if isinstance(kind, str):
                with contextlib.suppress(GraphCliError):
                    self.locals[local] = resolve_term("type", kind, self.ctx.model.classes)

    def check_locals(self, scan: OpScan, index: int) -> None:
        for path, name in scan.locals_used:
            if name not in self.locals:
                self.ctx.add(
                    "action.local_unresolved",
                    path_text(path),
                    f"@{name} is not created by an op of this action",
                    name=name,
                )

    # Dispatch

    def dispatch(self, index: int, op: JsonObject, out: dict[str, JsonValue]) -> None:
        kind = op["op"]
        if kind == "create":
            self.create(index, op, out)
        elif kind == "update":
            self.update(index, op)
        elif kind == "link":
            self.link(index, op, out)
        elif kind == "unlink":
            self.unlink(index, op, out)
        elif kind == "transition":
            self.transition(index, op)
        elif kind == "supersede":
            self.supersede(index, op)
        elif kind == "retract":
            self.retract(index, op)
        elif kind == "set_alias":
            self.set_alias(index, op)

    # Entity references

    def end(
        self, index: int, op: Mapping[str, JsonValue], key: str, role: str = "entity"
    ) -> _End | None:
        value = op.get(key)
        if isinstance(value, str):
            return self.end_of(value, ("ops", index, key), role)
        return None

    def end_of(self, text: str, path: tuple[str | int, ...], role: str = "entity") -> _End | None:
        if text.startswith("@"):
            found = self.locals.get(text[1:])
            return None if found is None else _End(frozenset({found}), found, f"@{text[1:]}")
        use = self.ctx.uses.get(path)
        if use is None:
            return None
        param = self.ctx.declared_param(use)
        if param is None:
            return None
        if param.type != "ref" or param.many != (role == "entities"):
            wanted = "a list of references" if role == "entities" else "one reference"
            self.ctx.add(
                "action.type_mismatch",
                path_text(path),
                f"parameter {param.name!r} is {param.type_text}; this position takes {wanted}",
                parameter=param.name,
            )
            return None
        if param.of_class not in self.ctx.model.classes:
            return None  # reported at the parameter
        assert param.of_class is not None
        self.footprint.types.add(param.of_class)
        return _End(
            frozenset(self.ctx.model.subtypes(param.of_class)),
            param.of_class,
            f"${param.name} ({param.of_class})",
        )

    def ends_of(self, index: int, op: Mapping[str, JsonValue], key: str) -> list[_End]:
        value = op.get(key)
        path = ("ops", index, key)
        if isinstance(value, str):  # a many parameter
            found = self.end_of(value, path, "entities")
            return [] if found is None else [found]
        result: list[_End] = []
        for i, item in enumerate(value if isinstance(value, list) else []):
            if isinstance(item, str):
                found = self.end_of(item, (*path, i))
                if found is not None:
                    result.append(found)
        return result

    def time(self, index: int, op: Mapping[str, JsonValue]) -> None:
        for key in ("valid_from", "valid_to"):
            use = self.ctx.uses.get(("ops", index, key))
            param = None if use is None else self.ctx.declared_param(use)
            if (
                use is not None
                and param is not None
                and (param.type not in ("date", "datetime") or param.many)
            ):
                self.ctx.add(
                    "action.type_mismatch",
                    path_text(use.path),
                    f"parameter {param.name!r} ({param.type_text}) cannot stand for {key}; "
                    "it takes one date or datetime",
                    parameter=param.name,
                )

    # Relations

    def relation(self, path: str, name: object) -> str | None:
        """The CURIE of the relation an op names (recorded in ``touches``)."""
        if not isinstance(name, str):
            return None
        curie = self.ctx.term("relation", name, self.ctx.model.relations, path)
        if curie is not None:
            self.footprint.relations.add(curie)
        return curie

    def edge(
        self,
        path: str,
        rel: str,
        source: _End | None,
        target: _End | None,
    ) -> None:
        """Domain and range of ``rel`` against the classes the two ends can have."""
        definition = self.ctx.model.relations[rel]
        ok_source = source is None or _accepts(self.ctx.model, definition.domain, source.classes)
        ok_target = target is None or _accepts(self.ctx.model, definition.range, target.classes)
        if ok_source and ok_target:
            return
        details: dict[str, JsonValue] = {"relation": rel}
        names = []
        if source is not None:
            details["source"] = source.subject
            names.append(source.label)
        if target is not None:
            details["target"] = target.subject
            names.append(target.label)
        if source is not None and target is not None:
            details["connecting"] = connecting(
                self.ctx.model,
                _general(self.ctx.model, source.classes),
                _general(self.ctx.model, target.classes),
            )
        which = "domain" if not ok_source else "range"
        self.ctx.add(
            "action.op_impossible",
            path,
            f"{rel} cannot connect {' to '.join(names)} (its {which} does not accept it); "
            "the write would always be refused",
            **details,
        )

    # Op kinds

    def create(self, index: int, op: JsonObject, out: dict[str, JsonValue]) -> None:
        where = f"ops[{index}]"
        curie = self.ctx.term("type", str(op["type"]), self.ctx.model.classes, f"{where}.type")
        if curie is None:
            return
        out["type"] = curie
        self.footprint.types.add(curie)
        self.footprint.needs.add((OP_ACTIONS["create"], curie))
        if self.ctx.model.classes[curie].abstract:
            self.ctx.add(
                "action.abstract_type",
                f"{where}.type",
                f"{curie} is abstract; a create names a concrete class",
                curie=curie,
                expected=[
                    c
                    for c in self.ctx.model.subtypes(curie)
                    if not self.ctx.model.classes[c].abstract
                ],
            )
            return
        slots = self.slots.attribute_slots(curie)
        for key in ("attrs", "attrs_text"):
            self.slots.value_map(index, op, key, slots, curie, "action.unknown_attribute")
        self.time(index, op)
        source = _End(frozenset({curie}), curie, curie)
        edges = op.get("edges")
        normalized = []
        for i, edge in enumerate(edges if isinstance(edges, list) else []):
            if not isinstance(edge, Mapping):
                continue
            base = ("ops", index, "edges", i)
            rel_path = path_text((*base, "rel"))
            found = self.relation(rel_path, edge.get("rel"))
            copy = dict(edge)
            target = (
                self.end_of(str(edge["target"]), (*base, "target")) if "target" in edge else None
            )
            if found is None:
                normalized.append(copy)
                continue
            copy["rel"] = found
            normalized.append(copy)
            self.edge(path_text(base), found, source, target)
            self.slots.value_map(
                index,
                edge,
                "qualifiers",
                self.slots.qualifier_slots(found),
                found,
                "action.unknown_qualifier",
                base=base,
            )
        if normalized:
            out["edges"] = normalized

    def update(self, index: int, op: JsonObject) -> None:
        end = self.end(index, op, "id")
        if end is None:
            return
        self.footprint.need("update", end.subject)
        slots = self.slots.attribute_slots(end.subject)
        for key in ("set", "set_text"):
            self.slots.value_map(
                index, op, key, slots, end.subject, "action.unknown_attribute", direct=True
            )
        unset = op.get("unset")
        for i, name in enumerate(unset if isinstance(unset, list) else []):
            self.slots.unset_name(index, i, str(name), slots, end.subject)
        self.slots.list_map(index, op, slots, end.subject)

    def link(self, index: int, op: JsonObject, out: dict[str, JsonValue]) -> None:
        where = f"ops[{index}]"
        source = self.end(index, op, "id")
        target = self.end(index, op, "target")
        if source is not None:
            self.footprint.need("link", source.subject)
        found = self.relation(f"{where}.rel", op.get("rel"))
        if found is None:
            return
        out["rel"] = found
        self.edge(where, found, source, target)
        self.slots.value_map(
            index,
            op,
            "qualifiers",
            self.slots.qualifier_slots(found),
            found,
            "action.unknown_qualifier",
        )

    def unlink(self, index: int, op: JsonObject, out: dict[str, JsonValue]) -> None:
        where = f"ops[{index}]"
        source = self.end(index, op, "id")
        found = self.relation(f"{where}.rel", op.get("rel"))
        if found is not None:
            out["rel"] = found
        if is_dynamic_unlink(op):
            direction = str(op.get("direction", "out"))
            out["direction"] = direction
            subject = op.get("id")
            if isinstance(subject, str) and subject.startswith("@"):
                self.ctx.add(
                    "action.invalid_op",
                    f"{where}.id",
                    "all: true reads the edges of an existing entity; it takes a reference "
                    "parameter, not an @local",
                )
                return
            if source is None or found is None:
                return
            definition = self.ctx.model.relations[found]
            accepted = definition.domain if direction == "in" else definition.range
            end_side = definition.range if direction == "in" else definition.domain
            if not _accepts(self.ctx.model, end_side, source.classes):
                self.ctx.add(
                    "action.op_impossible",
                    where,
                    f"{found} never has {source.label} as its "
                    f"{'target' if direction == 'in' else 'source'}; the unlink would find nothing",
                    relation=found,
                    direction=direction,
                    entity=source.subject,
                    expected=list(end_side),
                )
                return
            if direction == "in":
                for cls in accepted:  # the edges are removed at their sources
                    self.footprint.need("unlink", cls)
            else:
                self.footprint.need("unlink", source.subject)
            return
        target = self.end(index, op, "target")
        if source is not None:
            self.footprint.need("unlink", source.subject)
        if found is not None:
            self.edge(where, found, source, target)

    def transition(self, index: int, op: JsonObject) -> None:
        end = self.end(index, op, "id")
        if end is None:
            return
        self.footprint.need("transition", end.subject)
        where = f"ops[{index}]"
        attributes = self.ctx.model.classes[end.subject].attributes
        lifecycles = {n: a for n, a in attributes.items() if a.kind == "lifecycle"}
        attr = op.get("attr")
        chosen: AttributeDef | None
        if isinstance(attr, str):
            chosen = lifecycles.get(attr)
            if chosen is None:
                code = (
                    "action.lifecycle_missing" if attr in attributes else "action.unknown_attribute"
                )
                self.ctx.add(
                    code,
                    f"{where}.attr",
                    f"{attr!r} is not a lifecycle attribute of {end.subject}",
                    name=attr,
                    expected=sorted(lifecycles),
                )
                return
        elif len(lifecycles) == 1:
            chosen = next(iter(lifecycles.values()))
        else:
            self.ctx.add(
                "action.lifecycle_missing",
                where,
                f"{end.subject} has no lifecycle attribute"
                if not lifecycles
                else f"{end.subject} has several lifecycle attributes; name one with attr",
                expected=sorted(lifecycles),
            )
            return
        states = self.ctx.model.lifecycles[chosen.type].state_names()
        if op.get("to") not in states:
            self.ctx.add(
                "action.unknown_state",
                f"{where}.to",
                f"{op.get('to')!r} is not a state of {chosen.type}",
                name=str(op.get("to")),
                expected=list(states),
            )

    def supersede(self, index: int, op: JsonObject) -> None:
        by = self.end(index, op, "by")
        if by is not None:
            self.footprint.need("supersede", by.subject)
        for end in self.ends_of(index, op, "targets"):
            self.footprint.need("supersede", end.subject)

    def retract(self, index: int, op: JsonObject) -> None:
        end = self.end(index, op, "id")
        if end is not None:
            self.footprint.need("retract", end.subject)
        by = self.end(index, op, "by")
        if by is not None:
            self.footprint.need("retract", by.subject)

    def set_alias(self, index: int, op: JsonObject) -> None:
        end = self.end(index, op, "id")
        if end is not None:
            self.footprint.need("set_alias", end.subject)
        use = self.ctx.uses.get(("ops", index, "alias"))
        param = None if use is None else self.ctx.declared_param(use)
        if use is not None and param is not None and (param.type != "string" or param.many):
            self.ctx.add(
                "action.type_mismatch",
                path_text(use.path),
                f"parameter {param.name!r} ({param.type_text}) cannot stand for an alias; "
                "it takes one string",
                parameter=param.name,
            )


def _accepts(model: CompiledModel, accepted: tuple[str, ...], classes: frozenset[str]) -> bool:
    return any(model.is_a_any(c, accepted) for c in classes)


def _general(model: CompiledModel, classes: frozenset[str]) -> str:
    """The most general of the classes: the one the hint should start from."""
    return min(classes, key=lambda c: (len(model.classes[c].ancestors), len(c), c))
