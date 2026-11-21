"""Which published functions cover an ad-hoc query (usage shapes plan U2).

A function *covers* a query when running it with the right parameter answers the
question the query asked, possibly with extra columns or a client-side filter.
The rule is deliberately structural and deterministic: no ranking model, no
text matching. Both sides get a signature computed from a resolved query (a
function's from its stored normalized text, so there is no second description
of what it does), and the signatures are compared.

A signature has:

- *anchors*: node variables fixed by ``=`` or ``IN`` against data. Data is a
  literal or constant list or ``--param`` value on the query side, compared
  with ``id`` or ``alias`` only (the keys a ``ref`` takes: a filter on a flag
  fixes no entity), and a ``$parameter`` on any key on the function side (its
  ``ref`` type vouches for the key). Anchors count in the ``WHERE`` of a
  non-optional ``MATCH`` or ``WITH`` (through ``AND``; a disjunction counts only
  when every branch is an equality on one variable and key, which is an ``IN``)
  and in the inline property map of a non-optional ``MATCH`` node. Under
  ``NOT``, in an ``OPTIONAL MATCH`` or computed, a comparison fixes nothing.
- *triples* ``(source class, relations, target class)`` of every relationship,
  direction normalized to "out" (an undirected relationship keeps ``both`` and
  matches either orientation). *Required* triples are those of a non-optional
  ``MATCH`` pattern whose step has a minimum length of at least 1; all others
  (optional, ``*0..n``, patterns used as predicates) only count as *possible*.
  A pattern under ``NOT`` is no triple at all: it filters rows out, and a
  function built on it answers the opposite question.

A signature also records what the final ``RETURN`` projects: the property keys
of its items (``*`` for a whole entity) and whether it aggregates.

A class is the label of the node (the first label group; its alternatives), else
the label of the same variable elsewhere, else what the resolver knows about the
variable; ``None`` is any class.

A function covers a query when (1) a query anchor class is equal to or a
subclass of the class of a function anchor bound to a ``ref`` parameter, (2)
every required query triple matches a triple of the function, (3) every
required triple of the function matches a required triple of the query (a
relationship only the function requires drops the rows without it: the
function answers a narrower question), and (4) the function returns what the
query returns: every property key the query projects is projected by the
function (but the ``id`` or ``alias`` of an anchor: the caller gave it, and a
run per value knows which value its rows belong to), and a query that
aggregates meets a function that aggregates (the function's rows cannot be
regrouped client-side). Matching is subclass-aware
(a query node of a subclass fits a function node of its superclass, not the
other way round); a wildcard fits anything; relation types of the query must
be among those of the function. A query without anchor or without any
required triple is never covered, a deprecated function never covers.

``list_fit`` is false exactly when the query anchored a list (``IN`` with a list
or a list-valued parameter) and the function parameter takes a single value:
the agent has to run the function once per value.

Pure: signatures need only the resolved query and the model. ``SignatureCache``
keeps function signatures by function hash and model version.

Lint mode (``recipe_covers``) asks the same question of a query recipe written into a graph
skill, for the maintainer who sees the function and decides, not for an agent at run time.
It is looser where a person judges (an anchor on any key counts, also a name; a function may
need no triple) and stricter where a hint would mislead (every required parameter must be
supplied, and every filter the recipe applies must be met by the function). The live rule
above is untouched. A function covers a recipe when it is not deprecated and:

1. every required parameter (no default) is supplied: a ``ref`` by an anchor of the recipe
   (``id``, ``alias``, a unique field or a label, compared with text or a parameter: a flag,
   a number or a status fixes no entity) whose
   classes fit the class of the function's anchor for it, a ``boolean``
   always, any other parameter when the recipe compares data with a key the way the function
   compares the parameter with it (``=`` and ``IN`` alike, the other operators each their
   own);
2. the triples agree both ways (every required triple of the recipe matches a triple of the
   function, every required triple of the function matches one of the recipe); when neither
   side has a required triple, every class of a node of the recipe fits a class of a node of
   the function, and a function with required triples never covers a recipe without;
3. it returns what the recipe returns (a function's ``*`` counts as every key), aggregates
   when the recipe does (in a ``WITH`` or the ``RETURN``) and is a bare total (a ``RETURN`` of
   aggregates only) exactly when the recipe is one: a grouped ``collect`` or ``count`` keeps a
   row per entity. An aggregate is the same only over the same thing: ``count(x)`` over another
   class or relation, or with and without ``DISTINCT``, is another column;
4. every filter key of the recipe (property keys in the ``WHERE`` of a non-optional ``MATCH``
   or ``WITH``, inline map keys of a non-optional ``MATCH`` node) that rule 1 did not consume
   is projected by the function or used in its own ``WHERE`` or maps;
5. the function filters no more than the recipe: every filter key of the function that no
   parameter of rule 1 consumed is a filter key of the recipe too and its constants are the
   recipe's (the same operator and values, or an ``=`` or ``IN`` whose values include the
   recipe's); a function with a ``LIMIT`` or ``SKIP`` needs a recipe with one; every pattern the
   function negates (``NOT (a)-[:r]->(b)``) is negated by the recipe too (else the function
   would drop rows the recipe keeps). A filter on an aggregate of a ``WITH`` (``WITH p,
   count(q) AS n WHERE n > 1``) is a filter key and a constant like any other, named by what
   the alias stands for. A filter that only a boolean switch of the function applies (``WHERE
   $include_ended = true OR c.status <> 'ended'``) is met by setting the switch: to lift it when
   the recipe does not filter that field, to apply it when the recipe filters the field the same
   way (``status <> 'ended'``); a recipe that filters it another way is not covered. The call
   names the switch value when it is not the default. And every
   pattern the recipe negates is negated by the function too, or at least walked by it, so that
   its rows show whether the relationship is there.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kinetic.compile import REF_FIELDS, resolve_stored
from graph_cli.kinetic.entries import FunctionEntry
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.ontology_layer.vocabulary import LABEL_ROLES
from graph_cli.query.cypher.ast import (
    Binary,
    Clause,
    Const,
    CountStar,
    Expr,
    FunctionCall,
    ListExpr,
    Match,
    NodePattern,
    Parameter,
    PatternPart,
    PatternPredicate,
    Property,
    Query,
    Return,
    Unary,
    Variable,
    With,
)
from graph_cli.query.cypher.compare import Comparison, comparisons
from graph_cli.query.cypher.resolve import AGGREGATES, ResolvedQuery
from graph_cli.query.cypher.walk import clause_expressions, clause_patterns, subexpressions

# ``None``: any class.
Classes = frozenset[str] | None
CACHE_LIMIT: Final = 1024


@dataclass(frozen=True, slots=True)
class Triple:
    """One relationship step; ``both``: the relationship was undirected."""

    source: Classes
    relations: frozenset[str] | None  # None: any relation
    target: Classes
    both: bool = False


@dataclass(frozen=True, slots=True)
class QueryAnchor:
    classes: frozenset[str]
    is_list: bool


@dataclass(frozen=True, slots=True)
class Output:
    """What the final ``RETURN`` projects: property keys (``*``: a whole entity), aggregation."""

    keys: frozenset[str]
    aggregates: bool


NO_OUTPUT: Final = Output(frozenset(), False)


@dataclass(frozen=True, slots=True)
class QuerySignature:
    anchors: tuple[QueryAnchor, ...]
    required: frozenset[Triple]
    triples: frozenset[Triple]  # required and possible
    output: Output = NO_OUTPUT


@dataclass(frozen=True, slots=True)
class FunctionAnchor:
    classes: Classes
    parameter: str
    is_ref: bool
    many: bool
    of_class: str | None


@dataclass(frozen=True, slots=True)
class FunctionSignature:
    anchors: tuple[FunctionAnchor, ...]
    required: frozenset[Triple]
    triples: frozenset[Triple]
    output: Output = NO_OUTPUT


EMPTY_FUNCTION: Final = FunctionSignature((), frozenset(), frozenset())


@dataclass(frozen=True, slots=True)
class Cover:
    """A function that covers a query, with what a hint needs."""

    function: str
    parameter: str  # the parameter the query's anchor belongs to
    of_class: str | None  # the class that parameter takes
    many: bool
    list_fit: bool
    share: float  # of the function's triples, the part the query uses
    featured: bool
    summary: str


# Anchors and triples shared by both sides


@dataclass(frozen=True, slots=True)
class _Anchor:
    classes: Classes
    value: Const | ListExpr | Parameter
    variable: str | None
    key: str = ""


def _node_classes(
    resolved: ResolvedQuery,
) -> Mapping[str, Classes]:
    """The classes of every named node variable: its first label group, else the resolver's."""
    found: dict[str, Classes] = {}
    for clause in resolved.query.clauses:
        for part in clause_patterns(clause):
            for node in part.nodes:
                if node.variable is not None and node.labels:
                    found.setdefault(node.variable, frozenset(node.labels[0]))
    for scope in resolved.introduced:
        for name, info in scope.items():
            if name not in found and info.kind == "node" and info.types is not None:
                found[name] = frozenset(info.types)
    return found


