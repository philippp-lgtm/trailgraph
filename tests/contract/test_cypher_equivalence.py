"""Cypher against find and neighbors on every backend and toy ontology (plan cypher_query.md 6).

The main guard against a misleading subset: for generated ``find`` specs
(types, where ops, relations both ways, sort, paging) and ``neighbors`` specs
(relations, direction, depth), the equivalent Cypher query returns the same
entities, for ``find`` also in the same order.

The translation spells out the documented differences instead of hiding
them: Cypher hides inactive entities at both ends of a pattern, so it runs
with ``include_inactive`` and filters ``record_status`` like ``find``;
``find`` puts missing values last in both directions (``IS NULL`` first in
the Cypher sort); a negated ``find`` filter is true on a missing value,
where Cypher's ``NOT`` of null is null (``coalesce(..., false)``).
"""

import random
import zlib
from decimal import Decimal

from support.memlog import model as fixture_model
from support.stores import StoreEnv

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.ids import EntityId
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.records import Revision
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.ontology_layer.vocabulary import SYSTEM_FIELDS
from graph_cli.query.cypher import render
from graph_cli.query.cypher.execute import execute
from graph_cli.query.cypher.parser import parse
from graph_cli.query.cypher.resolve import resolve
from graph_cli.query.cypher.values import Node
from graph_cli.query.plan import FindSpec, plan_find, plan_neighbors

SPECS_PER_ENV = 150
_ORDER_OPS = ("lt", "le", "gt", "ge")


def cypher_ids(model: CompiledModel, snapshot: Snapshot, text: str) -> list[EntityId]:
    result = execute(resolve(parse(text), model, {}), model, snapshot, include_inactive=True)
    ids = []
    for (value,) in result.rows:
        assert isinstance(value, Node), text
        ids.append(value.id)
    return ids


def literal(value: JsonValue, base: str) -> str:
    if base == "decimal" and isinstance(value, str):
        return str(Decimal(value))
    return render.const(value)


