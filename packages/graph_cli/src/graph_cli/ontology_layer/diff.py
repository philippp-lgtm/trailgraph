"""Version diff and semver classifier (contracts B7.12).

``classify(old, new)`` compares two compiled models and returns one entry per
change with its level; the report level is the maximum. Renames are
recognized only when declared with ``renamed_from``; anything else shows up
as remove plus add. Inherited attributes are compared once, at the class
that declares them.

``check_bump`` judges the bundle version (the main module's) against the report level;
``module_bumps`` judges the own version of every other authored module against the level
of the changes to its terms, since importers pin those versions with ranges.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from graph_cli.kernel.canonical import JsonValue
from graph_cli.ontology_layer.model import (
    AttributeDef,
    Bound,
    ClassDef,
    CompiledModel,
    DatatypeDef,
    EnumDef,
    LifecycleDef,
    QualifierDef,
    RelationDef,
    RuleDef,
    TermDoc,
    TransitionDef,
)
from graph_cli.ontology_layer.semver import Level, Version, bump_level, max_level
from graph_cli.ontology_layer.vocabulary import BUILTIN_MODULES, DIFF_SCHEMA, MUTABILITY_RANK

ChangeLevel = Literal["patch", "minor", "major"]
_EMPTY: Mapping[str, JsonValue] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class Change:
    path: str
    kind: str
    level: ChangeLevel
    detail: Mapping[str, JsonValue] = field(default=_EMPTY)

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "path": self.path,
            "kind": self.kind,
            "level": self.level,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True, slots=True)
class DiffReport:
    from_version: str
    to_version: str
    level: Level
    changes: tuple[Change, ...]

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema": DIFF_SCHEMA,
            "from": self.from_version,
            "to": self.to_version,
            "level": self.level,
            "changes": [c.to_json() for c in self.changes],
        }


BumpCheck = Literal["ok", "insufficient", "excessive"]


def check_bump(report: DiffReport) -> BumpCheck:
    """Compare the declared version bump with the computed level (B7.12).

    ``insufficient`` maps to ``admin.version_bump_insufficient``; ``excessive``
    is a warning. A downgrade or an unchanged version with changes is
    insufficient.
    """
    return _judge(report.from_version, report.to_version, report.level)


def _judge(from_version: str, to_version: str, level: Level) -> BumpCheck:
    declared = bump_level(Version.parse(from_version), Version.parse(to_version))
    if declared == "downgrade":
        return "insufficient"
    order = ("none", "patch", "minor", "major")
    if order.index(declared) < order.index(level):
        return "insufficient"
    return "excessive" if order.index(declared) > order.index(level) else "ok"


@dataclass(frozen=True, slots=True)
class ModuleBump:
    """The own version step of one module and the level of the changes to its terms."""

    module: str
    from_version: str
    to_version: str
    level: Level
    check: BumpCheck

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "module": self.module,
            "from": self.from_version,
            "to": self.to_version,
            "level": self.level,
        }


def module_bumps(old: CompiledModel, new: CompiledModel, report: DiffReport) -> list[ModuleBump]:
    """One item per module in both models, except the main and the built-in modules (sorted
    by id). A change belongs to the module whose prefix its term carries (rule ids start with
    it too); ``modules.<id>`` changes to that module."""
    old_modules = {m.id: m for m in old.modules}
    new_modules = {m.id: m for m in new.modules}
    by_prefix = {m.prefix: m.id for m in (*old.modules, *new.modules)}
    levels: dict[str, list[Level]] = {}
    for change in report.changes:
        section, _, rest = change.path.partition(".")
        key = rest.split(".")[0] if section in ("modules", "rules") else rest.partition(":")[0]
        module_id = key if section == "modules" else by_prefix.get(key)
        if module_id is not None:
            levels.setdefault(module_id, []).append(change.level)
    main = {old.bundle_id, new.bundle_id}
    return [
        ModuleBump(
            module_id,
            old_modules[module_id].version,
            new_modules[module_id].version,
            level := max_level(levels.get(module_id, [])),
            _judge(old_modules[module_id].version, new_modules[module_id].version, level),
        )
        for module_id in sorted(old_modules.keys() & new_modules.keys())
        if module_id not in main and module_id not in BUILTIN_MODULES
    ]


def classify(old: CompiledModel, new: CompiledModel) -> DiffReport:
    differ = _Differ()
    differ.modules(old, new)
    differ.section("datatypes", old.datatypes, new.datatypes, differ.datatype)
    differ.section("enums", old.enums, new.enums, differ.enum)
    differ.section("lifecycles", old.lifecycles, new.lifecycles, differ.lifecycle)
    differ.section("classes", old.classes, new.classes, differ.class_, renames=_class_renames)
    differ.section(
        "relations", old.relations, new.relations, differ.relation, renames=_relation_renames
    )
    differ.rules(old.rules, new.rules)
    changes = tuple(sorted(differ.changes, key=lambda c: (c.path, c.kind)))
    return DiffReport(
        from_version=old.bundle_version,
        to_version=new.bundle_version,
        level=max_level([c.level for c in changes]),
        changes=changes,
    )


def _rename_target(curie: str, local: str | None) -> str | None:
    if local is None:
        return None
    return f"{curie.partition(':')[0]}:{local}"


def _class_renames(new: Mapping[str, ClassDef]) -> dict[str, str]:
    return {
        old_curie: curie
        for curie, cls in new.items()
        if (old_curie := _rename_target(curie, cls.renamed_from)) is not None
    }


def _relation_renames(new: Mapping[str, RelationDef]) -> dict[str, str]:
    return {
        old_curie: curie
        for curie, rel in new.items()
        if (old_curie := _rename_target(curie, rel.renamed_from)) is not None
    }


_SINGULAR = {
    "datatypes": "datatype",
    "enums": "enum",
    "lifecycles": "lifecycle",
    "classes": "class",
    "relations": "relation",
}


class _Differ:
    def __init__(self) -> None:
        self.changes: list[Change] = []

    def add(self, path: str, kind: str, level: ChangeLevel, **detail: JsonValue) -> None:
        self.changes.append(Change(path, kind, level, detail))

    # ---- generic ---------------------------------------------------------------------------

    def section[T](
        self,
        name: str,
        old: Mapping[str, T],
        new: Mapping[str, T],
        compare: Callable[[str, str, T, T], None],
        *,
        renames: Callable[[Mapping[str, T]], dict[str, str]] | None = None,
    ) -> None:
        renamed = {
            o: n for o, n in (renames(new) if renames else {}).items() if o in old and o not in new
        }
        renamed_targets = set(renamed.values())
        singular = _SINGULAR[name]
        for curie in sorted(old):
            if curie in renamed:
                target = renamed[curie]
                self.add(f"{name}.{curie}", "rename", "major", to=target)
                compare(f"{name}.{curie}", target, old[curie], new[target])
            elif curie not in new:
                self.add(f"{name}.{curie}", f"remove_{singular}", "major")
            else:
                compare(f"{name}.{curie}", curie, old[curie], new[curie])
        for curie in sorted(new):
            if curie not in old and curie not in renamed_targets:
                self.add(f"{name}.{curie}", f"add_{singular}", "minor")

    def doc(self, path: str, old: TermDoc, new: TermDoc) -> None:
        if old.summary != new.summary:
            self.add(path, "change_summary", "patch")
        if old.description != new.description:
            self.add(path, "change_description", "patch")
        if old.annotations != new.annotations:
            self.add(path, "change_annotation", "patch")
        if old.deprecated != new.deprecated:
            self.add(
                path,
                "deprecate" if new.deprecated else "undeprecate",
                "minor" if new.deprecated else "patch",
            )

    def flag(
        self, path: str, kind: str, old: bool, new: bool, *, on: ChangeLevel, off: ChangeLevel
    ) -> None:
        if old != new:
            self.add(path, f"{kind}_{'on' if new else 'off'}", on if new else off)

    # ---- modules -----------------------------------------------------------------------------

    def modules(self, old: CompiledModel, new: CompiledModel) -> None:
        old_modules = {m.id: m for m in old.modules}
        new_modules = {m.id: m for m in new.modules}
        for module_id in sorted(old_modules.keys() - new_modules.keys()):
            self.add(f"modules.{module_id}", "remove_import", "major")
        for module_id in sorted(new_modules.keys() - old_modules.keys()):
            self.add(f"modules.{module_id}", "add_import", "minor")
        for module_id in sorted(old_modules.keys() & new_modules.keys()):
            a, b, path = old_modules[module_id], new_modules[module_id], f"modules.{module_id}"
            if a.prefix != b.prefix:
                self.add(path, "change_prefix", "major", **{"from": a.prefix, "to": b.prefix})
            if a.namespace != b.namespace:
                self.add(path, "change_namespace", "major")
            if a.summary != b.summary:
                self.add(path, "change_summary", "patch")
            if a.extensions != b.extensions:
                self.add(path, "change_annotation", "patch")

    # ---- datatypes and enums -------------------------------------------------------------

    def datatype(self, path: str, _curie: str, old: DatatypeDef, new: DatatypeDef) -> None:
        self.doc(path, old.doc, new.doc)
        if old.base != new.base:
            self.add(path, "change_base", "major", **{"from": old.base, "to": new.base})
            return
        for facet in sorted(old.facets.keys() | new.facets.keys()):
            a, b = old.facets.get(facet), new.facets.get(facet)
            if a == b:
                continue
            facet_path = f"{path}.facets.{facet}"
            if a is None or (facet == "single_line" and b is True):
                self.add(facet_path, "tighten_facet", "major", to=b)
            elif b is None or (facet == "single_line" and b is False):
                self.add(facet_path, "loosen_facet", "minor", **{"from": a})
            elif facet == "pattern":
                self.add(facet_path, "change_facet", "major", **{"from": a, "to": b})
            elif isinstance(a, int) and isinstance(b, int):
                tighter = b > a if facet in ("min", "min_length") else b < a
                self.add(
                    facet_path,
                    "tighten_facet" if tighter else "loosen_facet",
                    "major" if tighter else "minor",
                    **{"from": a, "to": b},
                )

    def enum(self, path: str, _curie: str, old: EnumDef, new: EnumDef) -> None:
        self.doc(path, old.doc, new.doc)
        old_values = {v.name: v for v in old.values}
        new_values = {v.name: v for v in new.values}
        for value in sorted(old_values.keys() - new_values.keys()):
            self.add(f"{path}.values.{value}", "remove_enum_value", "major")
        for value in sorted(new_values.keys() - old_values.keys()):
            self.add(f"{path}.values.{value}", "add_enum_value", "minor")
        for value in sorted(old_values.keys() & new_values.keys()):
            if old_values[value].summary != new_values[value].summary:
                self.add(f"{path}.values.{value}", "change_summary", "patch")
        kept_old = [v for v in old_values if v in new_values]
        kept_new = [v for v in new_values if v in old_values]
        if kept_old != kept_new:
            self.add(f"{path}.values", "reorder_values", "patch")

    # ---- lifecycles ------------------------------------------------------------------------

    def lifecycle(self, path: str, _curie: str, old: LifecycleDef, new: LifecycleDef) -> None:
        self.doc(path, old.doc, new.doc)
        old_states = {s.name: s for s in old.states}
        new_states = {s.name: s for s in new.states}
        for state in sorted(old_states.keys() - new_states.keys()):
            self.add(f"{path}.states.{state}", "remove_state", "major")
        for state in sorted(new_states.keys() - old_states.keys()):
            initial = state in new.initial
            self.add(f"{path}.states.{state}", "add_state", "major" if initial else "minor")
        for state in sorted(old_states.keys() & new_states.keys()):
            if old_states[state].summary != new_states[state].summary:
                self.add(f"{path}.states.{state}", "change_summary", "patch")
        kept = old_states.keys() & new_states.keys()
        if {s for s in old.initial if s in kept} != {s for s in new.initial if s in kept}:
            self.add(f"{path}.initial", "change_initial", "major")
        if set(old.terminal) != set(new.terminal):
            self.add(f"{path}.terminal", "change_terminal", "major")
        old_t = {(t.from_state, t.to_state): t for t in old.transitions}
        new_t = {(t.from_state, t.to_state): t for t in new.transitions}
        for key in sorted(old_t.keys() - new_t.keys()):
            self.add(f"{path}.transitions.{key[0]}->{key[1]}", "remove_transition", "major")
        for key in sorted(new_t.keys() - old_t.keys()):
            self.add(f"{path}.transitions.{key[0]}->{key[1]}", "add_transition", "minor")
        for key in sorted(old_t.keys() & new_t.keys()):
            self.transition(f"{path}.transitions.{key[0]}->{key[1]}", old_t[key], new_t[key])

    def transition(self, path: str, old: TransitionDef, new: TransitionDef) -> None:
        if old.summary != new.summary:
            self.add(path, "change_summary", "patch")
        old_g = {g.id: g for g in old.guards}
        new_g = {g.id: g for g in new.guards}
        for guard in sorted(old_g.keys() - new_g.keys()):
            self.add(f"{path}.guards.{guard}", "remove_guard", "minor")
        for guard in sorted(new_g.keys() - old_g.keys()):
            self.add(f"{path}.guards.{guard}", "add_guard", "major")
        for guard in sorted(old_g.keys() & new_g.keys()):
            if old_g[guard].expr != new_g[guard].expr:
                self.add(f"{path}.guards.{guard}", "change_guard", "major")
            if old_g[guard].summary != new_g[guard].summary:
                self.add(f"{path}.guards.{guard}", "change_summary", "patch")

    # ---- classes and attributes ------------------------------------------------------------

    def class_(self, path: str, new_curie: str, old: ClassDef, new: ClassDef) -> None:
        self.doc(path, old.doc, new.doc)
        old_curie = path.partition(".")[2]
        if old.extends != new.extends:
            self.add(path, "change_extends", "major", **{"from": old.extends, "to": new.extends})
        self.flag(path, "abstract", old.abstract, new.abstract, on="major", off="minor")
        if old.display_prefix != new.display_prefix:
            self.add(path, "change_display_prefix", "patch")
        if old.mappings != new.mappings:
            self.add(path, "change_mapping", "patch")
        old_attrs = {n: a for n, a in old.attributes.items() if a.declared_in == old_curie}
        new_attrs = {n: a for n, a in new.attributes.items() if a.declared_in == new_curie}
        renamed = {
            a.renamed_from: name
            for name, a in new_attrs.items()
            if a.renamed_from in old_attrs and a.renamed_from not in new_attrs
        }
        for name in sorted(old_attrs):
            attr_path = f"{path}.attributes.{name}"
            if name in renamed:
                self.add(attr_path, "rename", "major", to=renamed[name])
                self.attribute(attr_path, old_attrs[name], new_attrs[renamed[name]])
            elif name not in new_attrs:
                self.add(attr_path, "remove_attribute", "major")
            else:
                self.attribute(attr_path, old_attrs[name], new_attrs[name])
        for name in sorted(new_attrs.keys() - old_attrs.keys() - set(renamed.values())):
            required = new_attrs[name].required
            self.add(
                f"{path}.attributes.{name}",
                "add_required_attribute" if required else "add_attribute",
                "major" if required else "minor",
            )

    def attribute(self, path: str, old: AttributeDef, new: AttributeDef) -> None:
        self.doc(path, old.doc, new.doc)
        if (old.type, old.kind) != (new.type, new.kind):
            self.add(path, "change_type", "major", **{"from": old.type, "to": new.type})
        self.flag(path, "required", old.required, new.required, on="major", off="minor")
        self.flag(path, "many", old.many, new.many, on="major", off="major")
        self.flag(path, "unique", old.unique, new.unique, on="major", off="minor")
        if old.default != new.default:
            self.add(path, "change_default", "minor", **{"from": old.default, "to": new.default})
        if old.mutability != new.mutability:
            a, b = MUTABILITY_RANK.get(old.mutability), MUTABILITY_RANK.get(new.mutability)
            looser = a is not None and b is not None and b < a
            self.add(
                path,
                "loosen_mutability" if looser else "tighten_mutability",
                "minor" if looser else "major",
                **{"from": old.mutability, "to": new.mutability},
            )
        if old.role != new.role:
            # Adding a role only lets lookups find more; removing or switching it
            # changes which names readers and generated skills rely on.
            kind = "add" if old.role is None else "remove" if new.role is None else "change"
            self.add(
                path,
                f"{kind}_role",
                "minor" if kind == "add" else "major",
                **{"from": old.role, "to": new.role},
            )

    # ---- relations ---------------------------------------------------------------------------

    def relation(self, path: str, _curie: str, old: RelationDef, new: RelationDef) -> None:
        self.doc(path, old.doc, new.doc)
        for key, a, b in (("domain", old.domain, new.domain), ("range", old.range, new.range)):
            if set(a) == set(b):
                continue
            wider = not b or (bool(a) and set(a) <= set(b))
            narrower = not a or (bool(b) and set(b) <= set(a))
            kind = "widen" if wider and not narrower else "narrow" if narrower else "change"
            self.add(
                f"{path}.{key}",
                f"{kind}_{key}",
                "minor" if kind == "widen" else "major",
                **{"from": list(a), "to": list(b)},
            )
        self.bound(f"{path}.cardinality.out", old.out, new.out)
        self.bound(f"{path}.cardinality.in", old.into, new.into)
        for item in sorted(set(old.characteristics) - set(new.characteristics)):
            self.add(f"{path}.characteristics.{item}", "remove_characteristic", "minor")
        for item in sorted(set(new.characteristics) - set(old.characteristics)):
            self.add(f"{path}.characteristics.{item}", "add_characteristic", "major")
        if old.on_target_inactive != new.on_target_inactive:
            # Only refuse is an error check; reaching it is major, every other move only
            # changes warnings or loosens.
            self.add(
                f"{path}.on_target_inactive",
                "change_on_target_inactive",
                "major" if new.on_target_inactive == "refuse" else "minor",
                **{"from": old.on_target_inactive, "to": new.on_target_inactive},
            )
        if old.inverse_label != new.inverse_label:
            self.add(
                path,
                "change_inverse_label",
                "major",
                **{"from": old.inverse_label, "to": new.inverse_label},
            )
        if old.mappings != new.mappings:
            self.add(path, "change_mapping", "patch")
        for name in sorted(old.qualifiers.keys() - new.qualifiers.keys()):
            self.add(f"{path}.qualifiers.{name}", "remove_qualifier", "major")
        for name in sorted(new.qualifiers.keys() - old.qualifiers.keys()):
            required = new.qualifiers[name].required
            self.add(
                f"{path}.qualifiers.{name}",
                "add_required_qualifier" if required else "add_qualifier",
                "major" if required else "minor",
            )
        for name in sorted(old.qualifiers.keys() & new.qualifiers.keys()):
            self.qualifier(f"{path}.qualifiers.{name}", old.qualifiers[name], new.qualifiers[name])

    def qualifier(self, path: str, old: QualifierDef, new: QualifierDef) -> None:
        if (old.type, old.kind) != (new.type, new.kind):
            self.add(path, "change_type", "major", **{"from": old.type, "to": new.type})
        self.flag(path, "required", old.required, new.required, on="major", off="minor")
        self.flag(path, "many", old.many, new.many, on="major", off="major")
        if old.summary != new.summary:
            self.add(path, "change_summary", "patch")

    def bound(self, path: str, old: Bound, new: Bound) -> None:
        if old.min != new.min:
            tighter = new.min > old.min
            self.add(
                f"{path}.min",
                "tighten_cardinality" if tighter else "loosen_cardinality",
                "major" if tighter else "minor",
                **{"from": old.min, "to": new.min},
            )
        if old.max != new.max:
            tighter = old.max is None or (new.max is not None and new.max < old.max)
            self.add(
                f"{path}.max",
                "tighten_cardinality" if tighter else "loosen_cardinality",
                "major" if tighter else "minor",
                **{"from": old.max, "to": new.max},
            )

    # ---- rules -------------------------------------------------------------------------------

    def rules(self, old: tuple[RuleDef, ...], new: tuple[RuleDef, ...]) -> None:
        old_rules = {r.id: r for r in old}
        new_rules = {r.id: r for r in new}
        for rule_id in sorted(old_rules.keys() - new_rules.keys()):
            error = old_rules[rule_id].severity == "error"
            self.add(f"rules.{rule_id}", "remove_rule", "major" if error else "minor")
        for rule_id in sorted(new_rules.keys() - old_rules.keys()):
            error = new_rules[rule_id].severity == "error"
            self.add(
                f"rules.{rule_id}",
                "add_error_rule" if error else "add_warning_rule",
                "major" if error else "minor",
            )
        for rule_id in sorted(old_rules.keys() & new_rules.keys()):
            a, b, path = old_rules[rule_id], new_rules[rule_id], f"rules.{rule_id}"
            self.doc(path, a.doc, b.doc)
            if a.severity != b.severity:
                stricter = b.severity == "error"
                self.add(
                    path,
                    "change_severity",
                    "major" if stricter else "minor",
                    **{"from": a.severity, "to": b.severity},
                )
            # A rule that ends up a warning blocks nothing and invalidates no data,
            # so changing what it checks is minor; an error rule's change is major
            # because looser and stricter cannot be told apart in general.
            level: ChangeLevel = "major" if b.severity == "error" else "minor"
            if a.when != b.when:
                self.add(path, "change_timing", level, **{"from": a.when, "to": b.when})
            if a.target != b.target:
                self.add(path, "change_target", level, **{"from": a.target, "to": b.target})
            if a.expr != b.expr:
                self.add(path, "change_expression", level)