def _classes(node: NodePattern, known: Mapping[str, Classes]) -> Classes:
    if node.labels:
        return frozenset(node.labels[0])
    return known.get(node.variable) if node.variable is not None else None


def _conjuncts(e: Expr) -> Iterator[Expr]:
    if isinstance(e, Binary) and e.op == "and":
        yield from _conjuncts(e.left)
        yield from _conjuncts(e.right)
    else:
        yield e


def _disjuncts(e: Expr) -> Iterator[Expr]:
    if isinstance(e, Binary) and e.op == "or":
        yield from _disjuncts(e.left)
        yield from _disjuncts(e.right)
    else:
        yield e


def _data(e: Expr, *, allow_list: bool) -> Const | ListExpr | Parameter | None:
    if isinstance(e, Const) and e.value is not None:
        return e
    if isinstance(e, Parameter):
        return e
    if allow_list and isinstance(e, ListExpr) and all(isinstance(i, Const) for i in e.items):
        return e
    return None


def _equality(e: Expr) -> tuple[tuple[str, str], Const | ListExpr | Parameter] | None:
    """``((variable, key), data)`` of ``v.key = data``, ``data = v.key`` or ``v.key IN data``."""
    if not isinstance(e, Binary) or e.op not in ("=", "in"):
        return None
    sides = [(e.left, e.right)] if e.op == "in" else [(e.left, e.right), (e.right, e.left)]
    for left, right in sides:
        if isinstance(left, Property) and isinstance(left.subject, Variable):
            data = _data(right, allow_list=e.op == "in")
            if data is not None:
                return (left.subject.name, left.key), data
    return None


