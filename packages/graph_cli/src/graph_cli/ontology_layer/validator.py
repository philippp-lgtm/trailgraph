"""Model validation at release time: ``validate_model`` (contracts B7.9, B7.13).

Runs on a loaded bundle (parse and import findings come first and stop the
pipeline, so findings here never cascade from a broken document). Findings
are sorted deterministically.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterable

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.findings import Finding, sort_findings
from graph_cli.ontology_layer.bundle import IrBundle, TermKind
from graph_cli.ontology_layer.expressions import AttrCond, Cond, CountCond, iter_conditions
from graph_cli.ontology_layer.ir import IrAttribute, IrClass, IrDatatype, IrEnum, IrLifecycle
from graph_cli.ontology_layer.report import Report
from graph_cli.ontology_layer.values import check_facets, coerce_text
from graph_cli.ontology_layer.vocabulary import (
    BASE_TYPES,
    EDGE_FIELDS,
    FACETS_BY_BASE,
    NON_SCALAR_BASES,
    SYSTEM_FIELDS,
)
from graph_cli.ontology_layer.yaml_strict import YamlScalar


def validate_model(bundle: IrBundle) -> list[Finding]:
    report = Report()
    _Validator(bundle, report).run()
    return sort_findings(report.findings)


def attribute_default(
    bundle: IrBundle, module_id: str, attribute: IrAttribute
) -> tuple[JsonValue, str | None]:
    """The typed default of an attribute and an error message if it does not fit."""
    node = attribute.default
    if node is None:
        return None, None
    if attribute.lifecycle is not None:
        return None, "lifecycle attributes start in the initial state; no default allowed"
    if attribute.many:
        return None, "attributes with many: true take no default"
    if attribute.type is None:
        return None, None
    resolved = bundle.resolve_type(module_id, attribute.type)
    if resolved is None:
        return None, None  # reported as unknown_datatype
    kind, type_ref = resolved
    if not isinstance(node, YamlScalar) or node.is_null():
        return None, "a default must be a scalar"
    if kind == "enum":
        found = bundle.term(type_ref, "enum")
        assert found is not None
        enum = found[1]
        assert isinstance(enum, IrEnum)
        names = [v.name for v in enum.values]
        if node.text not in names:
            return None, f"{node.text!r} is not one of {', '.join(names)}"
        return node.text, None
    facets: dict[str, JsonValue] = {}
    base = type_ref
    if kind == "datatype":
        found = bundle.term(type_ref, "datatype")
        assert found is not None
        datatype = found[1]
        assert isinstance(datatype, IrDatatype)
        base, facets = datatype.base, datatype.facets
    if base in NON_SCALAR_BASES:
        return None, f"attributes of type {base} take no default"
    try:
        if base == "integer":
            value: JsonValue = node.as_int()
            if value is None:
                raise ValueError(f"{node.text!r} is not an integer")
        elif base == "boolean":
            value = node.as_bool()
            if value is None:
                raise ValueError(f"{node.text!r} is not true or false")
        else:
            value = coerce_text(base, node.text)
    except ValueError as exc:
        return None, str(exc)
    violation = check_facets(value, facets)
    return (None, violation) if violation else (value, None)


_SUGGEST_KINDS: dict[str, tuple[TermKind, ...]] = {
    "class": ("class",),
    "lifecycle": ("lifecycle",),
    "relation": ("relation",),
    "type": ("datatype", "enum"),
}


class _Validator:
    def __init__(self, bundle: IrBundle, report: Report) -> None:
        self.b = bundle
        self.report = report

    def add(self, code: str, module: str, path: str, message: str, **extra: JsonValue) -> None:
        self.report.add(code, module, path, message, **extra)  # type: ignore[arg-type]

    def unknown(self, code: str, module_id: str, path: str, what: str, ref: str) -> None:
        """A reference that does not resolve; names close candidates in message and ``expected``."""
        extra = sorted(BASE_TYPES) if what == "type" else ()
        found = self.b.suggest(module_id, ref, _SUGGEST_KINDS[what], extra)
        hint = "; did you mean " + " or ".join(repr(name) for name in found) + "?" if found else ""
        message = f"{what} {ref!r} does not exist{hint}"
        self.add(code, module_id, path, message, expected=list(found) or None, actual=ref)

    def run(self) -> None:
        for module_id in self.b.order:
            self.duplicate_terms(module_id)
            self.datatypes(module_id)
            self.classes(module_id)
            self.relations(module_id)
            self.lifecycles(module_id)
            self.rules(module_id)
            self.mappings(module_id)
        self.inheritance()
        self.inverse_labels()

    # ---- structural ----------------------------------------------------------------------

    def duplicate_terms(self, module_id: str) -> None:
        module = self.b.modules[module_id]
        seen: dict[str, str] = {}
        sections: Iterable[tuple[str, Iterable[str]]] = (
            ("datatypes", module.datatypes),
            ("enums", module.enums),
            ("classes", module.classes),
            ("lifecycles", module.lifecycles),
        )
        for section, names in sections:
            for name in names:
                if name in seen:
                    self.add(
                        "model.structural.duplicate_term",
                        module_id,
                        f"{section}.{name}",
                        f"{name!r} is already defined in {seen[name]}",
                    )
                seen.setdefault(name, section)
        ids: dict[str, str] = {}
        guard_ids = [
            (g.id, g.path)
            for lc in module.lifecycles.values()
            for t in lc.transitions
            for g in t.guards
        ]
        for item_id, path in [*((r.id, r.path) for r in module.rules), *guard_ids]:
            if item_id in ids:
                self.add(
                    "model.structural.duplicate_term",
                    module_id,
                    path,
                    f"id {item_id!r} is already used at {ids[item_id]}",
                )
            ids.setdefault(item_id, path)

    def datatypes(self, module_id: str) -> None:
        for datatype in self.b.modules[module_id].datatypes.values():
            if datatype.base not in BASE_TYPES:
                self.add(
                    "model.structural.unknown_datatype",
                    module_id,
                    f"{datatype.path}.base",
                    f"{datatype.base!r} is not a base type",
                    actual=datatype.base,
                )
                continue
            allowed = FACETS_BY_BASE.get(datatype.base, frozenset())
            for facet in sorted(datatype.facets):
                if facet not in allowed:
                    self.add(
                        "model.structural.facet_not_applicable",
                        module_id,
                        f"{datatype.path}.{facet}",
                        f"facet {facet!r} does not apply to {datatype.base}",
                        expected=sorted(allowed),
                        actual=facet,
                    )
            pattern = datatype.facets.get("pattern")
            if isinstance(pattern, str):
                self.pattern(module_id, f"{datatype.path}.pattern", pattern)
            for low, high in (("min", "max"), ("min_length", "max_length")):
                lo, hi = datatype.facets.get(low), datatype.facets.get(high)
                if isinstance(lo, int) and isinstance(hi, int) and lo > hi:
                    self.add(
                        "model.structural.invalid_value",
                        module_id,
                        f"{datatype.path}.{low}",
                        f"{low} {lo} is greater than {high} {hi}",
                    )

    def pattern(self, module_id: str, path: str, pattern: str) -> None:
        problem = None
        if not (pattern.startswith("^") and pattern.endswith("$")):
            problem = "patterns must be anchored with ^ and $"
        else:
            try:
                re.compile(pattern)
            except re.error as exc:
                problem = f"pattern does not compile: {exc}"
        if problem:
            self.add("model.structural.invalid_pattern", module_id, path, problem, actual=pattern)

    # ---- classes and attributes -----------------------------------------------------------

    def classes(self, module_id: str) -> None:
        module = self.b.modules[module_id]
        for name, cls in module.classes.items():
            curie = self.b.curie(module_id, name)
            if cls.is_a is not None and self.b.parent(module_id, cls) is None:
                self.unknown(
                    "model.referential.unknown_parent",
                    module_id,
                    f"{cls.path}.is_a",
                    "class",
                    cls.is_a,
                )
            inherited = {
                attr_name: owner
                for owner in self.b.ancestors(curie)
                for attr_name in self._declared(owner)
            }
            for attribute in cls.attributes.values():
                self.attribute(module_id, attribute)
                if attribute.name in inherited:
                    self.add(
                        "model.semantic.attribute_redefined",
                        module_id,
                        attribute.path,
                        f"{attribute.name!r} is inherited from {inherited[attribute.name]}",
                    )
            if cls.abstract and not any(
                self.b.parent(m, c) == curie for m, _, c in self.b.classes()
            ):
                self.add(
                    "model.semantic.abstract_never_subclassed",
                    module_id,
                    cls.path,
                    f"abstract class {name!r} has no subclass, so it can never have entities",
                )

    def _declared(self, curie: str) -> Iterable[str]:
        found = self.b.term(curie, "class")
        if found is None:
            return ()
        cls = found[1]
        assert isinstance(cls, IrClass)
        return cls.attributes

    def attribute(self, module_id: str, attribute: IrAttribute) -> None:
        path = attribute.path
        if attribute.name in SYSTEM_FIELDS:
            self.add(
                "model.structural.reserved_name",
                module_id,
                path,
                f"{attribute.name!r} is a reserved system field",
            )
        base: str | None = None
        if attribute.lifecycle is not None:
            if self.b.resolve(module_id, attribute.lifecycle, "lifecycle") is None:
                self.unknown(
                    "model.referential.unknown_lifecycle",
                    module_id,
                    f"{path}.lifecycle",
                    "lifecycle",
                    attribute.lifecycle,
                )
            if attribute.mutability != "lifecycle":
                self.add(
                    "model.mutability.lifecycle_attribute_not_lifecycle",
                    module_id,
                    f"{path}.mutability",
                    "a lifecycle attribute must have mutability 'lifecycle'",
                    expected="lifecycle",
                    actual=attribute.mutability,
                )
            if attribute.many:
                self.add(
                    "model.structural.invalid_value",
                    module_id,
                    f"{path}.many",
                    "a lifecycle attribute holds one state",
                )
        elif attribute.type is not None:
            resolved = self.b.resolve_type(module_id, attribute.type)
            if resolved is None:
                self.unknown(
                    "model.structural.unknown_datatype",
                    module_id,
                    f"{path}.type",
                    "type",
                    attribute.type,
                )
            else:
                base = self._base(*resolved)
            if attribute.mutability == "lifecycle":
                self.add(
                    "model.mutability.lifecycle_attribute_not_lifecycle",
                    module_id,
                    f"{path}.mutability",
                    "only attributes with a lifecycle may have mutability 'lifecycle'",
                    actual=attribute.mutability,
                )
        if attribute.unique and (attribute.many or base in NON_SCALAR_BASES):
            self.add(
                "model.semantic.unique_on_non_scalar",
                module_id,
                f"{path}.unique",
                "unique applies to single scalar values only",
            )
        _, problem = attribute_default(self.b, module_id, attribute)
        if problem:
            self.add("model.structural.invalid_default", module_id, f"{path}.default", problem)

    def _base(self, kind: str, type_ref: str) -> str:
        if kind == "datatype":
            found = self.b.term(type_ref, "datatype")
            if found is not None:
                datatype = found[1]
                assert isinstance(datatype, IrDatatype)
                return datatype.base
        return type_ref if kind == "base" else "string"

    def inheritance(self) -> None:
        reported: set[str] = set()
        for module_id, curie, cls in self.b.classes():
            chain = [curie]
            current: tuple[str, IrClass] = (module_id, cls)
            while True:
                parent = self.b.parent(*current)
                if parent is None:
                    break
                if parent in chain:
                    cycle = chain[chain.index(parent) :]
                    if not reported & set(cycle):
                        first = min(cycle)
                        found = self.b.term(first, "class")
                        assert found is not None
                        owner, owner_cls = found
                        assert isinstance(owner_cls, IrClass)
                        self.add(
                            "model.referential.inheritance_cycle",
                            owner,
                            f"{owner_cls.path}.is_a",
                            f"inheritance cycle: {' -> '.join([*cycle, parent])}",
                        )
                    reported.update(cycle)
                    break
                found = self.b.term(parent, "class")
                if found is None:
                    break
                chain.append(parent)
                parent_module, parent_cls = found
                assert isinstance(parent_cls, IrClass)
                current = (parent_module, parent_cls)

    # ---- relations -----------------------------------------------------------------------

    def relations(self, module_id: str) -> None:
        for relation in self.b.modules[module_id].relations.values():
            for key, refs in (("domain", relation.domain), ("range", relation.range)):
                for index, ref in enumerate(refs):
                    if self.b.resolve(module_id, ref, "class") is None:
                        self.unknown(
                            f"model.referential.unknown_{key}",
                            module_id,
                            f"{relation.path}.{key}" + (f"[{index}]" if len(refs) > 1 else ""),
                            "class",
                            ref,
                        )
            for direction, bounds in (("out", relation.out), ("in", relation.into)):
                if bounds.max is not None and bounds.min > bounds.max:
                    self.add(
                        "model.structural.cardinality_min_gt_max",
                        module_id,
                        f"{relation.path}.cardinality.{direction}",
                        f"min {bounds.min} is greater than max {bounds.max}",
                    )
            for qualifier in relation.qualifiers.values():
                if qualifier.name in EDGE_FIELDS:
                    self.add(
                        "model.structural.reserved_name",
                        module_id,
                        qualifier.path,
                        f"{qualifier.name!r} is a reserved edge field",
                    )
                if self.b.resolve_type(module_id, qualifier.type) is None:
                    self.unknown(
                        "model.semantic.qualifier_unknown_datatype",
                        module_id,
                        f"{qualifier.path}.type",
                        "type",
                        qualifier.type,
                    )

    def inverse_labels(self) -> None:
        names: dict[str, str] = {}
        for module_id in self.b.order:
            for name in self.b.modules[module_id].relations:
                names.setdefault(name, self.b.curie(module_id, name))
        labels: dict[str, str] = {}
        for module_id in self.b.order:
            for name, relation in self.b.modules[module_id].relations.items():
                label = relation.inverse_label
                if label is None:
                    continue
                curie = self.b.curie(module_id, name)
                clash = names.get(label) or labels.get(label)
                if clash is not None and clash != curie:
                    self.add(
                        "model.referential.inverse_label_collision",
                        module_id,
                        f"{relation.path}.inverse_label",
                        f"inverse label {label!r} is already used by {clash}",
                        actual=label,
                    )
                labels.setdefault(label, curie)

    # ---- lifecycles ----------------------------------------------------------------------

    def lifecycles(self, module_id: str) -> None:
        for name, lifecycle in self.b.modules[module_id].lifecycles.items():
            self.lifecycle(module_id, self.b.curie(module_id, name), lifecycle)

    def lifecycle(self, module_id: str, curie: str, lifecycle: IrLifecycle) -> None:
        states = lifecycle.states
        initial = [s for s in states.values() if s.initial]
        if not initial:
            self.add(
                "model.lifecycle.no_initial_state",
                module_id,
                f"{lifecycle.path}.states",
                "no state is marked initial",
            )
        edges: dict[str, set[str]] = {s: set() for s in states}
        for transition in lifecycle.transitions:
            for key, refs in (("from", transition.from_states), ("to", (transition.to,))):
                for ref in refs:
                    if ref not in states:
                        self.add(
                            "model.lifecycle.undefined_state",
                            module_id,
                            f"{transition.path}.{key}",
                            f"state {ref!r} is not defined",
                            actual=ref,
                        )
            for source in transition.from_states:
                if source in states and states[source].terminal:
                    self.add(
                        "model.lifecycle.terminal_has_outgoing",
                        module_id,
                        f"{transition.path}.from",
                        f"terminal state {source!r} has an outgoing transition",
                    )
                if source in states and transition.to in states:
                    edges[source].add(transition.to)
            users = self._lifecycle_users(curie)
            for guard in transition.guards:
                self.conditions(module_id, guard.require, users, guard=True)
        if initial:
            reached = {s.name for s in initial}
            queue = deque(reached)
            while queue:
                for target in edges[queue.popleft()]:
                    if target not in reached:
                        reached.add(target)
                        queue.append(target)
            for state in states:
                if state not in reached:
                    self.add(
                        "model.lifecycle.unreachable_state",
                        module_id,
                        f"{lifecycle.path}.states.{state}",
                        f"state {state!r} cannot be reached from an initial state",
                    )

    def _lifecycle_users(self, lifecycle_curie: str) -> list[str]:
        """Classes (including subclasses) with an attribute bound to this lifecycle."""
        users = []
        for _, curie, _ in self.b.classes():
            for _, module_id, attribute in self.b.effective_attributes(curie).values():
                if (
                    attribute.lifecycle is not None
                    and self.b.resolve(module_id, attribute.lifecycle, "lifecycle")
                    == lifecycle_curie
                ):
                    users.append(curie)
                    break
        return users

    # ---- conditions, rules, mappings -----------------------------------------------------

    def conditions(
        self, module_id: str, conds: tuple[Cond, ...], scopes: list[str], *, guard: bool
    ) -> None:
        code = (
            "model.lifecycle.guard_unknown_attribute" if guard else "model.rule.unknown_attribute"
        )
        for cond in iter_conditions(conds):
            if isinstance(cond, AttrCond):
                for scope in scopes:
                    if cond.attr not in self.b.effective_attributes(scope):
                        self.add(
                            code,
                            module_id,
                            f"{cond.path}.attr",
                            f"{scope} has no attribute {cond.attr!r}",
                            actual=cond.attr,
                        )
            elif isinstance(cond, CountCond):
                if self.b.resolve(module_id, cond.rel, "relation") is None:
                    self.unknown(
                        "model.referential.unknown_relation",
                        module_id,
                        f"{cond.path}.count.rel",
                        "relation",
                        cond.rel,
                    )
                for ref in cond.types:
                    if self.b.resolve(module_id, ref, "class") is None:
                        self.unknown(
                            "model.referential.unknown_term",
                            module_id,
                            f"{cond.path}.count.where.type",
                            "class",
                            ref,
                        )

    def rules(self, module_id: str) -> None:
        for rule in self.b.modules[module_id].rules:
            scope = self.b.resolve(module_id, rule.scope, "class")
            if scope is None:
                self.unknown(
                    "model.rule.unknown_scope",
                    module_id,
                    f"{rule.path}.scope",
                    "class",
                    rule.scope,
                )
            scopes = [] if scope is None else [scope]
            self.conditions(module_id, (*(rule.if_ or ()), *rule.then), scopes, guard=False)

    def mappings(self, module_id: str) -> None:
        module = self.b.modules[module_id]
        for term in module.mappings:
            if term not in module.classes and term not in module.relations:
                self.add(
                    "model.referential.unknown_term",
                    module_id,
                    f"mappings.{term}",
                    f"{term!r} is not a class or relation of this module",
                    actual=term,
                )
