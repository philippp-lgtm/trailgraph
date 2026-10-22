"""Pattern matching of a resolved query over the ``Snapshot`` port.

Nodes are current revisions with an allowed record status (``active``, or
every status with ``include_inactive``); an edge matches only when both
ends are such nodes. Within one ``MATCH`` a relationship is used at most
once per row (openCypher relationship uniqueness), also along a
variable-length path.

Each pattern starts at its most selective node: one bound by an earlier
clause, then one with an ``id`` or ``alias`` equality (inline or in the
``WHERE`` conjuncts), then the label with the fewest entities, then all
entities. From there it extends right and left along the chain. Rows come
in a deterministic order: scans by id, edges by the other end's id and
edge id.

Work is bounded: every candidate node and edge counts against the budget
(default 100 000); above it the query ends with ``query.too_large``.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, TermRef, parse_id
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.query_ast import Query as StoreQuery
from graph_cli.kernel.records import RECORD_STATUSES, Revision
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.query.cypher.ast import (
    Binary,
    Const,
    CountStar,
    Direction,
    Expr,
    FunctionCall,
    Match,
    NodePattern,
    Parameter,
    PatternPart,
    Pos,
    Projection,
    Property,
    RelPattern,
    Unwind,
    Variable,
    With,
)
from graph_cli.query.cypher.errors import invalid_value, too_large
from graph_cli.query.cypher.evaluate import Evaluator, Row
from graph_cli.query.cypher.resolve import ResolvedQuery, aggregates_in
from graph_cli.query.cypher.values import (
    MAX_INTEGER,
    MIN_INTEGER,
    GroupKey,
    Node,
    Number,
    Rel,
    Value,
    as_number,
    equals,
    group_key,
    kind,
    order_key,
)

DEFAULT_BUDGET: Final = 100_000
_LOOKUP_FIELDS: Final = ("id", "alias")


@dataclass(frozen=True, slots=True)
class _Partial:
    """A row being extended by one MATCH: its bindings and the relationships used."""

    row: dict[str, Value]
    used: frozenset[tuple[EntityId, object]]


class Executor:
    def __init__(
        self,
        resolved: ResolvedQuery,
        model: CompiledModel,
        snapshot: Snapshot,
        *,
        include_inactive: bool = False,
        budget: int = DEFAULT_BUDGET,
    ) -> None:
        self.resolved = resolved
        self.model = model
        self.snapshot = snapshot
        self.statuses = frozenset(RECORD_STATUSES) if include_inactive else frozenset({"active"})
        self.budget = budget
        self.spent = 0
        self.evaluator = Evaluator(model, resolved.parameters, self.exists)
        self.revisions: dict[EntityId, Revision | None] = {}
        self.scans: dict[tuple[str, ...] | None, tuple[Revision, ...]] = {}
        self.clause_pos = Pos(1, 1, 0)
        self.clause_name = "MATCH"

    # Budget

    def at(self, pos: Pos, clause: str) -> None:
        """The clause that spends from now on (named when the budget runs out)."""
        self.clause_pos = pos
        self.clause_name = clause

    def spend(self, amount: int = 1) -> None:
        self.spent += amount
        if self.spent > self.budget:
            raise too_large(self.clause_pos, self.clause_name, self.budget)

    # Nodes

    def node(self, entity: EntityId) -> Node | None:
        if entity not in self.revisions:
            self.revisions[entity] = self.snapshot.get(entity)
        revision = self.revisions[entity]
        if revision is None or revision.record_status not in self.statuses:
            return None
        return Node(revision)

    def scan(self, types: tuple[str, ...] | None) -> tuple[Revision, ...]:
        """Visible nodes of these classes (None: all), by id."""
        if types == ():
            return ()
        if types not in self.scans:
            query = StoreQuery(
                types=tuple(TermRef(t) for t in types or ()),
                record_status=self.statuses,
                limit=self.budget + 1,
            )
            found = tuple(self.snapshot.query(query).items)
            for revision in found:
                self.revisions[revision.id] = revision
            self.scans[types] = found
        return self.scans[types]

    def classes(self, pattern: NodePattern) -> tuple[str, ...] | None:
        if not pattern.labels:
            return None
        found: set[str] | None = None
        for group in pattern.labels:
            members = {c for label in group for c in self.model.subtypes(label)}
            found = members if found is None else found & members
        return tuple(sorted(found or ()))

    def node_matches(self, pattern: NodePattern, node: Node, partial: _Partial) -> bool:
        if pattern.variable is not None and pattern.variable in partial.row:
            bound = partial.row[pattern.variable]
            if not isinstance(bound, Node) or bound.id != node.id:
                return False
        node_type = node.revision.type
        for group in pattern.labels:
            if not any(self.model.is_a(node_type, label) for label in group):
                return False
        if pattern.properties is not None:
            for key, value in pattern.properties.entries:
                expected = self.evaluator.evaluate(value, partial.row)
                if equals(self.evaluator.node_field(node, key), expected) is not True:
                    return False
        return True

    # Relationships

    def edges(
        self, node: Node, pattern: RelPattern, direction: Direction
    ) -> list[tuple[Rel, Node]]:
        """Edges of ``node`` in ``direction`` with the pattern's types, to visible nodes."""
        revision = node.revision
        types = frozenset(pattern.types)
        found: list[tuple[Rel, EntityId]] = []
        if direction in ("out", "both"):
            found += [
                (Rel(revision.id, edge), edge.target)
                for edge in revision.edges
                if not types or edge.rel in types
            ]
        if direction in ("in", "both"):
            for source, edge in self.snapshot.incoming(revision.id):
                if types and edge.rel not in types:
                    continue
                if direction == "both" and source == revision.id:
                    continue  # a self-loop is already listed as outgoing
                found.append((Rel(source, edge), source))
        found.sort(key=lambda pair: (pair[1], pair[0].edge.edge_id))
        result = []
        for rel, other in found:
            self.spend()
            target = self.node(other)
            if target is not None:
                result.append((rel, target))
        return result

    def rel_matches(self, pattern: RelPattern, rel: Rel, partial: _Partial) -> bool:
        if (rel.source, rel.edge.edge_id) in partial.used:
            return False
        if pattern.length is None and pattern.variable is not None:
            bound = partial.row.get(pattern.variable)
            if pattern.variable in partial.row and equals(bound, rel) is not True:
                return False
        if pattern.properties is not None:
            for key, value in pattern.properties.entries:
                expected = self.evaluator.evaluate(value, partial.row)
                if equals(self.evaluator.rel_field(rel, key), expected) is not True:
                    return False
        return True

    def steps(
        self, node: Node, pattern: RelPattern, direction: Direction, partial: _Partial
    ) -> Iterator[tuple[Value, Node, frozenset[tuple[EntityId, object]]]]:
        """``(relationship or path, other end, used)`` for one relationship pattern."""
        if pattern.length is None:
            for rel, other in self.edges(node, pattern, direction):
                if self.rel_matches(pattern, rel, partial):
                    yield rel, other, partial.used | {(rel.source, rel.edge.edge_id)}
            return
        low, high = pattern.length
        yield from self.paths(node, pattern, direction, partial, [], low, high)

    def paths(
        self,
        node: Node,
        pattern: RelPattern,
        direction: Direction,
        partial: _Partial,
        path: list[Rel],
        low: int,
        high: int,
    ) -> Iterator[tuple[Value, Node, frozenset[tuple[EntityId, object]]]]:
        if len(path) >= low:
            yield list(path), node, partial.used
        if len(path) == high:
            return
        for rel, other in self.edges(node, pattern, direction):
            if not self.rel_matches(pattern, rel, partial):
                continue
            extended = _Partial(partial.row, partial.used | {(rel.source, rel.edge.edge_id)})
            yield from self.paths(other, pattern, direction, extended, [*path, rel], low, high)

    # Patterns

    def lookups(self, where: Expr | None) -> dict[str, tuple[str, Expr]]:
        """``var.id = value`` and ``var.alias = value`` among the WHERE conjuncts."""
        found: dict[str, tuple[str, Expr]] = {}
        for conjunct in _conjuncts(where):
            if not isinstance(conjunct, Binary) or conjunct.op != "=":
                continue
            for side, other in ((conjunct.left, conjunct.right), (conjunct.right, conjunct.left)):
                if (
                    isinstance(side, Property)
                    and isinstance(side.subject, Variable)
                    and side.key in _LOOKUP_FIELDS
                    and isinstance(other, Const | Parameter)
                ):
                    found.setdefault(side.subject.name, (side.key, other))
        return found

    def lookup(
        self, pattern: NodePattern, hints: Mapping[str, tuple[str, Expr]]
    ) -> tuple[str, Expr] | None:
        if pattern.properties is not None:
            for key, value in pattern.properties.entries:
                if key in _LOOKUP_FIELDS and isinstance(value, Const | Parameter):
                    return key, value
        if pattern.variable is not None:
            return hints.get(pattern.variable)
        return None

    def start(
        self, part: PatternPart, partial: _Partial, hints: Mapping[str, tuple[str, Expr]]
    ) -> tuple[int, list[Node]]:
        """The index of the most selective node and its candidates."""
        for index, pattern in enumerate(part.nodes):
            if pattern.variable is not None and pattern.variable in partial.row:
                bound = partial.row[pattern.variable]
                return index, [bound] if isinstance(bound, Node) else []
        for index, pattern in enumerate(part.nodes):
            lookup = self.lookup(pattern, hints)
            if lookup is not None:
                return index, self.looked_up(*lookup, partial)
        labeled = [(i, self.classes(p)) for i, p in enumerate(part.nodes) if p.labels]
        if labeled:
            index, types = min(labeled, key=lambda pair: (len(self.scan(pair[1])), pair[0]))
            return index, [Node(r) for r in self.scan(types)]
        return 0, [Node(r) for r in self.scan(None)]

    def looked_up(self, key: str, value: Expr, partial: _Partial) -> list[Node]:
        wanted = self.evaluator.evaluate(value, partial.row)
        if not isinstance(wanted, str):
            return []
        if key == "alias":
            resolved = self.snapshot.resolve_alias(wanted)
            entity = None if resolved is None else resolved[0]
        else:
            try:
                entity = EntityId(parse_id(wanted))
            except GraphCliError:
                return []
        found = None if entity is None else self.node(entity)
        return [] if found is None else [found]

    def part(
        self, part: PatternPart, partial: _Partial, hints: Mapping[str, tuple[str, Expr]]
    ) -> Iterator[_Partial]:
        index, candidates = self.start(part, partial, hints)
        for node in candidates:
            self.spend()
            if not self.node_matches(part.nodes[index], node, partial):
                continue
            bound = _bind(partial, part.nodes[index].variable, node)
            for right in self.extend(part, index, node, bound, step=1):
                yield from self.extend(part, index, node, right, step=-1)

    def extend(
        self, part: PatternPart, index: int, node: Node, partial: _Partial, *, step: int
    ) -> Iterator[_Partial]:
        """Bindings of the chain from ``nodes[index]`` to its right (+1) or left (-1) end."""
        if (step == 1 and index == len(part.nodes) - 1) or (step == -1 and index == 0):
            yield partial
            return
        rel_index = index if step == 1 else index - 1
        pattern = part.rels[rel_index]
        direction = pattern.direction if step == 1 else _reverse(pattern.direction)
        next_pattern = part.nodes[index + step]
        for value, other, used in self.steps(node, pattern, direction, partial):
            moved = _Partial(partial.row, used)
            if not self.node_matches(next_pattern, other, moved):
                continue
            if step == -1 and isinstance(value, list):
                value = value[::-1]
            bound = _bind(_bind(moved, pattern.variable, value), next_pattern.variable, other)
            yield from self.extend(part, index + step, other, bound, step=step)

    def match(self, clause: Match, rows: Sequence[Row]) -> list[Row]:
        self.at(clause.pos, "OPTIONAL MATCH" if clause.optional else "MATCH")
        hints = self.lookups(clause.where)
        new = _pattern_variables(clause.patterns)
        result: list[Row] = []
        for row in rows:
            partials = [_Partial(dict(row), frozenset())]
            for part in clause.patterns:
                partials = [found for p in partials for found in self.part(part, p, hints)]
            matched = [
                p.row
                for p in partials
                if clause.where is None or self.evaluator.condition(clause.where, p.row)
            ]
            if not matched and clause.optional:
                matched = [{**row, **{v: None for v in new if v not in row}}]
            self.spend(len(matched))
            result += matched
        return result

    def exists(self, part: PatternPart, row: Row) -> bool | None:
        """A pattern predicate: at least one binding; null when a bound end is null."""
        if any(name in row and row[name] is None for name in _pattern_variables([part])):
            return None
        partial = _Partial(dict(row), frozenset())
        return next(self.part(part, partial, {}), None) is not None

    # Clause pipeline

    def unwind(self, clause: Unwind, rows: Sequence[Row]) -> list[Row]:
        self.at(clause.pos, "UNWIND")
        result: list[Row] = []
        for row in rows:
            value = self.evaluator.evaluate(clause.expr, row)
            items = [] if value is None else value if isinstance(value, list) else [value]
            self.spend(len(items))
            result += [{**row, clause.variable: item} for item in items]
        return result

    def project(
        self, projection: Projection, rows: Sequence[Row], pos: Pos, name: str
    ) -> list[Row]:
        """``WITH`` or ``RETURN``: the projected rows, distinct, sorted and paged."""
        self.at(pos, name)
        aggregating = any(aggregates_in(item.expr) for item in projection.items)
        shaped: list[tuple[dict[str, Value], Row]]
        if aggregating:
            shaped = self.aggregate(projection, rows)
        else:
            shaped = [(self.shape(projection, row, row), row) for row in rows]
        self.spend(len(shaped))
        if projection.distinct:
            seen: set[tuple[GroupKey, ...]] = set()
            unique = []
            for out, env in shaped:
                key = tuple(group_key(v) for v in out.values())
                if key not in seen:
                    seen.add(key)
                    unique.append((out, env))
            shaped = unique
        for item in reversed(projection.order):
            own = aggregating or projection.distinct
            shaped.sort(
                key=lambda pair: order_key(
                    self.evaluator.evaluate(item.expr, pair[0] if own else {**pair[1], **pair[0]})
                ),
                reverse=item.descending,
            )
        start = _count(projection.skip)
        stop = None if projection.limit is None else start + _count(projection.limit)
        return [out for out, _ in shaped[start:stop]]

    def shape(
        self,
        projection: Projection,
        row: Row,
        env: Row,
        aggregates: Mapping[int, Value] | None = None,
    ) -> dict[str, Value]:
        out: dict[str, Value] = {}
        if projection.star:
            out.update({k: row[k] for k in sorted(row)})
        for item in projection.items:
            out[item.name] = self.evaluator.evaluate(item.expr, env, aggregates)
        return out

    def aggregate(
        self, projection: Projection, rows: Sequence[Row]
    ) -> list[tuple[dict[str, Value], Row]]:
        """One row per group of the non-aggregate items (and ``*``)."""
        keys = [item for item in projection.items if not aggregates_in(item.expr)]
        groups: dict[tuple[GroupKey, ...], list[Row]] = {}
        for row in rows:
            star = [row[k] for k in sorted(row)] if projection.star else []
            values = star + [self.evaluator.evaluate(item.expr, row) for item in keys]
            groups.setdefault(tuple(group_key(v) for v in values), []).append(row)
        if not groups and not keys and not projection.star:
            groups[()] = []  # aggregates over no rows: count 0, collect [], ...
        shaped: list[tuple[dict[str, Value], Row]] = []
        for members in groups.values():
            first = members[0] if members else {}
            computed = {
                id(node): self.aggregate_value(node, members)
                for item in projection.items
                for node in aggregates_in(item.expr)
            }
            out = self.shape(projection, first, first, computed)
            shaped.append((out, out))
        return shaped

    def aggregate_value(self, node: Expr, rows: Sequence[Row]) -> Value:
        if isinstance(node, CountStar):
            return len(rows)
        assert isinstance(node, FunctionCall)
        (arg,) = node.args
        values: list[Value] = [
            v for v in (self.evaluator.evaluate(arg, row) for row in rows) if v is not None
        ]
        if node.distinct:
            values = _distinct(values)
        match node.name:
            case "count":
                return len(values)
            case "collect":
                return values
            case "min":
                return min(values, key=order_key, default=None)
            case "max":
                return max(values, key=order_key, default=None)
        numbers = [_number(v, node) for v in values]
        total: Number = sum(numbers, 0)
        if isinstance(total, int) and not MIN_INTEGER <= total <= MAX_INTEGER:
            raise invalid_value(node.pos, "integer overflow")
        if node.name == "sum":
            return total
        assert node.name == "avg", node.name
        return None if not numbers else Decimal(total) / len(numbers)


