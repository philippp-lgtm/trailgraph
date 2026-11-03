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
query returns: every property key the query projects is projected by
the function, and a query that aggregates meets a function that aggregates (the
function's rows cannot be regrouped client-side). Matching is subclass-aware
(a query node of a subclass fits a function node of its superclass, not the
other way round); a wildcard fits anything; relation types of the query must
be among those of the function. A query without anchor or without any
required triple is never covered, a deprecated function never covers.

``list_fit`` is false exactly when the query anchored a list (``IN`` with a list
or a list-valued parameter) and the function parameter takes a single value:
the agent has to run the function once per value.

Pure: signatures need only the resolved query and the model. ``SignatureCache``
keeps function signatures by function hash and model version.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kinetic.compile import REF_FIELDS, resolve_stored
from graph_cli.kinetic.entries import FunctionEntry
from graph_cli.ontology_layer.model import CompiledModel
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
                yield _Anchor(known.get(variable), data)
            continue
        branches = [_equality(d) for d in _disjuncts(conjunct)]
        if len(branches) > 1 and all(b is not None for b in branches):
            same = {b[0] for b in branches if b is not None}
            if len(same) == 1:  # ``v.k = a OR v.k = b``: an ``IN``
                ((variable, key), _) = next(b for b in branches if b is not None)
                if only is None or key in only:
                    yield _Anchor(known.get(variable), ListExpr(()))


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
                            yield _Anchor(_classes(node, known), data)
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


def _output(query: Query) -> Output:
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
    anchors = tuple(
        QueryAnchor(a.classes, _is_list(a.value, resolved.parameters))
        for a in _anchors(resolved.query, known, REF_FIELDS)
        if a.classes is not None
    )
    required, every = _triples(resolved.query, known)
    return QuerySignature(anchors, required, every, _output(resolved.query))


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
    resolved = resolve_stored(entry.query, entry.defn, model)
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