def _where_anchors(
    where: Expr, known: Mapping[str, Classes], only: frozenset[str] | None
) -> Iterator[_Anchor]:
    for conjunct in _conjuncts(where):
        found = _equality(conjunct)
        if found is not None:
            (variable, key), data = found
            if only is None or key in only:
                yield _Anchor(known.get(variable), data, variable, key)
            continue
        branches = [_equality(d) for d in _disjuncts(conjunct)]
        if len(branches) > 1 and all(b is not None for b in branches):
            same = {b[0] for b in branches if b is not None}
            if len(same) == 1:  # ``v.k = a OR v.k = b``: an ``IN``
                ((variable, key), _) = next(b for b in branches if b is not None)
                if only is None or key in only:
                    yield _Anchor(known.get(variable), ListExpr(()), variable, key)


def _anchors(
    query: Query, known: Mapping[str, Classes], only: frozenset[str] | None
) -> Iterator[_Anchor]:
    """``only``: the property keys that count as an anchor (None: any key)."""
    for clause in query.clauses:
        if isinstance(clause, Match) and not clause.optional:
            for part in clause.patterns:
                for node in part.nodes:
                    if node.properties is None:
                        continue
                    for key, value in node.properties.entries:
                        data = _data(value, allow_list=False)
                        if data is not None and (only is None or key in only):
                            yield _Anchor(_classes(node, known), data, node.variable, key)
            if clause.where is not None:
                yield from _where_anchors(clause.where, known, only)
        elif isinstance(clause, With) and clause.where is not None:
            yield from _where_anchors(clause.where, known, only)


def _triples(query: Query, known: Mapping[str, Classes]) -> tuple[frozenset[Triple], ...]:
    """``(required, all)``."""
    required: set[Triple] = set()
    every: set[Triple] = set()
    for clause in query.clauses:
        negated = _negated(clause)
        for part in clause_patterns(clause):
            if id(part) in negated:
                continue
            is_required = _is_required(clause, part)
            for i, rel in enumerate(part.rels):
                left = _classes(part.nodes[i], known)
                right = _classes(part.nodes[i + 1], known)
                source, target = (right, left) if rel.direction == "in" else (left, right)
                triple = Triple(
                    source,
                    frozenset(rel.types) if rel.types else None,
                    target,
                    both=rel.direction == "both",
                )
                every.add(triple)
                if is_required and (rel.length is None or rel.length[0] >= 1):
                    required.add(triple)
    return frozenset(required), frozenset(every)


def _negated(clause: Clause) -> frozenset[int]:
    """The patterns used as predicates under ``NOT``: an anti-join, not a step of the answer."""
    return frozenset(
        id(inner.pattern)
        for e in clause_expressions(clause)
        if isinstance(e, Unary) and e.op == "not"
        for inner in subexpressions(e.operand)
        if isinstance(inner, PatternPredicate)
    )


def _output(query: Query, anchored: frozenset[str] = frozenset()) -> Output:
    """``anchored``: variables whose ``id`` and ``alias`` are left out: the caller gave them,
    and a run per value knows which value its rows belong to."""
    last = query.clauses[-1]
    if not isinstance(last, Return):
        return NO_OUTPUT
    keys: set[str] = {"*"} if last.projection.star else set()
    aggregates = False
    for item in last.projection.items:
        if isinstance(item.expr, Variable):
            keys.add("*")
        for e in subexpressions(item.expr):
            if isinstance(e, Property):
                subject = e.subject
                given = isinstance(subject, Variable) and subject.name in anchored
                if not (given and e.key in REF_FIELDS):
                    keys.add(e.key)
            elif isinstance(e, CountStar) or (isinstance(e, FunctionCall) and e.name in AGGREGATES):
                aggregates = True
    return Output(frozenset(keys), aggregates)


def _is_required(clause: Clause, part: PatternPart) -> bool:
    """The part is a pattern of a non-optional ``MATCH`` (not a predicate, not optional)."""
    return (
        isinstance(clause, Match)
        and not clause.optional
        and any(part is own for own in clause.patterns)
    )


# Signatures


def query_signature(resolved: ResolvedQuery) -> QuerySignature:
    """Anchors and triples of an ad-hoc query; ``--param`` values count as literals."""
    known = _node_classes(resolved)
    found = [a for a in _anchors(resolved.query, known, REF_FIELDS) if a.classes is not None]
    anchors = tuple(
        QueryAnchor(a.classes, _is_list(a.value, resolved.parameters))
        for a in found
        if a.classes is not None  # narrowed for the type checker; ``found`` has no None
    )
    anchored = frozenset(a.variable for a in found if a.variable is not None)
    required, every = _triples(resolved.query, known)
    return QuerySignature(anchors, required, every, _output(resolved.query, anchored))


def _is_list(data: Const | ListExpr | Parameter, parameters: Mapping[str, object]) -> bool:
    if isinstance(data, ListExpr):
        return True
    if isinstance(data, Parameter):
        return isinstance(parameters.get(data.name), list)
    return False


def function_signature(entry: FunctionEntry, model: CompiledModel) -> FunctionSignature:
    """Anchors and triples of a published function, from its stored query text.

    Raises the resolver's error when the stored text no longer resolves against
    ``model`` (a stale catalog).
    """
    return _signature_of(entry, resolve_stored(entry.query, entry.defn, model))


def _signature_of(entry: FunctionEntry, resolved: ResolvedQuery) -> FunctionSignature:
    known = _node_classes(resolved)
    declared = {p.name: p for p in entry.defn.params}
    anchors = []
    for a in _anchors(resolved.query, known, None):
        if not isinstance(a.value, Parameter) or a.value.name not in declared:
            continue
        param = declared[a.value.name]
        is_ref = param.type == "ref"
        classes = a.classes
        if classes is None and is_ref and param.of_class is not None:
            classes = frozenset({param.of_class})
        anchors.append(FunctionAnchor(classes, param.name, is_ref, param.many, param.of_class))
    required, every = _triples(resolved.query, known)
    return FunctionSignature(tuple(anchors), required, every, _output(resolved.query))