def _bind(partial: _Partial, variable: str | None, value: Value) -> _Partial:
    if variable is None or variable in partial.row:
        return partial
    return _Partial({**partial.row, variable: value}, partial.used)


def _reverse(direction: Direction) -> Direction:
    return "in" if direction == "out" else "out" if direction == "in" else "both"


def _conjuncts(where: Expr | None) -> Iterator[Expr]:
    if where is None:
        return
    if isinstance(where, Binary) and where.op == "and":
        yield from _conjuncts(where.left)
        yield from _conjuncts(where.right)
    else:
        yield where


def _pattern_variables(parts: Sequence[PatternPart]) -> list[str]:
    names: list[str] = []
    for part in parts:
        for node in part.nodes:
            if node.variable is not None and node.variable not in names:
                names.append(node.variable)
        for rel in part.rels:
            if rel.variable is not None and rel.variable not in names:
                names.append(rel.variable)
    return names


@dataclass(frozen=True, slots=True)
class Result:
    columns: tuple[str, ...]
    rows: tuple[tuple[Value, ...], ...]


def execute(
    resolved: ResolvedQuery,
    model: CompiledModel,
    snapshot: Snapshot,
    *,
    include_inactive: bool = False,
    budget: int = DEFAULT_BUDGET,
) -> Result:
    """Run the clauses in order over one snapshot; the rows of ``RETURN``."""
    executor = Executor(resolved, model, snapshot, include_inactive=include_inactive, budget=budget)
    rows: list[Row] = [{}]
    for clause in resolved.query.clauses:
        if isinstance(clause, Match):
            rows = executor.match(clause, rows)
        elif isinstance(clause, Unwind):
            rows = executor.unwind(clause, rows)
        elif isinstance(clause, With):
            rows = executor.project(clause.projection, rows, clause.pos, "WITH")
            if clause.where is not None:
                rows = [row for row in rows if executor.evaluator.condition(clause.where, row)]
        else:
            rows = executor.project(clause.projection, rows, clause.pos, "RETURN")
    columns = resolved.columns
    return Result(columns, tuple(tuple(row[c] for c in columns) for row in rows))


def _count(value: Expr | None) -> int:
    """``SKIP``/``LIMIT``: resolve made them integer constants."""
    if value is None:
        return 0
    assert isinstance(value, Const) and isinstance(value.value, int), value
    return value.value


def _number(value: Value, node: FunctionCall) -> Number:
    number = as_number(value)
    if number is None:
        raise invalid_value(node.pos, f"{node.name}() needs numbers, got a {kind(value)}")
    return number


def _distinct(values: list[Value]) -> list[Value]:
    seen: set[GroupKey] = set()
    unique = []
    for value in values:
        key = group_key(value)
        if key not in seen:
            seen.add(key)
            unique.append(value)
    return unique
