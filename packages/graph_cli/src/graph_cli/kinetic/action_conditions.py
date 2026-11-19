"""The checks of the preconditions of an action (actions plan A9, check 5).

A precondition is a list of rule-language conditions on the entity a ``ref``
parameter names; ``ConditionCheck`` resolves the attribute, relation and type
names of one list against the model, as the ontology validator does for rule
conditions, and returns the conditions lowered to CURIEs. Problems go to the
shared ``CheckContext``.
"""

from __future__ import annotations

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kinetic.action_context import CheckContext
from graph_cli.ontology_layer.expressions import (
    AttrCond,
    AttrRef,
    Cond,
    CountCond,
    SumCond,
    iter_conditions,
    lower,
)
from graph_cli.ontology_layer.model import AttributeDef, CompiledModel


class ConditionCheck:
    """The checks of rule conditions in the ontology validator, on a ``CompiledModel``."""

    def __init__(self, ctx: CheckContext, scope: str, conds: tuple[Cond, ...]) -> None:
        self.ctx = ctx
        self.model = ctx.model
        self.scope = scope
        self.conds = conds
        self.rels: dict[str, str] = {}
        self.classes: dict[str, str] = {}

    def run(self) -> JsonValue | None:
        for cond in iter_conditions(self.conds):
            compares_attr = any(isinstance(value, AttrRef) for _, value in cond.ops)
            if isinstance(cond, AttrCond):
                self.scope_attribute(f"{cond.path}.attr", cond.attr, compares_attr)
            else:
                self.aggregate(cond)
            for op, value in cond.ops:
                if isinstance(value, AttrRef):
                    self.scope_attribute(f"{cond.path}.{op}.attr", value.attr, True)
        return lower(self.conds, lambda r: self.rels.get(r, r), lambda c: self.classes.get(c, c))

    def scope_attribute(self, path: str, name: str, numeric: bool) -> None:
        found = self.model.classes[self.scope].attributes.get(name)
        if found is None:
            self.ctx.unknown(
                "action.unknown_attribute",
                path,
                self.scope,
                name,
                list(self.model.classes[self.scope].attributes),
            )
        elif numeric and not _numeric(self.model, found):
            self.ctx.add(
                "action.not_numeric",
                path,
                f"{name!r} of {self.scope} is not a single integer or decimal value",
                name=name,
            )

    def aggregate(self, cond: CountCond | SumCond) -> None:
        kind = "count" if isinstance(cond, CountCond) else "sum"
        rel = self.ctx.term("relation", cond.rel, self.model.relations, f"{cond.path}.{kind}.rel")
        if rel is not None:
            self.rels[cond.rel] = rel
        types: set[str] = set()
        complete = True
        for ref in cond.types:
            curie = self.ctx.term("type", ref, self.model.classes, f"{cond.path}.{kind}.where.type")
            if curie is None:
                complete = False
            else:
                self.classes[ref] = curie
                types.add(curie)
        if isinstance(cond, SumCond) and rel is not None and complete:
            self.summed(cond, rel, types)

    def summed(self, cond: SumCond, rel: str, types: set[str]) -> None:
        definition = self.model.relations[rel]
        bases = set(definition.range if cond.direction == "out" else definition.domain)
        ends = []
        for curie, cls in self.model.classes.items():
            lineage = {curie, *cls.ancestors}
            if cls.abstract or (bases and lineage.isdisjoint(bases)):
                continue
            if not types or not lineage.isdisjoint(types):
                ends.append(curie)
        path = f"{cond.path}.sum.attr"
        missing, other = [], []
        for curie in ends:
            attribute = self.model.classes[curie].attributes.get(cond.attr)
            if attribute is None:
                missing.append(curie)
            elif not _numeric(self.model, attribute):
                other.append(curie)
        if missing:
            self.ctx.add(
                "action.unknown_attribute",
                path,
                f"{', '.join(missing)} at the other end of {rel} "
                f"{'has' if len(missing) == 1 else 'have'} no attribute {cond.attr!r}; "
                "narrow with where.type",
                name=cond.attr,
            )
        if other:
            self.ctx.add(
                "action.not_numeric",
                path,
                f"{cond.attr!r} of {other[0]} is not a single integer or decimal value",
                name=cond.attr,
            )


def _numeric(model: CompiledModel, attribute: AttributeDef) -> bool:
    if attribute.kind in ("lifecycle", "enum") or attribute.many:
        return False
    return model.value_base(attribute.kind, attribute.type) in ("integer", "decimal")
