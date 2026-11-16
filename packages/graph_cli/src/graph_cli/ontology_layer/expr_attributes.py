"""Which attributes of which classes a compiled expression reads (field visibility plan F2,
F5).

An expression names attributes by their bare name (``{"attr": "x"}``, ``sum: {rel, attr,
where}``, B7.15), so what it reads depends on where it is evaluated: an ``attr`` node reads the
attribute on the entity the expression runs on, a ``sum`` reads it on the entities at the far
end of its relation. ``reads`` walks an expression and yields, per attribute operand, the
classes whose entities it is read on; the release warnings about marked attributes in
expressions and the model view of a reader without a marking share it.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping

from graph_cli.kernel.canonical import JsonValue
from graph_cli.ontology_layer.expr_ast import walk
from graph_cli.ontology_layer.model import CompiledModel, RuleDef


def reads(
    model: CompiledModel, classes: tuple[str, ...], expr: JsonValue
) -> Iterator[tuple[tuple[str, ...], str]]:
    """``(classes, attribute)`` for every attribute operand of ``expr`` evaluated on entities of
    ``classes``: an ``attr`` node reads on ``classes``, a ``sum`` on ``sum_classes``."""
    for kind, arg, _ in walk(expr):
        if kind == "attr" and isinstance(arg, str):
            yield classes, arg
        elif kind == "sum" and isinstance(arg, Mapping) and isinstance(arg.get("attr"), str):
            yield sum_classes(model, arg), str(arg["attr"])


def sum_classes(model: CompiledModel, spec: Mapping[str, JsonValue]) -> tuple[str, ...]:
    """The classes whose entities a ``sum`` reads the attribute of: the far end of its
    relation, narrowed by ``where.type`` when that names classes of the model; every class when
    the relation's end is open or unknown."""
    relation = model.relations.get(str(spec.get("rel")))
    end = (
        ()
        if relation is None
        else (relation.domain if spec.get("direction") == "in" else relation.range)
    )
    reachable = {c for curie in end for c in model.subtypes(curie)} or set(model.classes)
    where = spec.get("where")
    types = where.get("type") if isinstance(where, Mapping) else None
    if isinstance(types, list):
        named = {c for t in types for c in model.subtypes(str(t))}
        reachable = (reachable & named) or reachable
    return tuple(sorted(reachable))


def rule_classes(model: CompiledModel, rule: RuleDef) -> tuple[str, ...]:
    """The classes a rule is evaluated on: its target and every subclass."""
    return model.subtypes(rule.target) if rule.target in model.classes else ()


def guard_classes(model: CompiledModel, lifecycle: str) -> tuple[str, ...]:
    """The classes a guard of ``lifecycle`` is evaluated on: those with an attribute the
    lifecycle moves, sorted."""
    return tuple(
        sorted(
            curie
            for curie, cls in model.classes.items()
            if any(a.kind == "lifecycle" and a.type == lifecycle for a in cls.attributes.values())
        )
    )