class SignatureCache:
    """Function signatures by ``(function hash, model id and version)``.

    A function hash covers its declaration and normalized text; a released
    ontology version is immutable, so the pair identifies the signature. A
    function that no longer resolves caches as an empty signature (it covers
    nothing). Bounded; the oldest entries go first.
    """

    def __init__(self, limit: int = CACHE_LIMIT) -> None:
        self._limit = limit
        self._found: dict[tuple[str, str, str], FunctionSignature] = {}

    def signature(self, entry: FunctionEntry, model: CompiledModel) -> FunctionSignature:
        key = (entry.hash, model.bundle_id, model.bundle_version)
        found = self._found.get(key)
        if found is None:
            try:
                found = function_signature(entry, model)
            except GraphCliError:
                found = EMPTY_FUNCTION
            if len(self._found) >= self._limit:
                del self._found[next(iter(self._found))]
            self._found[key] = found
        return found

    def pairs(
        self, entries: Sequence[FunctionEntry], model: CompiledModel
    ) -> tuple[tuple[FunctionEntry, FunctionSignature], ...]:
        return tuple((e, self.signature(e, model)) for e in entries)


# Matching


def _fits(query: Classes, function: Classes, model: CompiledModel) -> bool:
    """Every class the query node can have is the function node's class or a subclass of it."""
    if query is None or function is None:
        return True
    return all(model.is_a_any(c, tuple(function)) for c in query)


def _oriented(q: Triple, f: Triple, model: CompiledModel) -> bool:
    return _fits(q.source, f.source, model) and _fits(q.target, f.target, model)


def _matches(q: Triple, f: Triple, model: CompiledModel) -> bool:
    if q.relations is not None and f.relations is not None and not q.relations <= f.relations:
        return False
    if _oriented(q, f, model):
        return True
    if not (q.both or f.both):
        return False
    return _fits(q.source, f.target, model) and _fits(q.target, f.source, model)


def _returns_enough(query: Output, function: Output) -> bool:
    return query.keys <= function.keys and (function.aggregates or not query.aggregates)


def _anchor_pair(
    query: QuerySignature, function: FunctionSignature, model: CompiledModel
) -> tuple[FunctionAnchor, QueryAnchor] | None:
    """The first shared anchor, preferring one where the list fits."""
    found: list[tuple[FunctionAnchor, QueryAnchor]] = []
    for f in function.anchors:
        if not f.is_ref or f.classes is None:
            continue
        for q in query.anchors:
            if _fits(q.classes, f.classes, model):
                found.append((f, q))
    found.sort(key=lambda pair: pair[1].is_list and not pair[0].many)
    return found[0] if found else None


def covers(
    query: QuerySignature,
    functions: Sequence[tuple[FunctionEntry, FunctionSignature]],
    model: CompiledModel,
) -> tuple[Cover, ...]:
    """The functions that cover the query, best first.

    Ranked by the share of the function's triples the query uses (descending),
    then ``featured`` functions, then name.
    """
    if not query.anchors or not query.required:
        return ()
    found: list[Cover] = []
    for entry, signature in functions:
        if entry.deprecated or not signature.required:
            continue
        if not _returns_enough(query.output, signature.output):
            continue
        pair = _anchor_pair(query, signature, model)
        if pair is None:
            continue
        if not all(any(_matches(q, f, model) for f in signature.triples) for q in query.required):
            continue
        # a relationship the function requires and the query does not filters its rows
        if not all(any(_matches(q, f, model) for q in query.required) for f in signature.required):
            continue
        used = sum(
            1 for f in signature.triples if any(_matches(q, f, model) for q in query.required)
        )
        function_anchor, query_anchor = pair
        found.append(
            Cover(
                function=entry.name,
                parameter=function_anchor.parameter,
                of_class=function_anchor.of_class,
                many=function_anchor.many,
                list_fit=function_anchor.many or not query_anchor.is_list,
                share=used / len(signature.triples),
                featured=entry.defn.featured,
                summary=entry.defn.summary,
            )
        )
    found.sort(key=lambda c: (-c.share, not c.featured, c.function))
    return tuple(found)


# Lint mode: query recipes of a graph skill against the functions of the graph

RecipeData = Const | ListExpr | Parameter
# ``(key, operator, values)`` of a field compared with constants; values as their ``repr``.
Constant = tuple[str, str, frozenset[str]]


@dataclass(frozen=True, slots=True)
class RecipeAnchor:
    classes: Classes
    key: str  # the property the data is compared with, any key
    value: RecipeData


@dataclass(frozen=True, slots=True)
class RecipeSignature:
    """What a query recipe asks (lint mode): anchors on any key, triples, output, the data it
    compares with which key, its filter keys and the classes of its non-optional nodes."""

    anchors: tuple[RecipeAnchor, ...]
    required: frozenset[Triple]
    triples: frozenset[Triple]
    keys: frozenset[str]  # what the ``RETURN`` projects, see ``_lint_keys``
    compared: Mapping[tuple[str, str], RecipeData]  # (key, operator) -> data of the first one
    filters: frozenset[str]
    classes: frozenset[frozenset[str]]
    negated: frozenset[frozenset[str]]  # the relations of each pattern under ``NOT``
    aggregates: bool  # an aggregate in any ``WITH`` or in the ``RETURN``
    totals: bool  # the ``RETURN`` holds aggregates only: no row per entity
    limited: bool  # a ``SKIP`` or ``LIMIT`` somewhere
    constants: frozenset[Constant]  # the constants compared with a field in a filter