class Generator:
    """Random ``find`` specs and their Cypher text, with values taken from the data."""

    def __init__(self, model: CompiledModel, revisions: list[Revision], seed: int) -> None:
        self.model = model
        self.revisions = revisions
        self.rng = random.Random(seed)

    def base(self, cls: str, field: str) -> tuple[str, bool]:
        attribute = self.model.classes[cls].attributes[field]
        return self.model.value_base(attribute.kind, attribute.type), attribute.many

    def sample(self, field: str) -> JsonValue:
        present = [r.attrs[field] for r in self.revisions if r.attrs.get(field) is not None]
        return self.rng.choice(present) if present else None

    def where(self, classes: list[str]) -> tuple[str, str] | None:
        fields = sorted({(c, f) for c in classes for f in self.model.classes[c].attributes})
        cls, field = self.rng.choice(fields)
        base, many = self.base(cls, field)
        if base in ("json", "blob", "local_path", "external_ref"):
            return None
        value = self.sample(field)
        var = f"n.{render.key(field)}"
        if value is None or self.rng.random() < 0.15:
            exists = self.rng.random() < 0.5
            return (
                f"{field}:exists={str(exists).lower()}",
                f"{var} IS {'NOT ' if exists else ''}NULL",
            )
        if many:
            assert isinstance(value, list) and value
            item = self.rng.choice(value)
            text = item if isinstance(item, str) else str(item).lower()
            if self.rng.random() < 0.5:
                return f"{field}={text}", f"{literal(item, base)} IN {var}"
            return f"{field}:ne={text}", f"NOT coalesce({literal(item, base)} IN {var}, false)"
        text = value if isinstance(value, str) else str(value).lower()
        op = self.rng.choice(("eq", "ne", *_ORDER_OPS, "in", "prefix", "contains"))
        if op in ("prefix", "contains") and not isinstance(value, str):
            op = "eq"
        if op == "in":
            if "," in text:
                op = "eq"
            else:
                other = self.sample(field)
                other_text = other if isinstance(other, str) else str(other).lower()
                if other is None or "," in other_text:
                    other, other_text = value, text
                cypher = f"{var} IN [{literal(value, base)}, {literal(other, base)}]"
                return f"{field}:in={text},{other_text}", cypher
        if op in ("prefix", "contains"):
            assert isinstance(value, str)
            part = value[: max(1, len(value) // 2)] if op == "prefix" else value[1:3]
            word = "STARTS WITH" if op == "prefix" else "CONTAINS"
            return f"{field}:{op}={part}", f"{var} {word} {render.const(part)}"
        symbol = {"eq": "=", "ne": "<>", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}[op]
        prefix = "" if op == "eq" else f":{op}"
        return f"{field}{prefix}={text}", f"{var} {symbol} {literal(value, base)}"

    def relation(self, incoming: bool) -> tuple[tuple[str, tuple[EntityId, ...]], str]:
        rel = self.rng.choice(sorted(self.model.relations))
        edges = [
            (source.id, edge.target)
            for source in self.revisions
            for edge in source.edges
            if edge.rel == rel
        ]
        arrow = ("<-[:{}]-" if incoming else "-[:{}]->").format(render.name(rel))
        if not edges or self.rng.random() < 0.3:
            return (rel, ()), f"(n){arrow}()"
        ends = sorted({s if incoming else t for s, t in self.rng.sample(edges, min(2, len(edges)))})
        tests = [f"(n){arrow}({{id: {render.const(str(e))}}})" for e in ends]
        return (rel, tuple(ends)), "(" + " OR ".join(tests) + ")"

    def find(self) -> tuple[FindSpec, str]:
        classes = sorted(self.model.classes)
        types = sorted(self.rng.sample(classes, self.rng.choice((0, 1, 1, 2))))
        selected = sorted({c for t in types for c in self.model.subtypes(t)}) or classes
        include_inactive = self.rng.random() < 0.3
        where_text: list[str] = []
        conditions: list[str] = []
        if not include_inactive:
            conditions.append("n.record_status = 'active'")
        for _ in range(self.rng.choice((0, 1, 1, 2))):
            found = self.where(selected)
            if found is not None:
                where_text.append(found[0])
                conditions.append(found[1])
        rels: list[tuple[str, tuple[EntityId, ...]]] = []
        in_rels: list[tuple[str, tuple[EntityId, ...]]] = []
        for _ in range(self.rng.choice((0, 0, 1))):
            spec, condition = self.relation(incoming=False)
            rels.append(spec)
            conditions.append(condition)
        for _ in range(self.rng.choice((0, 0, 1))):
            spec, condition = self.relation(incoming=True)
            in_rels.append(spec)
            conditions.append(condition)
        sort_text: list[str] = []
        order: list[str] = []
        fields = sorted({f for c in selected for f in self.model.classes[c].attributes})
        for _ in range(self.rng.choice((0, 1, 1, 2))):
            field = self.rng.choice([*fields, "rev", "alias", "created_at"])
            if (
                field not in SYSTEM_FIELDS
                and self.base(_owner(self.model, selected, field), field)[1]
            ):
                continue  # many-valued attributes are not sort keys
            descending = self.rng.random() < 0.5
            sort_text.append(f"{field}:desc" if descending else field)
            var = f"n.{render.key(field)}"
            order += [f"{var} IS NULL", f"{var} DESC"] if descending else [var]
        order.append("n.id")
        limit = self.rng.choice((1, 3, 50))
        offset = self.rng.choice((0, 0, 2))
        labels = ":" + "|".join(render.name(t) for t in types) if types else ""
        text = f"MATCH (n{labels})"
        if conditions:
            text += " WHERE " + " AND ".join(conditions)
        text += f" RETURN n ORDER BY {', '.join(order)} SKIP {offset} LIMIT {limit}"
        spec = FindSpec(
            types=tuple(types),
            where=tuple(where_text),
            rels=tuple(rels),
            in_rels=tuple(in_rels),
            include_inactive=include_inactive,
            sort=tuple(sort_text),
            limit=limit,
            offset=offset,
        )
        return spec, text


def _owner(model: CompiledModel, classes: list[str], field: str) -> str:
    return next(c for c in classes if field in model.classes[c].attributes)


def _revisions(model: CompiledModel, snapshot: Snapshot) -> list[Revision]:
    query = plan_find(model, FindSpec(include_inactive=True, limit=500))
    return list(snapshot.query(query).items)


def test_cypher_returns_what_find_returns(env: StoreEnv) -> None:
    env.random(80)
    model = fixture_model(env.ontology)
    snapshot = env.store.snapshot()
    generator = Generator(
        model, _revisions(model, snapshot), seed=zlib.crc32(env.ontology.encode())
    )
    hits = 0
    for _ in range(SPECS_PER_ENV):
        spec, text = generator.find()
        expected = [r.id for r in snapshot.query(plan_find(model, spec)).items]
        assert cypher_ids(model, snapshot, text) == expected, (spec, text)
        hits += bool(expected)
    assert hits > SPECS_PER_ENV // 4  # the generator finds data, not only empty results


def test_cypher_returns_what_neighbors_returns(env: StoreEnv) -> None:
    env.random(80)
    model = fixture_model(env.ontology)
    snapshot = env.store.snapshot()
    revisions = _revisions(model, snapshot)
    rng = random.Random(7)
    relations = sorted(model.relations)
    found_any = False
    targets = {edge.target for r in revisions for edge in r.edges}
    connected = [r for r in revisions if r.edges or r.id in targets]
    assert connected, "the random data has no edges"
    for revision in connected[:15]:
        rels = sorted(rng.sample(relations, rng.choice((0, 1, 2))))
        direction = rng.choice(("out", "in", "both"))
        depth = rng.choice((1, 2, 3))
        query = plan_neighbors(model, revision.id, rels=rels, direction=direction, depth=depth)
        expected = sorted(neighbor for neighbor, _, _ in snapshot.neighbors(query))
        types = ":" + "|".join(render.name(r) for r in rels) if rels else ""
        arrow = {"out": ("-", "->"), "in": ("<-", "-"), "both": ("-", "-")}[direction]
        text = (
            f"MATCH (s {{id: {render.const(str(revision.id))}}})"
            f"{arrow[0]}[{types}*1..{depth}]{arrow[1]}(m) WHERE m <> s "
            "RETURN DISTINCT m ORDER BY m.id"
        )
        assert cypher_ids(model, snapshot, text) == expected, text
        found_any = found_any or bool(expected)
    assert found_any
