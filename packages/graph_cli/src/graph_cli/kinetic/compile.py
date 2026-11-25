"""Checking and compiling functions against a compiled ontology (plan K3, K4, K5).

``compile_catalog`` reads the function files of a graph, checks each against
the model and returns the catalog document ``graph_cli.functions/1`` together
with every problem found; the document holds only functions without problems,
and a caller publishes only when there are none. A function is checked by
resolving its query with typed placeholders for the declared parameters, so no
data and no parameter values are needed. Besides what the query resolver
refuses (unknown names, unbound variables, ...) the checks are: parameters
declared and used (and none in ``SKIP``/``LIMIT``), their types against the
fields they are compared with, relationship patterns the relation's domain and
range allow, the declared columns, and declared column types against what the query makes
certain (``return_types``).

The compiled entry stores the normalized query text (``render`` of the
resolved tree: CURIEs, backticked), which parses and resolves again against
the current model. Compiling reads that text back and refuses a function
whose stored form does not resolve to the same query
(``function.round_trip_failed``, a defect of graph_cli), so publishing never
stores a query that ``graph run`` would refuse. ``check_catalog`` runs the
same checks over a published catalog against another model (the release
invariant, K5).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Final

from graph_cli.kernel.canonical import JsonObject, JsonValue, hash_document
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.json_access import as_object, req_list, req_str
from graph_cli.kinetic.bind import bind_json, placeholder
from graph_cli.kinetic.format import (
    CATALOG_SCHEMA,
    FunctionDef,
    ParamDef,
    Problem,
    load_function,
)
from graph_cli.kinetic.return_types import ColumnType, contradicts, infer_columns, merge
from graph_cli.ontology_layer.connecting import connecting
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.ontology_layer.vocabulary import BUILTIN_MODULES, SYSTEM_FIELDS
from graph_cli.query.cypher import parameters, render
from graph_cli.query.cypher.ast import (
    HasLabels,
    NodePattern,
    Parameter,
    PatternPart,
    Query,
    RelPattern,
    Return,
    With,
)
from graph_cli.query.cypher.compare import Comparison, comparisons
from graph_cli.query.cypher.parser import parse
from graph_cli.query.cypher.resolve import SYSTEM_BASES, FieldUse, ResolvedQuery, VarInfo, resolve
from graph_cli.query.cypher.text_params import TEXT_BASES
from graph_cli.query.cypher.values import Value
from graph_cli.query.cypher.walk import clause_expressions, clause_patterns

MAX_QUERY_CHARS: Final = 2000
MAX_FUNCTIONS: Final = 1000

# Base types of a field that a parameter of the declared type can be compared with.
FITS: Final[dict[str, frozenset[str]]] = {
    "string": frozenset({"string", "text", "iri"}),
    "enum": frozenset({"string", "text"}),
    "integer": frozenset({"integer", "decimal"}),
    "decimal": frozenset({"decimal", "integer"}),
    "boolean": frozenset({"boolean"}),
    "date": frozenset({"date"}),
    "datetime": frozenset({"datetime"}),
    "ref": frozenset({"string"}),
}
REF_FIELDS: Final = frozenset({"id", "alias"})


def compile_catalog(
    sources: Mapping[str, str | bytes], model: CompiledModel
) -> tuple[dict[str, JsonValue], list[Problem]]:
    """Compile the function files (file name -> content) of one graph against ``model``."""
    problems: list[Problem] = []
    defs: list[FunctionDef] = []
    for file_name in sorted(sources):
        defn, found = load_function(file_name, sources[file_name])
        problems.extend(found)
        if defn is not None:
            defs.append(defn)
    if len(sources) > MAX_FUNCTIONS:
        problems.append(_too_many(len(sources)))
    document, found = _compile(defs, model)
    return document, [*problems, *found]


def check_catalog(document: JsonObject, model: CompiledModel) -> list[Problem]:
    """The problems a published catalog has against ``model`` (none: it still holds).

    Every function is compiled again from its stored source, and the stored
    normalized query text must still resolve (``function.stale``). A document of
    another shape raises ``JsonShapeError``, an unknown schema
    ``format.unsupported_version``.
    """
    schema = req_str(document, "schema")
    if schema != CATALOG_SCHEMA:
        raise GraphCliError(
            "format.unsupported_version",
            f"unsupported function catalog format {schema!r}; expected {CATALOG_SCHEMA}",
            details={"expected": CATALOG_SCHEMA, "actual": schema},
        )
    entries = [as_object(e, "functions") for e in req_list(document, "functions")]
    defs = [FunctionDef.from_json(e, req_str(e, "source_query")) for e in entries]
    _, problems = _compile(defs, model)
    if len(defs) > MAX_FUNCTIONS:
        problems.append(_too_many(len(defs)))
    for defn, entry in zip(defs, entries, strict=True):
        text = req_str(entry, "query")
        stale = _check_stored(defn, text, model)
        if stale is not None:
            problems.append(stale)
    return problems


def _too_many(count: int) -> Problem:
    return Problem(
        "(catalog)",
        "function.too_many_functions",
        f"{count} functions; at most {MAX_FUNCTIONS} per graph",
        details={"count": count, "limit": MAX_FUNCTIONS},
    )


def _compile(
    defs: Sequence[FunctionDef], model: CompiledModel
) -> tuple[dict[str, JsonValue], list[Problem]]:
    active = frozenset(d.name for d in defs if d.status == "active")
    entries: list[JsonValue] = []
    problems: list[Problem] = []
    for defn in sorted(defs, key=lambda d: d.name):
        check = _Compile(defn, model, active)
        entry = check.run()
        problems.extend(check.problems)
        if entry is not None and not check.problems:
            entries.append(entry)
    return {"schema": CATALOG_SCHEMA, "functions": entries}, problems


def _resolve_with_placeholders(
    query: Query, defn: FunctionDef, model: CompiledModel, paging: frozenset[str] = frozenset()
) -> ResolvedQuery:
    """Resolve with a placeholder per parameter the query uses."""
    declared = {p.name: p for p in defn.params}
    values: dict[str, Value] = {}
    for use in parameters(query):
        if use.name in paging:
            values[use.name] = 0
        elif use.name in declared:
            values[use.name] = placeholder(declared[use.name], model)
        else:
            values[use.name] = None  # reported as undeclared; resolve goes on
    return resolve(query, model, values)


def resolve_stored(text: str, defn: FunctionDef, model: CompiledModel) -> ResolvedQuery:
    """Resolve a stored (normalized) query text with placeholder parameters, as compiling does.

    For callers that analyse a published function's query without running it.
    """
    parsed = parse(text)
    paging = frozenset(use.name for use in parameters(parsed) if use.paging)
    return _resolve_with_placeholders(parsed, defn, model, paging)


def column_types(
    defn: FunctionDef, text: str, model: CompiledModel
) -> tuple[ColumnType | None, ...]:
    """The type of each result column of a function with the stored query ``text``, against
    ``model``: what the query makes certain, with what the author declared (``None``: untyped).
    A query that no longer resolves has only its declared types."""
    declared = tuple(r.declared for r in defn.returns)
    try:
        inferred = infer_columns(resolve_stored(text, defn, model), model)
    except GraphCliError:
        return declared
    if len(inferred) != len(declared):
        return declared
    return tuple(merge(d, i) for d, i in zip(declared, inferred, strict=True))


def single_refs(
    document: JsonObject, model: CompiledModel, exempt: frozenset[str] = frozenset()
) -> list[tuple[str, str, str]]:
    """The single-value ``ref`` parameters of the active functions of a compiled catalog that a
    list would serve as well: ``(function, parameter, class)`` in catalog order.

    A parameter qualifies when it is a ``ref`` without ``many`` and every use of it in the
    stored query is an ``=`` comparison with a field (or an inline map entry): the function
    can then take a list and use ``IN``, and answers several entities in one run. A parameter
    that is also returned, ordered against or compared otherwise is left alone. Skipped are
    functions in ``exempt`` (those a view runs, which hands them one entity), deprecated
    functions, a function with more than one single-value ``ref`` (a question about a pair:
    lists would mean a cross product), a function whose stored query has a ``LIMIT`` (a
    pick per entity cannot be batched) and a function whose stored query does not resolve
    against ``model`` (checking the catalog reports that).
    """
    found: list[tuple[str, str, str]] = []
    for item in req_list(document, "functions"):
        entry = as_object(item, "functions")
        defn = FunctionDef.from_json(entry, req_str(entry, "source_query"))
        refs = [p for p in defn.params if p.type == "ref" and not p.many]
        if defn.status != "active" or defn.name in exempt or len(refs) != 1:
            continue
        try:
            resolved = resolve_stored(req_str(entry, "query"), defn, model)
        except GraphCliError:
            continue
        if any(
            isinstance(c, With | Return) and c.projection.limit is not None
            for c in resolved.query.clauses
        ):
            continue
        compared: dict[str, list[Comparison]] = {}
        for use in comparisons(resolved.query):
            if isinstance(use.value, Parameter):
                compared.setdefault(use.value.name, []).append(use)
        occurrences = Counter(use.name for use in parameters(resolved.query))
        for param in refs:
            mine = compared.get(param.name, [])
            if (
                mine
                and len(mine) == occurrences[param.name]
                and all(c.shape == "value" and c.op == "=" for c in mine)
            ):
                found.append((defn.name, param.name, param.of_class or ""))
    return found


def _check_stored(defn: FunctionDef, text: str, model: CompiledModel) -> Problem | None:
    try:
        _resolve_with_placeholders(parse(text), defn, model)
    except GraphCliError as exc:
        return Problem(
            defn.name,
            "function.stale",
            f"the stored query no longer resolves against this model: {exc.message}",
            "query",
            details={"error": exc.code},
        )
    return None


def _normalized(resolved: ResolvedQuery) -> str:
    return "\n".join(render.clause(c) for c in resolved.query.clauses)


class _Compile:
    """The checks of one function; ``run`` returns the entry, problems collect in ``problems``."""

    def __init__(self, defn: FunctionDef, model: CompiledModel, active: frozenset[str]) -> None:
        self.defn = defn
        self.model = model
        self.active = active
        self.problems: list[Problem] = []
        self.declared = {p.name: p for p in defn.params}
        self.emptied: set[str] = set()  # variables whose labels exclude each other, reported

    def add(self, code: str, path: str | None, message: str, **details: JsonValue) -> None:
        self.problems.append(Problem(self.defn.name, code, message, path, None, details))

    def error(self, exc: GraphCliError, path: str) -> None:
        self.problems.append(
            Problem(self.defn.name, exc.code, exc.message, path, None, dict(exc.details))
        )

    def run(self) -> dict[str, JsonValue] | None:
        module = self.module()
        self.parameters()
        self.declared_returns()
        self.examples()
        self.replacement()
        text = self.defn.query
        if len(text) > MAX_QUERY_CHARS:
            self.add(
                "function.query_too_long",
                "query",
                f"the query is {len(text)} characters; at most {MAX_QUERY_CHARS}",
                length=len(text),
                limit=MAX_QUERY_CHARS,
            )
        try:
            parsed = parse(text)
        except GraphCliError as exc:
            self.error(exc, "query")
            return None
        paging = self.parameter_uses(parsed)
        try:
            resolved = _resolve_with_placeholders(parsed, self.defn, self.model, paging)
        except GraphCliError as exc:
            self.error(exc, "query")
            return None
        self.columns(resolved)
        self.return_types(resolved)
        self.type_fit(resolved)
        self.patterns(resolved)
        self.round_trip(_normalized(resolved), paging)
        return self.entry(module, resolved)

    def round_trip(self, text: str, paging: frozenset[str]) -> None:
        """The stored text must resolve again, and to the same query."""
        try:
            again = _resolve_with_placeholders(parse(text), self.defn, self.model, paging)
        except GraphCliError as exc:
            reason = f"{exc.code}: {exc.message}"
        else:
            if _normalized(again) == text:
                return
            reason = "it reads back as another query"
        self.add(
            "function.round_trip_failed",
            "query",
            f"the normalized form of the query does not read back ({reason}); this is a defect"
            " of graph_cli: write the query another way and report it",
            normalized=text,
        )

    # Declaration

    def module(self) -> str | None:
        return resolve_module(self.model, self.defn.module, "function", self.add)

    def parameters(self) -> None:
        for param in self.defn.params:
            path = f"params.{param.name}"
            usable = True
            if param.type == "ref" and param.of_class not in self.model.classes:
                self.add(
                    "function.unknown_class",
                    f"{path}.class",
                    f"the model has no class {param.of_class!r}",
                    curie=param.of_class,
                )
                usable = False
            if param.type == "enum" and param.enum not in self.model.enums:
                self.add(
                    "function.unknown_enum",
                    f"{path}.enum",
                    f"the model has no enum {param.enum!r}",
                    curie=param.enum,
                )
                usable = False
            if param.default is None or not usable:
                continue
            if param.type == "ref":
                self.add(
                    "function.invalid_default",
                    f"{path}.default",
                    "a ref has no default; entity ids go stale",
                )
                continue
            self.check_value(param, param.default, f"{path}.default", "function.invalid_default")

    def check_value(self, param: ParamDef, value: JsonValue, path: str, code: str) -> None:
        try:
            bind_json(self.defn.name, [param], {param.name: value}, self.model)
        except GraphCliError as exc:
            self.add(code, path, exc.message, **dict(exc.details))

    def examples(self) -> None:
        for index, example in enumerate(self.defn.examples):
            for name, value in example.params.items():
                path = f"examples[{index}].params.{name}"
                param = self.declared.get(name)
                if param is None:
                    self.add("function.invalid_example", path, f"{name!r} is not a parameter")
                elif param.type == "ref":
                    self.add(
                        "function.invalid_example",
                        path,
                        "an example carries no entity reference; they go stale",
                    )
                else:
                    self.check_value(param, value, path, "function.invalid_example")

    def replacement(self) -> None:
        target = self.defn.replaced_by
        if target is not None and target not in self.active:
            self.add(
                "function.replaced_by_invalid",
                "replaced_by",
                f"{target!r} is not an active function of the graph",
                replaced_by=target,
            )

    # Query

    def parameter_uses(self, parsed: Query) -> frozenset[str]:
        """Declared against used parameters; returns the names used in SKIP or LIMIT."""
        uses = parameters(parsed)
        paging: set[str] = set()
        reported: set[str] = set()
        for use in uses:
            where = {"line": use.pos.line, "column": use.pos.column}
            if use.paging:
                paging.add(use.name)
                self.add(
                    "function.parameter_paging",
                    "query",
                    f"parameter ${use.name} is used in SKIP or LIMIT; paging is not a "
                    "function parameter",
                    parameter=use.name,
                    **where,
                )
            elif use.name not in self.declared and use.name not in reported:
                reported.add(use.name)
                self.add(
                    "function.parameter_undeclared",
                    "query",
                    f"parameter ${use.name} is not declared under params",
                    parameter=use.name,
                    **where,
                )
        used = {use.name for use in uses}
        for name in self.declared:
            if name not in used:
                self.add(
                    "function.parameter_unused",
                    f"params.{name}",
                    f"parameter {name!r} is declared but the query does not use it",
                    parameter=name,
                )
        return frozenset(paging)

    def columns(self, resolved: ResolvedQuery) -> None:
        if resolved.query.returns.projection.star:
            self.add(
                "function.returns_star",
                "query",
                "RETURN * has no stable columns; list them",
            )
            return
        declared = [r.column for r in self.defn.returns]
        if declared != list(resolved.columns):
            self.add(
                "function.returns_mismatch",
                "returns",
                "returns must list exactly the result columns of the query, in order",
                expected=list(resolved.columns),
                actual=declared,
            )

    # Return types

    def declared_returns(self) -> None:
        """The enum and class of a declared column type must be terms of the model."""
        for index, ret in enumerate(self.defn.returns):
            if ret.enum is not None and ret.enum not in self.model.enums:
                self.add(
                    "function.unknown_enum",
                    f"returns[{index}].enum",
                    f"the model has no enum {ret.enum!r}",
                    curie=ret.enum,
                )
            if ret.of_class is not None and ret.of_class not in self.model.classes:
                self.add(
                    "function.unknown_class",
                    f"returns[{index}].class",
                    f"the model has no class {ret.of_class!r}",
                    curie=ret.of_class,
                )

    def return_types(self, resolved: ResolvedQuery) -> None:
        """A declared type must not contradict what the query makes certain."""
        if [r.column for r in self.defn.returns] != list(resolved.columns):
            return  # reported by ``columns``
        inferred = infer_columns(resolved, self.model)
        for index, (ret, found) in enumerate(zip(self.defn.returns, inferred, strict=False)):
            declared = ret.declared
            if declared is None or found is None or not contradicts(declared, found):
                continue
            self.add(
                "function.returns_type_mismatch",
                f"returns[{index}].type",
                f"column {ret.column!r} is declared {declared.text}, but its query makes it "
                f"{found.text}: declare that, or drop the type",
                column=ret.column,
                declared=declared.text,
                inferred=found.text,
            )

    # Parameter types

    def type_fit(self, resolved: ResolvedQuery) -> None:
        fields = {(f.variable, f.field): f for f in resolved.fields}
        for use in comparisons(resolved.query):
            if not isinstance(use.value, Parameter):
                continue
            param = self.declared.get(use.value.name)
            if param is None:
                continue
            used = fields.get((use.variable, use.field)) if use.variable is not None else None
            if use.variable is None and use.node is not None:
                used = self.anonymous_field(use.node, use.field)
            if used is None:
                continue
            reason = self.misfit(param, use.shape, used)
            if reason is not None:
                label = f"{use.variable or 'a node'}.{use.field}"
                self.add(
                    "function.type_mismatch",
                    f"params.{param.name}",
                    f"parameter {param.name!r} is compared with {label}: {reason}",
                    parameter=param.name,
                    field=use.field,
                    datatypes=list(used.datatypes),
                    line=use.value.pos.line,
                    column=use.value.pos.column,
                )

    def misfit(self, param: ParamDef, shape: str, used: FieldUse) -> str | None:
        if shape == "in_field":
            if not used.many:
                return "the field holds a single value, not a list"
            if param.many:
                return "a list parameter cannot be one member of the field"
        elif shape == "in_param":
            if not param.many:
                return "IN needs a list parameter"
            if used.many:
                return "the field is a list itself"
        elif shape == "text":
            if param.many or param.type != "string":
                return "text matching takes a string parameter"
            if used.many or not set(used.datatypes) & TEXT_BASES:
                return f"the field is {_describe_field(used)}, not text"
            return None
        elif param.many != used.many:
            return (
                "a list parameter cannot be compared with a single value"
                if param.many
                else "a single value cannot be compared with a list field"
            )
        if param.type == "ref" and used.field not in REF_FIELDS:
            return "a ref can only be compared with id or alias"
        if not set(used.datatypes) & FITS[param.type]:
            return f"a {param.type} does not fit the field ({_describe_field(used)})"
        return None

    def anonymous_field(self, node: NodePattern, name: str) -> FieldUse | None:
        classes = _node_classes(self.model, node, {})
        if not classes:
            return None
        if name in SYSTEM_FIELDS:
            return FieldUse(None, name, (SYSTEM_BASES.get(name, "string"),), False)
        bases: set[str] = set()
        many = False
        for cls in classes:
            attribute = self.model.classes[cls].attributes.get(name)
            if attribute is not None:
                bases.add(self.model.value_base(attribute.kind, attribute.type))
                many = many or attribute.many
        return FieldUse(None, name, tuple(sorted(bases)), many) if bases else None

    # Patterns

    def patterns(self, resolved: ResolvedQuery) -> None:
        scope: dict[str, VarInfo] = {}
        for clause, introduced in zip(resolved.query.clauses, resolved.introduced, strict=True):
            scope.update(introduced)
            for part in clause_patterns(clause):
                self.pattern(part, scope)

    def pattern(self, part: PatternPart, scope: Mapping[str, VarInfo]) -> None:
        ends = [_node_classes(self.model, n, scope) for n in part.nodes]
        for node, classes in zip(part.nodes, ends, strict=True):
            if classes is not None and not classes and node.variable not in self.emptied:
                self.emptied.add(node.variable or "")
                self.add(
                    "function.pattern_impossible",
                    "query",
                    f"no type has all the labels of {node.variable or 'this node'}; "
                    "the pattern would never match",
                    line=node.pos.line,
                    column=node.pos.column,
                )
        for index, rel in enumerate(part.rels):
            source, target = ends[index], ends[index + 1]
            if not rel.types or source == frozenset() or target == frozenset():
                continue
            if self.possible(rel, source, target):
                continue
            left, right = part.nodes[index], part.nodes[index + 1]
            details: dict[str, JsonValue] = {
                "relations": list(rel.types),
                "source": _end_types(left, source),
                "target": _end_types(right, target),
                "line": rel.pos.line,
                "column": rel.pos.column,
            }
            if source and target:
                details["connecting"] = connecting(
                    self.model, self.general(source), self.general(target)
                )
            self.add(
                "function.pattern_impossible",
                "query",
                f"{', '.join(rel.types)} cannot connect {_end_name(left, source)} to "
                f"{_end_name(right, target)} (domain and range of the relation); "
                "the pattern would never match",
                **details,
            )

    def possible(
        self, rel: RelPattern, source: frozenset[str] | None, target: frozenset[str] | None
    ) -> bool:
        low, high = (1, 1) if rel.length is None else rel.length
        if low == 0 and (source is None or target is None or source & target):
            return True  # the empty path: both ends are one node
        if high == 0:
            return False
        orientations = {
            "out": [(source, target)],
            "in": [(target, source)],
            "both": [(source, target), (target, source)],
        }[rel.direction]
        for name in rel.types:
            definition = self.model.relations[name]
            for head, tail in orientations:
                if self.accepts(definition.domain, head) and self.accepts(definition.range, tail):
                    return True
        return False

    def accepts(self, accepted: tuple[str, ...], classes: frozenset[str] | None) -> bool:
        return classes is None or any(self.model.is_a_any(c, accepted) for c in classes)

    def general(self, classes: frozenset[str]) -> str:
        """The most general of the classes: the one the hint should start from."""
        return min(classes, key=lambda c: (len(self.model.classes[c].ancestors), len(c), c))

    # Entry

    def entry(self, module: str | None, resolved: ResolvedQuery) -> dict[str, JsonValue]:
        entry: dict[str, JsonValue] = {
            **self.defn.to_json(),
            "module": module,
            "source_query": self.defn.query,
            "query": _normalized(resolved),
            "touches": self.touches(resolved),
            "fields": [f.to_json() for f in resolved.fields],
        }
        entry["hash"] = hash_document(entry)
        return entry

    def touches(self, resolved: ResolvedQuery) -> dict[str, JsonValue]:
        types: set[str] = set()
        relations: set[str] = set()
        for clause in resolved.query.clauses:
            for part in clause_patterns(clause):
                types.update(label for n in part.nodes for g in n.labels for label in g)
                relations.update(t for r in part.rels for t in r.types)
            for e in clause_expressions(clause):
                if isinstance(e, HasLabels):
                    types.update(label for g in e.labels for label in g)
        modules = {m.prefix: m.id for m in self.model.modules}

        def named(curies: set[str]) -> list[JsonValue]:
            return [
                {"curie": c, "module": modules.get(c.partition(":")[0])} for c in sorted(curies)
            ]

        return {"types": named(types), "relations": named(relations)}


def _article(kind: str) -> str:
    return "an" if kind[0] in "aeiou" else "a"


def resolve_module(
    model: CompiledModel, wanted: str | None, kind: str, add: Callable[..., None]
) -> str | None:
    """The id of the module a function or action belongs to (``<kind>.module_required``,
    ``<kind>.builtin_module``, ``<kind>.unknown_module`` through ``add``)."""
    own = [m for m in model.modules if m.id not in BUILTIN_MODULES]
    if wanted is None:
        if len(own) > 1:
            add(
                f"{kind}.module_required",
                "module",
                f"the graph has several modules; name the module this {kind} belongs to",
                expected=[m.id for m in own],
            )
        return own[0].id if len(own) == 1 else None
    for info in model.modules:
        if wanted in (info.id, info.prefix):
            if info.id in BUILTIN_MODULES:
                add(
                    f"{kind}.builtin_module",
                    "module",
                    f"module {wanted!r} is built in; {_article(kind)} {kind} belongs to a module "
                    "of the graph",
                    expected=[m.id for m in own],
                )
                return None
            return info.id
    add(
        f"{kind}.unknown_module",
        "module",
        f"the graph has no module {wanted!r}",
        expected=[m.id for m in own],
    )
    return None


def _describe_field(used: FieldUse) -> str:
    kind = "/".join(used.datatypes)
    return f"list of {kind}" if used.many else kind


def _node_classes(
    model: CompiledModel, node: NodePattern, scope: Mapping[str, VarInfo]
) -> frozenset[str] | None:
    """The classes the node can have (None: any); empty when its labels exclude each other."""
    found: set[str] | None = None
    for group in node.labels:
        members = {c for label in group for c in model.subtypes(label)}
        found = members if found is None else found & members
    info = scope.get(node.variable) if node.variable is not None else None
    if info is not None and info.kind == "node" and info.types is not None:
        known = set(info.types)
        found = known if found is None else found & known
    return None if found is None else frozenset(found)


def _end_types(node: NodePattern, classes: frozenset[str] | None) -> list[JsonValue]:
    labels: list[JsonValue] = [label for group in node.labels for label in group]
    return labels or list[JsonValue](sorted(classes or ()))


def _end_name(node: NodePattern, classes: frozenset[str] | None) -> str:
    names = _end_types(node, classes)
    return "|".join(str(n) for n in names) if names else "any type"