@dataclass(frozen=True, slots=True)
class FunctionLintSignature:
    """A function as lint mode sees it: its signature plus the keys and operators it compares
    each parameter with, its own filter keys, the classes of its non-optional nodes and the
    patterns it negates."""

    base: FunctionSignature
    keys: frozenset[str]
    compared: Mapping[str, frozenset[tuple[str, str]]]  # parameter -> (key, operator)
    filters: frozenset[str]
    classes: frozenset[frozenset[str]]
    negated: frozenset[frozenset[str]]
    aggregates: bool
    totals: bool
    limited: bool
    constants: frozenset[Constant]
    switches: Mapping[str, tuple[str, bool]]  # filter key -> (boolean parameter, value lifting it)


@dataclass(frozen=True, slots=True)
class RecipeCover:
    """A function that answers a recipe: which parameters the recipe supplies (``ref_params``)
    and with what data (``value_params``: parameter and the text the recipe compares with, or
    ``None`` when it is not a constant), for the call a finding shows."""

    function: str
    featured: bool
    share: float
    ref_params: tuple[str, ...]
    value_params: tuple[tuple[str, str | None], ...]


def _in_scope(query: Query) -> Iterator[Match | With]:
    """The clauses whose conditions filter the rows: non-optional ``MATCH`` and ``WITH``."""
    for clause in query.clauses:
        if (isinstance(clause, Match) and not clause.optional) or isinstance(clause, With):
            yield clause


def _filter_keys(query: Query) -> frozenset[str]:
    keys: set[str] = set()
    for clause in _in_scope(query):
        if isinstance(clause, Match):
            for part in clause.patterns:
                for node in part.nodes:
                    if node.properties is not None:
                        keys.update(key for key, _ in node.properties.entries)
        if clause.where is not None:
            keys.update(e.key for e in subexpressions(clause.where) if isinstance(e, Property))
    return frozenset(keys)


def _scoped_comparisons(query: Query, *, literals: bool) -> Iterator[Comparison]:
    for clause in _in_scope(query):
        yield from comparisons(Query((clause,)), literals=literals)


def _operator(c: Comparison) -> str:
    """``=`` for ``=`` and ``IN`` either way (a list of one is the value), else the operator."""
    return "=" if c.op in ("=", "in") else c.op


def _negations(query: Query) -> frozenset[frozenset[str]]:
    """The relation types of each pattern under ``NOT``."""
    found: set[frozenset[str]] = set()
    for clause in query.clauses:
        negated = _negated(clause)
        for part in clause_patterns(clause):
            if id(part) in negated:
                found.add(frozenset(t for rel in part.rels for t in rel.types))
    return frozenset(found)


def _match_classes(query: Query, known: Mapping[str, Classes]) -> frozenset[frozenset[str]]:
    found: set[frozenset[str]] = set()
    for clause in query.clauses:
        if isinstance(clause, Match) and not clause.optional:
            for part in clause.patterns:
                for node in part.nodes:
                    classes = _classes(node, known)
                    if classes is not None:
                        found.add(classes)
    return frozenset(found)


def _identifies(key: str, classes: frozenset[str], model: CompiledModel) -> bool:
    """Whether a value of this field can stand for one entity of the classes: ``id``, ``alias``,
    a unique field or a label (``label``, ``alt_label``, ``hidden_label``). A status, an enum or
    any other field narrows the rows and names nobody."""
    if key in REF_FIELDS:
        return True
    for curie in classes:
        found = model.classes[curie].attributes.get(key) if curie in model.classes else None
        if (
            found is not None
            and found.kind in ("base", "datatype")
            and (found.unique or found.role in LABEL_ROLES)
        ):
            return True
    return False


def _names_entity(value: RecipeData) -> bool:
    """Whether the data can stand for an entity: text or a list of texts, or a parameter. A flag,
    a number or a null compared with a field narrows the rows but fixes no entity."""
    if isinstance(value, Const):
        return isinstance(value.value, str)
    if isinstance(value, ListExpr):
        return all(isinstance(i, Const) and isinstance(i.value, str) for i in value.items)
    return True


def recipe_signature(resolved: ResolvedQuery) -> RecipeSignature:
    """The lint-mode signature of a resolved recipe (``--param`` values are data)."""
    known = _node_classes(resolved)
    what = _what(resolved)
    having_keys, having_constants = _having(resolved, what)
    found = list(_anchors(resolved.query, known, None))
    given = frozenset(a.variable for a in found if a.variable is not None and a.key in REF_FIELDS)
    required, every = _triples(resolved.query, known)
    compared: dict[tuple[str, str], RecipeData] = {}
    for c in _scoped_comparisons(resolved.query, literals=True):
        compared.setdefault((c.field, _operator(c)), c.value)
    return RecipeSignature(
        tuple(RecipeAnchor(a.classes, a.key, a.value) for a in found if _names_entity(a.value)),
        required,
        every,
        _lint_keys(resolved, given, what),
        compared,
        _filter_keys(resolved.query) | having_keys,
        _match_classes(resolved.query, known),
        _negations(resolved.query),
        *_aggregation(resolved.query),
        _limited(resolved.query),
        _constants(resolved.query) | having_constants,
    )


