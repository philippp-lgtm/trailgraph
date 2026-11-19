"""Checking and compiling views against a compiled ontology and the function catalog.

``compile_views`` reads the view files of a graph, checks each against the model and the
functions the graph will have, and returns the catalog document ``graph_cli.view_catalog/1``
together with every problem found; the document holds only views without problems, and a
caller publishes only when there are none. Nothing needs data or parameter values. The
checks, in the order of the plan (V9):

1. the file (from the parser), ``module``, ``replaced_by``, parameters (classes and enums
   exist, defaults bind), one ``default`` view per class;
2. terms: ``takes`` and ``type`` are classes, every named attribute belongs to a class of the
   section, every relation exists and starts (``direction`` out) or ends (``in``) at a class
   of the section, ``where`` and ``order`` plan with the ``find`` planner against the
   classes of the section, and a ``$name`` fits the attribute it is compared with;
3. functions: a ``rows`` function exists in the catalog, every required parameter is bound,
   each argument fits (``$entity`` binds a ``ref`` parameter whose class is the view's class or
   a superclass, ``$name`` a declared view parameter of a compatible type, a literal binds like
   a ``graph run`` value and is never a ``ref``).

The compiled entry stores the normalized definition (CURIEs), the authored source (enough to
compile again), ``touches`` (types, relations, attributes, functions: what hiding and
``describe`` read) and the hash. ``check_views`` is the release invariant: it compiles the
stored sources against another model (and another function catalog), and the stored entry
must read the same (``view.stale``). A deprecated function behind a ``rows`` section is not a
problem; ``deprecated_function_uses`` lists those as warnings.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping, Sequence
from dataclasses import replace
from decimal import Decimal
from typing import Final

from graph_cli.kernel.canonical import JsonObject, JsonValue, hash_document
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.json_access import JsonShapeError, as_object, req_list, req_obj, req_str
from graph_cli.kinetic.compile import FITS, resolve_module
from graph_cli.kinetic.entries import FunctionEntry
from graph_cli.kinetic.params import (
    ParamDef,
    Problem,
    bind_json,
    placeholder,
)
from graph_cli.kinetic.view_format import (
    CATALOG_SCHEMA,
    ENTITY,
    AffordancesSection,
    FieldSel,
    FieldsSection,
    FilesSection,
    ListSection,
    NeighborsSection,
    RelationRef,
    RelationsSection,
    RowRelation,
    RowsSection,
    SectionDef,
    ViewDef,
    load_view,
    placeholder_name,
    where_parts,
)
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.ontology_layer.vocabulary import LANG_BASES, SYSTEM_FIELDS
from graph_cli.query.cypher.resolve import SYSTEM_BASES
from graph_cli.query.cypher.values import Value
from graph_cli.query.plan import Clause, Operand, plan_clauses, resolve_term

MAX_VIEWS: Final = 1000

_TEXT_OPS: Final = frozenset({"contains", "prefix", "lang"})


def compile_views(
    sources: Mapping[str, str | bytes],
    model: CompiledModel,
    functions: Sequence[FunctionEntry] = (),
) -> tuple[dict[str, JsonValue], list[Problem]]:
    """Compile the view files (file name -> content) of one graph against ``model``;
    ``functions`` are the published functions the graph will have."""
    problems: list[Problem] = []
    defs: list[ViewDef] = []
    for file_name in sorted(sources):
        defn, found = load_view(file_name, sources[file_name])
        problems.extend(found)
        if defn is not None:
            defs.append(defn)
    if len(sources) > MAX_VIEWS:
        problems.append(_too_many(len(sources)))
    document, found = _compile(defs, model, functions)
    return document, [*problems, *found]


def check_views(
    document: JsonObject, model: CompiledModel, functions: Sequence[FunctionEntry] = ()
) -> list[Problem]:
    """The problems a published catalog has against ``model`` and ``functions`` (none: it
    still holds).

    Every view is compiled again from its stored source. A view that compiles is stale
    (``view.stale``) when its stored normalized form no longer compiles or reads differently
    than the source does now (its class, parameters or sections); the hash is the identity of
    the entry, not the invariant: a release that only changes a module id breaks nothing. A document
    of another shape raises ``JsonShapeError``, an unknown schema
    ``format.unsupported_version``.
    """
    schema = req_str(document, "schema")
    if schema != CATALOG_SCHEMA:
        raise GraphCliError(
            "format.unsupported_version",
            f"unsupported view catalog format {schema!r}; expected {CATALOG_SCHEMA}",
            details={"expected": CATALOG_SCHEMA, "actual": schema},
        )
    entries = [as_object(e, "views") for e in req_list(document, "views")]
    problems: list[Problem] = []
    defs: list[ViewDef] = []
    stored: dict[str, tuple[JsonObject, ViewDef]] = {}
    for entry in entries:
        name = req_str(entry, "name")
        try:
            source = ViewDef.from_json(req_obj(entry, "source", "views."))
            normalized = ViewDef.from_json(entry)
        except JsonShapeError as exc:
            problems.append(_stale(name, f"the stored view no longer reads: {exc}"))
            continue
        if source.name != name or normalized.name != name:
            problems.append(_stale(name, "the stored view and its source have different names"))
            continue
        defs.append(source)
        stored[name] = (entry, normalized)
    fresh, found = _compile(defs, model, functions)
    problems.extend(found)
    active = frozenset(d.name for d in defs if d.status == "active")
    by_name = {f.name: f for f in functions}
    for value in req_list(fresh, "views"):
        compiled = as_object(value, "views")
        name = req_str(compiled, "name")
        entry, normalized = stored[name]
        again = _Compile(normalized, model, by_name, active)
        again.run()
        if again.problems:
            reason = f"the stored form no longer compiles: {again.problems[0].message}"
            problems.append(_stale(name, reason))
        elif any(compiled.get(key) != entry.get(key) for key in ("takes", "params", "sections")):
            reason = "the stored form reads differently than its source does against this model"
            problems.append(_stale(name, reason))
    if len(entries) > MAX_VIEWS:
        problems.append(_too_many(len(entries)))
    return problems


def deprecated_function_uses(
    document: JsonObject, functions: Sequence[FunctionEntry]
) -> list[Problem]:
    """``view.function_deprecated`` for each ``rows`` section of a catalog whose function is
    deprecated. Not a refusal: the caller shows these as warnings."""
    by_name = {f.name: f for f in functions}
    found: list[Problem] = []
    for value in req_list(document, "views"):
        entry = as_object(value, "views")
        name = req_str(entry, "name")
        for index, raw in enumerate(req_list(entry, "sections", f"{name}.")):
            section = as_object(raw, "sections")
            function = by_name.get(str(section.get("function")))
            if section.get("kind") != "rows" or function is None or not function.deprecated:
                continue
            found.append(
                Problem(
                    name,
                    "view.function_deprecated",
                    f"function {function.name!r} is deprecated"
                    + (f"; use {function.defn.replaced_by!r}" if function.defn.replaced_by else ""),
                    f"sections[{index}].function",
                    details={"function": function.name, "replaced_by": function.defn.replaced_by},
                    kind="view",
                )
            )
    return found


def _operand(value: Value) -> Operand:
    """A placeholder value as the typed operand of a ``where`` entry."""
    if isinstance(value, list):
        return [_operand(item) for item in value]
    assert isinstance(value, str | int | bool | Decimal)
    return value


def _too_many(count: int) -> Problem:
    return Problem(
        "(catalog)",
        "view.too_many_views",
        f"{count} views; at most {MAX_VIEWS} per graph",
        details={"count": count, "limit": MAX_VIEWS},
        kind="view",
    )


def _stale(name: str, message: str) -> Problem:
    return Problem(name, "view.stale", message, kind="view")


def _compile(
    defs: Sequence[ViewDef], model: CompiledModel, functions: Sequence[FunctionEntry]
) -> tuple[dict[str, JsonValue], list[Problem]]:
    active = frozenset(d.name for d in defs if d.status == "active")
    by_name = {f.name: f for f in functions}
    entries: list[JsonValue] = []
    problems: list[Problem] = []
    defaults: dict[str, str] = {}  # class -> the view that is its default
    for defn in sorted(defs, key=lambda d: d.name):
        check = _Compile(defn, model, by_name, active)
        entry = check.run()
        problems.extend(check.problems)
        if entry is None or check.problems:
            continue
        takes = entry["takes"]
        if entry["default"] is True and isinstance(takes, str):
            other = defaults.setdefault(takes, defn.name)
            if other != defn.name:
                problems.append(
                    Problem(
                        defn.name,
                        "view.default_taken",
                        f"{other!r} is already the default view of {takes}",
                        "default",
                        details={"takes": takes, "default": other},
                        kind="view",
                    )
                )
                continue
        entries.append(entry)
    return {"schema": CATALOG_SCHEMA, "views": entries}, problems


class _Compile:
    """The checks of one view; ``run`` returns the entry, problems collect in ``problems``."""

    def __init__(
        self,
        defn: ViewDef,
        model: CompiledModel,
        functions: Mapping[str, FunctionEntry],
        active: frozenset[str],
    ) -> None:
        self.defn = defn
        self.model = model
        self.functions = functions
        self.active = active
        self.problems: list[Problem] = []
        self.declared = {p.name: p for p in defn.params}
        self.used: set[str] = set()
        self.takes: str | None = None  # the class an entity view takes, as a CURIE
        self.entity_classes: frozenset[str] = frozenset()
        self.types: set[str] = set()
        self.relations: set[str] = set()
        self.attributes: set[str] = set()
        self.function_names: set[str] = set()

    def add(self, code: str, path: str | None, message: str, **details: JsonValue) -> None:
        self.problems.append(Problem(self.defn.name, code, message, path, None, details, "view"))

    def collect_uses(self) -> None:
        """The parameters the sections use, found before any check can skip a section: a
        parameter is unused only when no where entry and no argument names it."""
        for section in self.defn.sections:
            texts: list[str] = []
            if isinstance(section, NeighborsSection | ListSection):
                texts = [*section.where, *(w for r in section.relations for w in r.where)]
            for text in texts:
                name = where_parts(text)[1]
                if name is not None and name != ENTITY:
                    self.used.add(name)
            if isinstance(section, RowsSection):
                for value in section.args.values():
                    name = placeholder_name(value)
                    if name is not None and name != ENTITY:
                        self.used.add(name)

    def run(self) -> dict[str, JsonValue] | None:
        self.collect_uses()
        module = resolve_module(self.model, self.defn.module, "view", self.add)
        self.replacement()
        self.parameters()
        self.entity_class()
        sections = [self.section(i, s) for i, s in enumerate(self.defn.sections)]
        self.examples()
        self.unused()
        if self.problems or None in sections:
            return None
        normalized = replace(
            self.defn,
            module=module,
            takes=self.takes,
            sections=tuple(s for s in sections if s is not None),
        )
        return self.entry(normalized)

    # Declaration

    def replacement(self) -> None:
        target = self.defn.replaced_by
        if target is not None and target not in self.active:
            self.add(
                "view.replaced_by_invalid",
                "replaced_by",
                f"{target!r} is not an active view of the graph",
                replaced_by=target,
            )

    def parameters(self) -> None:
        for param in self.defn.params:
            path = f"params.{param.name}"
            usable = True
            if param.type == "ref" and param.of_class not in self.model.classes:
                self.add(
                    "view.unknown_class",
                    f"{path}.class",
                    f"the model has no class {param.of_class!r}",
                    curie=param.of_class,
                )
                usable = False
            elif param.type == "ref" and param.of_class is not None:
                self.types.add(param.of_class)
            if param.type == "enum" and param.enum not in self.model.enums:
                self.add(
                    "view.unknown_enum",
                    f"{path}.enum",
                    f"the model has no enum {param.enum!r}",
                    curie=param.enum,
                )
                usable = False
            if param.default is None or not usable:
                continue
            if param.type == "ref":
                self.add(
                    "view.invalid_default",
                    f"{path}.default",
                    "a ref has no default; entity ids go stale",
                )
                continue
            self.check_value(param, param.default, f"{path}.default", "view.invalid_default")

    def check_value(self, param: ParamDef, value: JsonValue, path: str, code: str) -> None:
        try:
            bind_json("view", self.defn.name, [param], {param.name: value}, self.model)
        except GraphCliError as exc:
            self.add(code, path, exc.message, **dict(exc.details))

    def examples(self) -> None:
        for index, example in enumerate(self.defn.examples):
            for name, value in example.params.items():
                path = f"examples[{index}].params.{name}"
                param = self.declared.get(name)
                if param is None:
                    self.add("view.invalid_example", path, f"{name!r} is not a parameter")
                elif param.type == "ref":
                    self.add(
                        "view.invalid_example",
                        path,
                        "an example carries no entity reference; they go stale",
                    )
                else:
                    self.check_value(param, value, path, "view.invalid_example")

    def unused(self) -> None:
        for name in self.declared:
            if name not in self.used:
                self.add(
                    "view.parameter_unused",
                    f"params.{name}",
                    f"parameter {name!r} is declared but no where entry or argument uses it",
                    parameter=name,
                )

    def entity_class(self) -> None:
        if self.defn.takes is None:
            return
        self.takes = self.resolve("type", self.defn.takes, self.model.classes, "takes")
        if self.takes is not None:
            self.types.add(self.takes)
            self.entity_classes = frozenset(self.model.subtypes(self.takes))

    # Terms

    def resolve(self, kind: str, name: str, known: Mapping[str, object], path: str) -> str | None:
        """The CURIE ``name`` means (``view.unknown_class``, ``view.unknown_relation`` or
        ``view.ambiguous_term`` otherwise)."""
        try:
            return resolve_term(kind, name, known)
        except GraphCliError as exc:
            code = (
                "view.ambiguous_term"
                if exc.code == "query.ambiguous_term"
                else f"view.unknown_{'class' if kind == 'type' else kind}"
            )
            self.add(code, path, exc.message, **dict(exc.details))
            return None

    def relation(self, name: str, path: str) -> str | None:
        found = self.resolve("relation", name, self.model.relations, path)
        if found is not None:
            self.relations.add(found)
        return found

    def classes_of(self, declared: tuple[str, ...]) -> frozenset[str]:
        """The classes a relation end or a type list stands for, subclasses included; every
        class for an end that is not restricted."""
        if not declared:
            return frozenset(self.model.classes)
        return frozenset(c for d in declared for c in self.model.subtypes(d))

    def end_of(self, relation: str, direction: str) -> tuple[str, ...]:
        """The classes at the far end of ``relation`` read in ``direction`` as declared."""
        found = self.model.relations[relation]
        return found.range if direction == "out" else found.domain

    def starts_at(self, relation: str, direction: str, classes: frozenset[str]) -> bool:
        """Whether an entity of one of ``classes`` can be at the near end."""
        found = self.model.relations[relation]
        near = found.domain if direction == "out" else found.range
        return any(self.model.is_a_any(c, near) for c in classes)

    def fits(
        self, relation: str, direction: str, classes: frozenset[str], path: str, where: str
    ) -> bool:
        """Report a relation that does not start at any of ``classes``; whether it does."""
        if self.starts_at(relation, direction, classes):
            return True
        found = self.model.relations[relation]
        near = found.domain if direction == "out" else found.range
        self.add(
            "view.relation_mismatch",
            path,
            f"{relation} read {direction} starts at "
            f"{'|'.join(near) if near else 'any class'}, which {where} can never be",
            relation=relation,
            direction=direction,
            expected=list(near),
            classes=sorted(classes)[:12],
        )
        return False

    def attribute_names(self, classes: frozenset[str]) -> set[str]:
        return {n for c in classes for n in self.model.classes[c].attributes}

    def field_names(self, sel: FieldSel, classes: frozenset[str], path: str) -> None:
        if not isinstance(sel, tuple):
            return
        known = self.attribute_names(classes)
        for index, name in enumerate(sel):
            self.touch(name)
            if name not in known:
                self.unknown_attribute(f"{path}[{index}]", name, known)

    def touch(self, attribute: str) -> None:
        if attribute not in SYSTEM_FIELDS:
            self.attributes.add(attribute)

    def unknown_attribute(self, path: str, name: str, known: set[str]) -> None:
        self.add(
            "view.unknown_attribute",
            path,
            f"no class of this section has the attribute {name!r}",
            kind="attribute",
            term=name,
            did_you_mean=difflib.get_close_matches(name, sorted(known), n=5),
        )

    # where and order

    def clauses(
        self,
        types: tuple[str, ...],
        classes: frozenset[str],
        where: tuple[str, ...],
        order: tuple[str, ...],
        path: str,
    ) -> None:
        """Plan the entries with the ``find`` planner against ``types`` (``()``: any class); a
        ``$name`` is planned with a placeholder operand of the parameter's type."""
        for index, text in enumerate(where):
            here = f"{path}.where[{index}]"
            left, name = where_parts(text)
            attribute, _, op = left.partition(":")
            self.touch(attribute)
            clause: Clause = text
            if name is not None:
                param = self.parameter(name, here)
                if param is None:
                    continue
                if self.where_fit(param, attribute, op or "eq", classes, here):
                    continue
                operand = "*" if op == "lang" else _operand(placeholder(param, self.model))
                clause = (left, operand)
            self.plan(types, here, "view.invalid_where", where=(clause,))
        for index, text in enumerate(order):
            self.touch(text.partition(":")[0])
            self.plan(types, f"{path}.order[{index}]", "view.invalid_order", sort=(text,))

    def plan(
        self,
        types: tuple[str, ...],
        path: str,
        code: str,
        *,
        where: tuple[Clause, ...] = (),
        sort: tuple[str, ...] = (),
    ) -> None:
        try:
            plan_clauses(self.model, types, where, sort)
        except GraphCliError as exc:
            if exc.code == "term.unknown" and exc.details.get("kind") == "field":
                known = exc.details.get("did_you_mean")
                self.add(
                    "view.unknown_attribute",
                    path,
                    exc.message,
                    kind="attribute",
                    term=exc.details.get("term"),
                    did_you_mean=known if isinstance(known, list) else [],
                )
                return
            details = {k: v for k, v in exc.details.items() if k != "argument"}
            self.add(
                code,
                path,
                exc.message.removeprefix("--where: ").removeprefix("--sort: "),
                error=exc.code,
                **details,
            )

    def parameter(self, name: str, path: str) -> ParamDef | None:
        """The declared parameter ``$name`` stands for (a problem for an undeclared one)."""
        if name == ENTITY:
            self.add(
                "view.placeholder_position",
                path,
                f"${ENTITY} is the entity of an entity view and can only be an argument of a "
                "rows section",
            )
            return None
        param = self.declared.get(name)
        if param is None:
            self.add(
                "view.parameter_undeclared",
                path,
                f"parameter ${name} is not declared under params",
                parameter=name,
            )
        return param

    def where_fit(
        self, param: ParamDef, attribute: str, op: str, classes: frozenset[str], path: str
    ) -> bool:
        """Report a parameter that does not fit the attribute it is compared with; whether it
        did not."""
        base, many = self.base_of(attribute, classes)
        if base is None:
            return False  # unknown attribute: the planner reports it
        reason: str | None = None
        if op == "exists":
            reason = "exists takes true or false, not a parameter"
        elif op == "in":
            reason = None if param.many else "in needs a list parameter"
        elif op in _TEXT_OPS:
            if param.many or param.type != "string":
                reason = f"{op} takes a string parameter"
        elif param.many:
            reason = "a list parameter needs the in operator"
        if reason is None and op not in _TEXT_OPS and op != "exists":
            reason = self.type_misfit(param, attribute, base, many)
        if reason is None:
            return False
        self.add(
            "view.type_mismatch",
            path,
            f"parameter {param.name!r} is compared with {attribute}: {reason}",
            parameter=param.name,
            attribute=attribute,
        )
        return True

    def type_misfit(self, param: ParamDef, attribute: str, base: str, many: bool) -> str | None:
        if param.type == "ref" and attribute != "id":
            # a ref names one entity: its id says it all; not every entity has an alias
            return "compare a ref parameter with id"
        if param.type == "decimal" and base == "integer":
            return "a decimal does not fit an integer attribute"  # 1.5 is no integer
        if base in LANG_BASES and param.type == "string":
            return None
        if base not in FITS[param.type]:
            kind = f"list of {base}" if many else base
            return f"a {param.type} does not fit the attribute ({kind})"
        return None

    def base_of(self, attribute: str, classes: frozenset[str]) -> tuple[str | None, bool]:
        """The base type of an attribute or system field and whether it holds a list."""
        if attribute in SYSTEM_FIELDS:
            return SYSTEM_BASES.get(attribute, "string"), False
        for cls in sorted(classes):
            found = self.model.classes[cls].attributes.get(attribute)
            if found is not None:
                return self.model.value_base(found.kind, found.type), found.many
        return None, False

    # Sections

    def section(self, index: int, section: SectionDef) -> SectionDef | None:
        path = f"sections[{index}]"
        before = len(self.problems)
        unknown_class = self.defn.takes is not None and self.takes is None
        if unknown_class and isinstance(
            section, FieldsSection | RelationsSection | NeighborsSection
        ):
            return None  # the class is unknown (reported): nothing to check them against
        found: SectionDef | None
        if isinstance(section, FieldsSection):
            self.field_names(section.fields, self.entity_classes, f"{path}.fields")
            found = section
        elif isinstance(section, RelationsSection):
            found = self.relations_section(section, path)
        elif isinstance(section, FilesSection | AffordancesSection):
            found = section
        elif isinstance(section, NeighborsSection):
            found = self.neighbors_section(section, path)
        elif isinstance(section, ListSection):
            found = self.list_section(section, path)
        else:
            found = self.rows_section(section, path)
        return found if len(self.problems) == before else None

    def relations_section(self, section: RelationsSection, path: str) -> RelationsSection:
        if section.relations is None:
            return section
        refs: list[RelationRef] = []
        for index, ref in enumerate(section.relations):
            here = f"{path}.relations[{index}]"
            curie = self.relation(ref.relation, f"{here}.relation")
            if curie is None:
                continue
            self.fits(curie, ref.direction, self.entity_classes, here, "the entity")
            refs.append(RelationRef(curie, ref.direction))
        return replace(section, relations=tuple(refs))

    def neighbors_section(self, section: NeighborsSection, path: str) -> NeighborsSection | None:
        curie = self.relation(section.relation, f"{path}.relation")
        if curie is None:
            return None
        if not self.fits(
            curie, section.direction, self.entity_classes, f"{path}.relation", "the entity"
        ):
            return None
        end = self.end_of(curie, section.direction)
        classes = self.classes_of(end)
        self.field_names(section.fields, classes, f"{path}.fields")
        self.clauses(end, classes, section.where, section.order, path)
        relations = self.row_relations(section.relations, classes, path)
        return replace(section, relation=curie, relations=relations)

    def list_section(self, section: ListSection, path: str) -> ListSection | None:
        types: list[str] = []
        for index, name in enumerate(section.types):
            curie = self.resolve("type", name, self.model.classes, f"{path}.type[{index}]")
            if curie is not None:
                types.append(curie)
                self.types.add(curie)
        if len(types) != len(section.types):
            return None
        classes = self.classes_of(tuple(types))
        self.field_names(section.fields, classes, f"{path}.fields")
        self.clauses(tuple(types), classes, section.where, section.order, path)
        relations = self.row_relations(section.relations, classes, path)
        return replace(section, types=tuple(types), relations=relations)

    def row_relations(
        self, relations: tuple[RowRelation, ...], classes: frozenset[str], path: str
    ) -> tuple[RowRelation, ...]:
        found: list[RowRelation] = []
        for index, rel in enumerate(relations):
            here = f"{path}.relations[{index}]"
            if rel.relation is None:
                found.append(rel)
                continue
            curie = self.relation(rel.relation, f"{here}.relation")
            if curie is None:
                continue
            if not self.fits(curie, rel.direction, classes, f"{here}.relation", "the rows"):
                continue
            end = self.end_of(curie, rel.direction)
            ends = self.classes_of(end)
            self.field_names(rel.fields, ends, f"{here}.fields")
            self.clauses(end, ends, rel.where, rel.order, here)
            found.append(replace(rel, relation=curie))
        return tuple(found)

    # Functions

    def rows_section(self, section: RowsSection, path: str) -> RowsSection | None:
        function = self.functions.get(section.function)
        if function is None:
            names = sorted(self.functions)
            self.add(
                "view.unknown_function",
                f"{path}.function",
                f"the graph has no function {section.function!r}",
                function=section.function,
                did_you_mean=difflib.get_close_matches(section.function, names, n=5),
            )
            return None
        self.function_names.add(function.name)
        declared = {p.name: p for p in function.defn.params}
        for name, value in section.args.items():
            target = declared.get(name)
            here = f"{path}.args.{name}"
            if target is None:
                self.add(
                    "view.unknown_argument",
                    here,
                    f"function {function.name} has no parameter {name!r}",
                    function=function.name,
                    argument=name,
                    expected=list(declared),
                )
            else:
                self.argument(function.name, target, value, here)
        for name, target in declared.items():
            if target.required and name not in section.args:
                self.add(
                    "view.argument_missing",
                    f"{path}.args",
                    f"function {function.name} needs the parameter {name!r} ({target.type_text})",
                    function=function.name,
                    argument=name,
                )
        return section

    def argument(self, function: str, target: ParamDef, value: JsonValue, path: str) -> None:
        name = placeholder_name(value)
        if name is None:
            self.literal_argument(target, value, path)
            return
        if name == ENTITY:
            self.entity_argument(function, target, path)
            return
        source = self.parameter(name, path)
        if source is not None:
            reason = self.incompatible(source, target)
            if reason is not None:
                self.add(
                    "view.type_mismatch",
                    path,
                    f"parameter ${name} ({source.type_text}) does not fit {function}."
                    f"{target.name} ({target.type_text}): {reason}",
                    parameter=name,
                    function=function,
                    argument=target.name,
                )

    def entity_argument(self, function: str, target: ParamDef, path: str) -> None:
        if self.defn.takes is None:
            self.add(
                "view.placeholder_position",
                path,
                f"${ENTITY} needs an entity view; a set view takes no entity",
            )
            return
        if self.takes is None:
            return  # takes is unknown: reported there
        if target.type != "ref" or target.many:
            self.add(
                "view.type_mismatch",
                path,
                f"${ENTITY} is a reference; {function}.{target.name} is {target.type_text}",
                function=function,
                argument=target.name,
            )
        elif target.of_class is None or not self.model.is_a(self.takes, target.of_class):
            self.add(
                "view.type_mismatch",
                path,
                f"${ENTITY} is a {self.takes}, which is not a {target.of_class} "
                f"({function}.{target.name})",
                function=function,
                argument=target.name,
            )

    def literal_argument(self, target: ParamDef, value: JsonValue, path: str) -> None:
        if target.type == "ref":
            self.add(
                "view.literal_reference",
                path,
                "a ref argument is $entity or a view parameter; entity ids in a file go stale",
            )
            return
        try:
            bind_json("view", self.defn.name, [target], {target.name: value}, self.model)
        except GraphCliError as exc:
            self.add("view.invalid_value", path, exc.message, **dict(exc.details))

    def incompatible(self, source: ParamDef, target: ParamDef) -> str | None:
        """Why a view parameter cannot feed a function parameter (``None``: it can)."""
        if source.many != target.many:
            return "a list does not fit a single value" if source.many else "needs a list"
        if source.type == target.type:
            if source.type == "ref" and not (
                source.of_class is not None
                and target.of_class is not None
                and self.model.is_a(source.of_class, target.of_class)
            ):
                return f"{source.of_class} is not a {target.of_class}"
            if source.type == "enum" and source.enum != target.enum:
                return f"enum {source.enum} is not enum {target.enum}"
            return None
        if source.type == "integer" and target.type == "decimal":
            return None
        return f"{source.type} is not {target.type}"

    # Entry

    def entry(self, normalized: ViewDef) -> dict[str, JsonValue]:
        entry: dict[str, JsonValue] = {
            **normalized.to_json(),
            "source": self.defn.to_json(),
            "touches": self.touches(),
        }
        entry["hash"] = hash_document(entry)
        return entry

    def touches(self) -> dict[str, JsonValue]:
        modules = {m.prefix: m.id for m in self.model.modules}

        def named(curies: set[str]) -> list[JsonValue]:
            return [
                {"curie": c, "module": modules.get(c.partition(":")[0])} for c in sorted(curies)
            ]

        return {
            "types": named(self.types),
            "relations": named(self.relations),
            "attributes": list[JsonValue](sorted(self.attributes)),
            "functions": list[JsonValue](sorted(self.function_names)),
        }