def function_lint_signature(entry: FunctionEntry, model: CompiledModel) -> FunctionLintSignature:
    """The lint-mode signature of a published function; raises the resolver's error when the
    stored text no longer resolves against ``model``."""
    resolved = resolve_stored(entry.query, entry.defn, model)
    known = _node_classes(resolved)
    what = _what(resolved)
    having_keys, having_constants = _having(resolved, what)
    keys: dict[str, set[tuple[str, str]]] = {}
    for c in _scoped_comparisons(resolved.query, literals=False):
        if isinstance(c.value, Parameter):
            keys.setdefault(c.value.name, set()).add((c.field, _operator(c)))
    return FunctionLintSignature(
        _signature_of(entry, resolved),
        _lint_keys(resolved, frozenset(), what),
        {name: frozenset(found) for name, found in keys.items()},
        _filter_keys(resolved.query) | having_keys,
        _match_classes(resolved.query, known),
        _negations(resolved.query),
        *_aggregation(resolved.query),
        _limited(resolved.query),
        _constants(resolved.query) | having_constants,
        _switches(
            resolved.query,
            frozenset(p.name for p in entry.defn.params if p.type == "boolean"),
        ),
    )


def _what(resolved: ResolvedQuery) -> Mapping[str, str]:
    """What a variable stands for in an aggregate: the classes of a node, the relation types of a
    relationship (``*`` when they are not known), so that ``count(q)`` over one class or relation
    is not ``count(q)`` over another."""
    found: dict[str, str] = {}
    for variable, classes in _node_classes(resolved).items():
        found[variable] = "|".join(sorted(classes)) if classes else "*"
    for clause in resolved.query.clauses:
        for part in clause_patterns(clause):
            for rel in part.rels:
                if rel.variable is not None:
                    found[rel.variable] = "|".join(sorted(rel.types)) or "*"
    return found


def _tokens(
    e: Expr,
    defs: Mapping[str, frozenset[str]],
    anchored: frozenset[str],
    what: Mapping[str, str],
) -> set[str]:
    """The keys an expression projects: a property key, ``min:held_on`` for a property inside an
    aggregate (a ``max`` of a key is not the key), ``count:@<classes or relations>`` for an
    aggregate of a node or relationship (``count_distinct`` with ``DISTINCT``, ``count:*`` for
    ``count(*)``), and what the alias of an earlier ``WITH`` item stands for. ``id`` and ``alias``
    of an ``anchored`` variable are left out (the caller gave them)."""
    found: set[str] = set()
    inside: set[int] = set()
    for x in subexpressions(e):
        if isinstance(x, CountStar):
            found.add("count:*")
        elif isinstance(x, FunctionCall) and x.name in AGGREGATES:
            name = f"{x.name}_distinct" if x.distinct else x.name
            for arg in x.args:
                for y in subexpressions(arg):
                    inside.add(id(y))
                    if isinstance(y, Property):
                        found.add(f"{name}:{y.key}")
                    elif isinstance(y, Variable):
                        stands = defs.get(y.name)
                        found.add(
                            f"{name}:@{'+'.join(sorted(stands))}"
                            if stands
                            else f"{name}:@{what.get(y.name, '*')}"
                        )
    for x in subexpressions(e):
        if id(x) in inside:
            continue
        if isinstance(x, Property):
            given = isinstance(x.subject, Variable) and x.subject.name in anchored
            if not (given and x.key in REF_FIELDS):
                found.add(x.key)
        elif isinstance(x, Variable):
            found |= defs.get(x.name, frozenset())
    return found


def _lint_keys(
    resolved: ResolvedQuery, anchored: frozenset[str], what: Mapping[str, str]
) -> frozenset[str]:
    """What the final ``RETURN`` projects, key by key (see ``_tokens``); ``*`` for a whole node or
    relationship (a variable that holds a list or a number projects no entity)."""
    query = resolved.query
    kinds = {name: info.kind for scope in resolved.introduced for name, info in scope.items()}
    defs: dict[str, frozenset[str]] = {}
    for clause in query.clauses[:-1]:
        if isinstance(clause, With):
            for item in clause.projection.items:
                if not (isinstance(item.expr, Variable) and item.expr.name == item.name):
                    defs[item.name] = frozenset(_tokens(item.expr, defs, anchored, what))
    last = query.clauses[-1]
    if not isinstance(last, Return):
        return frozenset()
    keys: set[str] = {"*"} if last.projection.star else set()
    for item in last.projection.items:
        if isinstance(item.expr, Variable) and kinds.get(item.expr.name) in ("node", "rel"):
            keys.add("*")
        else:
            keys |= _tokens(item.expr, defs, anchored, what)
    return frozenset(keys)


_FLIPPED: Final = {"=": "=", "<>": "<>", "<": ">", "<=": ">=", ">": "<", ">=": "<="}


def _having(
    resolved: ResolvedQuery, what: Mapping[str, str]
) -> tuple[frozenset[str], frozenset[Constant]]:
    """The filters a ``WITH`` puts on an aggregate alias (``WITH p, count(q) AS n WHERE n > 1``):
    the filter key is what the alias stands for (``count:@<class>``) and the constants are its
    comparisons with literals, so that both sides compare the same aggregate the same way."""
    defs: dict[str, frozenset[str]] = {}
    aggregated: dict[str, frozenset[str]] = {}
    keys: set[str] = set()
    constants: set[Constant] = set()
    for clause in resolved.query.clauses:
        if not isinstance(clause, With):
            continue
        for item in clause.projection.items:
            if isinstance(item.expr, Variable) and item.expr.name == item.name:
                continue
            tokens = frozenset(_tokens(item.expr, defs, frozenset(), what))
            defs[item.name] = tokens
            if _has_aggregate(item.expr) or any(
                isinstance(x, Variable) and x.name in aggregated for x in subexpressions(item.expr)
            ):
                aggregated[item.name] = tokens
        if clause.where is None:
            continue
        for x in subexpressions(clause.where):
            if isinstance(x, Variable) and x.name in aggregated:
                keys.add("+".join(sorted(aggregated[x.name])))
            if isinstance(x, Binary) and x.op in _FLIPPED:
                for side, other, op in (
                    (x.left, x.right, x.op),
                    (x.right, x.left, _FLIPPED[x.op]),
                ):
                    if (
                        isinstance(side, Variable)
                        and side.name in aggregated
                        and isinstance(other, Const)
                    ):
                        key = "+".join(sorted(aggregated[side.name]))
                        constants.add(
                            (key, "=" if op == "=" else op, frozenset({repr(other.value)}))
                        )
    return frozenset(keys), frozenset(constants)


def _limited(query: Query) -> bool:
    return any(
        isinstance(c, With | Return) and (c.projection.skip or c.projection.limit) is not None
        for c in query.clauses
    )


def _constants(query: Query) -> frozenset[Constant]:
    """The fields a filter compares with constants, with the operator and the values."""
    found: set[Constant] = set()
    for c in _scoped_comparisons(query, literals=True):
        if isinstance(c.value, Const):
            values = frozenset({repr(c.value.value)})
        elif isinstance(c.value, ListExpr):
            values = frozenset(repr(i.value) for i in c.value.items if isinstance(i, Const))
        else:
            continue
        found.add((c.field, _operator(c), values))
    return frozenset(found)


def _constant_met(wanted: Constant, recipe: frozenset[Constant]) -> bool:
    """The recipe filters the field the same way, or asks for values among those the function
    keeps (an ``=`` or ``IN`` of the function is a superset of the recipe's)."""
    key, operator, values = wanted
    return any(
        k == key and o == operator and (v == values or (operator == "=" and v <= values))
        for k, o, v in recipe
    )


def _has_aggregate(e: Expr) -> bool:
    return any(
        isinstance(x, CountStar) or (isinstance(x, FunctionCall) and x.name in AGGREGATES)
        for x in subexpressions(e)
    )


def _aggregation(query: Query) -> tuple[bool, bool]:
    """``(aggregates, totals)``: an aggregate in any ``WITH`` or the ``RETURN``; the ``RETURN``
    holds aggregates only. A grouped ``collect`` or ``count`` keeps a row per entity."""
    anywhere = False
    for clause in query.clauses:
        if isinstance(clause, With | Return):
            anywhere = anywhere or any(_has_aggregate(i.expr) for i in clause.projection.items)
    last = query.clauses[-1]
    items = last.projection.items if isinstance(last, Return) else ()
    return anywhere, bool(items) and all(_has_aggregate(i.expr) for i in items)


def _lifting(disjunct: Expr, booleans: frozenset[str]) -> tuple[str, bool] | None:
    """``($p, value)`` when the disjunct is ``$p = true``, ``$p = false``, ``$p`` or ``NOT $p``:
    the value of the boolean parameter that makes it true."""
    if isinstance(disjunct, Parameter) and disjunct.name in booleans:
        return disjunct.name, True
    if (
        isinstance(disjunct, Unary)
        and disjunct.op == "not"
        and isinstance(disjunct.operand, Parameter)
        and disjunct.operand.name in booleans
    ):
        return disjunct.operand.name, False
    if isinstance(disjunct, Binary) and disjunct.op == "=":
        for side, other in ((disjunct.left, disjunct.right), (disjunct.right, disjunct.left)):
            if (
                isinstance(side, Parameter)
                and side.name in booleans
                and isinstance(other, Const)
                and isinstance(other.value, bool)
            ):
                return side.name, other.value
    return None


def _switches(query: Query, booleans: frozenset[str]) -> Mapping[str, tuple[str, bool]]:
    """The filter keys that a boolean parameter lifts: keys that occur only in conjuncts of a
    ``WHERE`` of the shape ``$p = true OR ...`` (a switch such as ``include_ended``), each with
    the parameter and the value that makes the conjunct true, whatever the keys say."""
    switched: dict[str, tuple[str, bool]] = {}
    fixed: set[str] = set()
    for clause in _in_scope(query):
        if isinstance(clause, Match):
            for part in clause.patterns:
                for node in part.nodes:
                    if node.properties is not None:
                        fixed.update(key for key, _ in node.properties.entries)
        if clause.where is None:
            continue
        for conjunct in _conjuncts(clause.where):
            keys = {e.key for e in subexpressions(conjunct) if isinstance(e, Property)}
            lifts = [_lifting(d, booleans) for d in _disjuncts(conjunct)]
            lift = next((x for x in lifts if x is not None), None)
            if lift is None:
                fixed |= keys
            else:
                switched.update({key: lift for key in keys if key not in switched})
    return {key: lift for key, lift in switched.items() if key not in fixed}


def _lint_output(recipe: RecipeSignature, function: FunctionLintSignature) -> bool:
    """The function returns every key of the recipe (``*`` is every key), aggregates when the
    recipe does, and is no bare total (a count answers no listing) unless the recipe is one."""
    if not ("*" in function.keys or recipe.keys <= function.keys):
        return False
    return (function.aggregates or not recipe.aggregates) and recipe.totals == function.totals


@dataclass(frozen=True, slots=True)
class _Supplied:
    refs: tuple[str, ...]
    values: tuple[tuple[str, str | None], ...]
    consumed: frozenset[str]  # keys of the recipe the parameters took
    taken: frozenset[str]  # keys of the function the parameters filter on


def _supply(
    recipe: RecipeSignature,
    entry: FunctionEntry,
    function: FunctionLintSignature,
    model: CompiledModel,
) -> _Supplied | None:
    """What the recipe supplies for the required parameters of the function; ``None`` when one
    stays open."""
    refs: list[str] = []
    values: list[tuple[str, str | None]] = []
    consumed: set[str] = set()
    filtered: set[str] = set()
    taken: set[int] = set()
    for param in entry.defn.params:
        if param.default is not None:
            continue
        filtered.update(key for key, _ in function.compared.get(param.name, ()))
        if param.type == "boolean":
            values.append((param.name, "<true|false>"))
            continue
        if param.type == "ref":
            slots = [a for a in function.base.anchors if a.parameter == param.name and a.is_ref]
            found = next(
                (
                    (i, a)
                    for a in slots
                    for i, anchor in enumerate(recipe.anchors)
                    if i not in taken
                    and anchor.classes is not None
                    and a.classes is not None
                    and _identifies(anchor.key, anchor.classes, model)
                    and _fits(anchor.classes, a.classes, model)
                ),
                None,
            )
            if found is None:
                return None
            taken.add(found[0])
            consumed.add(recipe.anchors[found[0]].key)
            refs.append(param.name)
        else:
            met = sorted(function.compared.get(param.name, frozenset()) & recipe.compared.keys())
            if not met:
                return None
            consumed.update(key for key, _ in met)
            values.append((param.name, _text_of(recipe.compared[met[0]])))
    return _Supplied(tuple(refs), tuple(values), frozenset(consumed), frozenset(filtered))


def _text_of(data: RecipeData) -> str | None:
    if isinstance(data, Const) and data.value is not None and not isinstance(data.value, bool):
        return str(data.value)
    return None


def _switch_values(
    entry: FunctionEntry, lifted: set[tuple[str, bool]]
) -> tuple[tuple[str, str | None], ...]:
    """The ``--param`` pairs of the switches a call must set, to lift a filter the recipe lacks or
    to apply one it has: the parameters whose value is not their default."""
    defaults = {p.name: p.default for p in entry.defn.params}
    return tuple(
        (name, "true" if value else "false")
        for name, value in sorted(lifted)
        if defaults.get(name) is not value
    )


def _call_values(
    supplied: tuple[tuple[str, str | None], ...], switches: tuple[tuple[str, str | None], ...]
) -> tuple[tuple[str, str | None], ...]:
    """The ``--param`` pairs of a call: what the recipe supplies, with the value of a switch
    replacing the placeholder of a required boolean that is also that switch."""
    set_to = dict(switches)
    shown = [(name, set_to.pop(name, value)) for name, value in supplied]
    return (*shown, *set_to.items())


def recipe_covers(
    recipe: RecipeSignature,
    functions: Sequence[tuple[FunctionEntry, FunctionLintSignature]],
    model: CompiledModel,
) -> tuple[RecipeCover, ...]:
    """The functions that answer the recipe (rules in the module docstring), best first: the
    share of the function's triples the recipe uses (descending), ``featured`` functions, then
    name."""
    found: list[RecipeCover] = []
    for entry, function in functions:
        if entry.deprecated:
            continue
        supplied = _supply(recipe, entry, function, model)
        if supplied is None or not _lint_output(recipe, function):
            continue
        base = function.base
        if recipe.required:
            if not all(any(_matches(q, f, model) for f in base.triples) for q in recipe.required):
                continue
            if not all(any(_matches(q, f, model) for q in recipe.required) for f in base.required):
                continue
            used = sum(
                1 for f in base.triples if any(_matches(q, f, model) for q in recipe.required)
            )
            share = used / len(base.triples)
        else:
            if base.required or not recipe.classes:
                continue
            if not all(any(_fits(c, f, model) for f in function.classes) for c in recipe.classes):
                continue
            share = 1.0
        met = function.keys | function.filters
        if not all("*" in met or key in met for key in recipe.filters - supplied.consumed):
            continue
        open_keys = function.filters - supplied.taken - recipe.filters
        if any(k not in function.switches for k in open_keys):
            continue
        lifted = {function.switches[k] for k in open_keys}
        applied: set[tuple[str, bool]] = set()
        refused = False
        for c in function.constants:
            if c[0] in supplied.taken:
                continue
            if c[0] not in function.switches:
                refused = refused or not _constant_met(c, recipe.constants)
            elif _constant_met(c, recipe.constants):
                name, lifting = function.switches[c[0]]
                applied.add((name, not lifting))  # the recipe filters like the function does
            elif c[0] in recipe.filters:
                refused = True  # the recipe filters that field another way: no switch fits
        if refused or {n for n, _ in lifted} & {n for n, _ in applied}:
            continue
        if function.limited and not recipe.limited:
            continue  # a top-n answers no question about all
        if not function.negated <= recipe.negated:
            continue
        walked = {t for triple in base.triples for t in triple.relations or ()}
        if not all(rels <= walked for rels in recipe.negated - function.negated):
            continue
        found.append(
            RecipeCover(
                entry.name,
                entry.defn.featured,
                share,
                supplied.refs,
                _call_values(supplied.values, _switch_values(entry, lifted | applied)),
            )
        )
    found.sort(key=lambda c: (-c.share, not c.featured, c.function))
    return tuple(found)
