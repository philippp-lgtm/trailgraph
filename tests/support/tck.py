"""A small runner for openCypher TCK scenarios against the Cypher subset.

The vendored feature files (``tests/fixtures/opencypher_tck``) set up their
graphs with ``CREATE`` and use any labels, relationship types and property
keys. The runner turns each setup into revisions of a model built for the
scenario: one class per label (a class per label combination, with the
labels as ancestors), one relation per relationship type, every property
key a ``json`` attribute or qualifier. Results are compared as normalized
values: nodes by their labels and properties, relationships by type and
properties, ``1`` and ``1.0`` kept apart (a Cypher float is a decimal here).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import UUID

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EdgeId, EntityId, TermRef, TxId
from graph_cli.kernel.records import EdgeRecord, Revision
from graph_cli.ontology_layer.model import (
    AttributeDef,
    Bound,
    ClassDef,
    CompiledModel,
    QualifierDef,
    RelationDef,
    TermDoc,
)
from graph_cli.ontology_layer.vocabulary import SYSTEM_FIELDS
from graph_cli.query.cypher import ast
from graph_cli.query.cypher.evaluate import Evaluator
from graph_cli.query.cypher.execute import execute
from graph_cli.query.cypher.lexer import Token, tokenize
from graph_cli.query.cypher.parser import parse
from graph_cli.query.cypher.resolve import resolve
from graph_cli.query.cypher.values import Node, Rel
from support.snapshots import DictSnapshot

PREFIX = "tck"
UNLABELED = f"{PREFIX}:_"
_AT = datetime(2026, 1, 1, tzinfo=UTC)
_TX = TxId(UUID("0192f000-0000-7000-8000-0000000000aa"))
_DOC = TermDoc("", None, False, MappingProxyType({}))


class Skip(Exception):
    """The runner cannot set the scenario up."""


class Mismatch(AssertionError):
    """The outcome differs from the scenario; ``error`` when the query raised one."""

    def __init__(self, message: str, error: GraphCliError | None = None) -> None:
        super().__init__(message)
        self.error = error


# Feature files


@dataclass
class Scenario:
    feature: str
    name: str
    setup: list[str] = field(default_factory=list)
    parameters: dict[str, str] = field(default_factory=dict)
    query: str = ""
    expect: str = ""  # "rows", "error"
    ordered: bool = False
    lists_unordered: bool = False
    table: list[list[str]] = field(default_factory=list)
    error: str = ""

    @property
    def id(self) -> str:
        return f"{self.feature}: {self.name}"


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def scenarios(path: Path) -> Iterator[Scenario]:
    lines = path.read_text(encoding="utf-8").splitlines()
    feature = path.stem
    background: list[str] = []
    blocks: list[tuple[str, list[str]]] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("Background:"):
            blocks.append(("", background))
        elif stripped.startswith("Scenario"):
            blocks.append((stripped.split(":", 1)[1].strip(), []))
        elif blocks and not stripped.startswith(("#", "@")):
            blocks[-1][1].append(line)
    for name, body in blocks:
        if name:
            yield from _expand(feature, name, [*background, *body])


def _expand(feature: str, name: str, body: list[str]) -> Iterator[Scenario]:
    if not any(line.strip().startswith("Examples:") for line in body):
        yield _scenario(feature, name, body)
        return
    at = next(i for i, line in enumerate(body) if line.strip().startswith("Examples:"))
    rows = [_cells(line) for line in body[at + 1 :] if line.strip().startswith("|")]
    header, *examples = rows
    for number, values in enumerate(examples, 1):
        substituted = []
        for line in body[:at]:
            for key, value in zip(header, values, strict=True):
                line = line.replace(f"<{key}>", value)
            substituted.append(line)
        yield _scenario(feature, f"{name} #{number}", substituted)


def _scenario(feature: str, name: str, body: list[str]) -> Scenario:
    scenario = Scenario(feature, name)
    i = 0

    def docstring() -> str:
        nonlocal i
        assert body[i + 1].strip() == '"""', body[i + 1]
        end = next(j for j in range(i + 2, len(body)) if body[j].strip() == '"""')
        text = "\n".join(line.strip() for line in body[i + 2 : end])
        i = end
        return text

    def table() -> list[list[str]]:
        nonlocal i
        rows = []
        while i + 1 < len(body) and body[i + 1].strip().startswith("|"):
            i += 1
            rows.append(_cells(body[i]))
        return rows

    while i < len(body):
        step = body[i].strip()
        if step.endswith("having executed:"):
            scenario.setup.append(docstring())
        elif step.endswith("parameters are:"):
            scenario.parameters = {row[0]: row[1] for row in table()}
        elif step.endswith("executing query:"):
            scenario.query = docstring()
        elif step.startswith("Then the result should be"):
            scenario.expect = "rows"
            scenario.ordered = "in order" in step
            scenario.lists_unordered = "ignoring element order for lists" in step
            scenario.table = table()
        elif step.startswith("Then a "):
            scenario.expect = "error"
            scenario.error = step.removeprefix("Then a ")
        elif step.startswith("And the side effects should be"):
            table()
        elif step.startswith(("Given", "And no side effects", "And ")) or not step:
            pass
        else:
            raise AssertionError(f"unknown step {step!r} in {scenario.id}")
        i += 1
    return scenario


# Graph setup


@dataclass
class _Created:
    labels: frozenset[str]
    props: dict[str, Any]
    edges: list[tuple[str, int, dict[str, Any]]] = field(
        default_factory=list
    )  # (type, target, props)


@dataclass
class World:
    nodes: list[_Created] = field(default_factory=list)
    labels: set[str] = field(default_factory=set)
    types: set[str] = field(default_factory=set)
    keys: set[str] = field(default_factory=set)


def _constant(expr: ast.Expr) -> Any:
    evaluator = Evaluator(_EMPTY_MODEL, {}, lambda part, row: None)
    return evaluator.evaluate(expr, {})


def build(setup: Sequence[str]) -> World:
    world = World()
    bound: dict[str, int] = {}
    for text in setup:
        for body in _creates(text):
            try:
                query = parse(f"MATCH {body} RETURN 1")
            except GraphCliError as exc:
                raise Skip(f"setup: {exc.message}") from None
            match = query.clauses[0]
            assert isinstance(match, ast.Match)
            for part in match.patterns:
                indexes = [_node(world, bound, n) for n in part.nodes]
                for k, rel in enumerate(part.rels):
                    if len(rel.types) != 1 or rel.length is not None:
                        raise Skip("setup: relationship without one type")
                    props = _props(rel.properties)
                    world.types.add(rel.types[0])
                    world.keys.update(props)
                    source, target = indexes[k], indexes[k + 1]
                    if rel.direction == "in":
                        source, target = target, source
                    world.nodes[source].edges.append((rel.types[0], target, props))
    return world


def _creates(text: str) -> list[str]:
    words = re.split(r"(?i)\bCREATE\b", text)
    if words[0].strip():
        raise Skip("setup: not plain CREATE")
    bodies = [w.strip() for w in words[1:]]
    if any(
        re.search(r"(?i)\b(MATCH|WITH|UNWIND|RETURN|MERGE|SET|DELETE|FOREACH)\b", b) for b in bodies
    ):
        raise Skip("setup: not plain CREATE")
    return bodies


def _props(properties: ast.MapExpr | None) -> dict[str, Any]:
    if properties is None:
        return {}
    try:
        found = {k: _constant(v) for k, v in properties.entries}
    except GraphCliError as exc:
        raise Skip(f"setup: {exc.message}") from None
    clash = sorted(set(found) & SYSTEM_FIELDS)
    if clash:
        raise Skip(f"setup: property {clash[0]!r} is a graph_cli system field")
    return {k: v for k, v in found.items() if v is not None}


def _node(world: World, bound: dict[str, int], pattern: ast.NodePattern) -> int:
    if pattern.variable is not None and pattern.variable in bound:
        return bound[pattern.variable]
    labels = frozenset(label for group in pattern.labels for label in group)
    props = _props(pattern.properties)
    world.labels.update(labels)
    world.keys.update(props)
    world.nodes.append(_Created(labels, props))
    index = len(world.nodes) - 1
    if pattern.variable is not None:
        bound[pattern.variable] = index
    return index


def _mentioned(query: ast.Query, world: World) -> None:
    """Labels, types and keys the query names, so that it resolves (Cypher does not check them)."""
    stack: list[object] = [query]
    while stack:
        item = stack.pop()
        if isinstance(item, ast.NodePattern | ast.HasLabels):
            world.labels.update(label for group in item.labels for label in group)
        if isinstance(item, ast.RelPattern):
            world.types.update(item.types)
        if isinstance(item, ast.Property):
            if item.key in SYSTEM_FIELDS:
                raise Skip(f"query: property {item.key!r} is a graph_cli system field")
            world.keys.add(item.key)
        if isinstance(item, ast.MapExpr):
            world.keys.update(k for k, _ in item.entries)
        if isinstance(item, tuple | list):
            stack.extend(item)
        elif hasattr(item, "__dataclass_fields__"):
            stack.extend(getattr(item, name) for name in item.__dataclass_fields__)


def _class_name(labels: frozenset[str]) -> str:
    if not labels:
        return UNLABELED
    if len(labels) == 1:
        return f"{PREFIX}:{next(iter(labels))}"
    return f"{PREFIX}:" + "+".join(sorted(labels))


def model(world: World) -> CompiledModel:
    attributes = {
        key: AttributeDef(
            "json", "base", False, False, False, None, "content", None, "", None, _DOC
        )
        for key in sorted(world.keys)
    }
    classes: dict[str, ClassDef] = {}

    def add(name: str, ancestors: tuple[str, ...]) -> None:
        classes[name] = ClassDef(False, None, ancestors, attributes, None, None, {}, _DOC)

    add(UNLABELED, ())
    for label in sorted(world.labels):
        add(f"{PREFIX}:{label}", ())
    for created in world.nodes:
        if len(created.labels) > 1:
            add(
                _class_name(created.labels),
                tuple(f"{PREFIX}:{label}" for label in sorted(created.labels)),
            )
    qualifiers = {key: QualifierDef("json", "base", False, False, "") for key in sorted(world.keys)}
    relations = {
        f"{PREFIX}:{t}": RelationDef(
            (), (), Bound(0, None), Bound(0, None), (), "allow", None, qualifiers, None, {}, _DOC
        )
        for t in sorted(world.types)
    }
    return CompiledModel(
        "tck", "1.0.0", (), {PREFIX: "urn:tck#"}, {}, {}, {}, classes, relations, ()
    )


_EMPTY_MODEL = model(World())


def snapshot(world: World) -> DictSnapshot:
    def entity(index: int) -> EntityId:
        return EntityId(UUID(int=(0x0192F000_0000_7000_8000_000000000000 + index + 1)))

    revisions = []
    for index, created in enumerate(world.nodes):
        edges = tuple(
            EdgeRecord(
                EdgeId(UUID(int=0x0192F000_0000_7000_9000_000000000000 + index * 1000 + k)),
                TermRef(f"{PREFIX}:{t}"),
                entity(target),
                props,
            )
            for k, (t, target, props) in enumerate(created.edges)
        )
        revisions.append(
            Revision(
                id=entity(index),
                rev=1,
                prev_rev=None,
                type=TermRef(_class_name(created.labels)),
                record_status="active",
                attrs=created.props,
                edges=edges,
                alias=None,
                valid_from=None,
                valid_to=None,
                created_at=_AT,
                recorded_at=_AT,
                tx_id=_TX,
                tx_seq=1,
                ontology_version="1.0.0",
                content_hash="",
            )
        )
    return DictSnapshot(*revisions)


# Values


def _labels_of(node: Node, compiled: CompiledModel) -> frozenset[str]:
    cls = node.revision.type
    if cls == UNLABELED:
        return frozenset()
    found = compiled.classes[cls].ancestors or (cls,)
    return frozenset(name.partition(":")[2] for name in found)


def normal(value: Any, compiled: CompiledModel, lists_unordered: bool) -> Any:
    def go(v: Any) -> Any:
        if v is None:
            return ("null",)
        if isinstance(v, bool):
            return ("bool", v)
        if isinstance(v, int):
            return ("int", v)
        if isinstance(v, Decimal):
            return ("float", v.normalize() if v else Decimal(0))
        if isinstance(v, str):
            return ("str", v)
        if isinstance(v, list):
            items = tuple(go(i) for i in v)
            return ("list", tuple(sorted(items, key=repr)) if lists_unordered else items)
        if isinstance(v, dict):
            return ("map", tuple(sorted((k, go(i)) for k, i in v.items())))
        if isinstance(v, Node):
            props = {k: x for k, x in v.revision.attrs.items() if x is not None}
            return ("node", _labels_of(v, compiled), go(props))
        if isinstance(v, Rel):
            props = {k: x for k, x in v.edge.qualifiers.items() if x is not None}
            return ("rel", v.edge.rel.partition(":")[2], go(props))
        raise AssertionError(f"unknown value {v!r}")

    return go(value)


class _Expected:
    """Parses a TCK result cell: literals, nodes ``(:A {k: 1})``, relationships ``[:T]``."""

    def __init__(self, text: str) -> None:
        self.tokens = tokenize(text)
        self.at = 0

    def peek(self, ahead: int = 0) -> Token:
        return self.tokens[min(self.at + ahead, len(self.tokens) - 1)]

    def take(self, symbol: str | None = None) -> Token:
        token = self.tokens[self.at]
        if symbol is not None and not (token.kind == "symbol" and token.text == symbol):
            raise Skip(f"expected value: unexpected {token.text!r}")
        self.at += 1
        return token

    def value(self) -> Any:
        token = self.peek()
        if token.kind == "symbol" and token.text == "-":
            self.take()
            return -self.value()
        if token.kind == "integer":
            self.take()
            return int(token.text)
        if token.kind == "float":
            self.take()
            return Decimal(token.text)
        if token.kind == "string":
            self.take()
            return token.text
        if token.kind == "ident" and token.text.lower() in ("true", "false", "null"):
            self.take()
            return {"true": True, "false": False, "null": None}[token.text.lower()]
        if token.kind == "symbol" and token.text == "[" and self.peek(1).text == ":":
            self.take("[")
            self.take(":")
            name = self.take().text
            props = self.map() if self.peek().text == "{" else {}
            self.take("]")
            return ("rel", name, props)
        if token.kind == "symbol" and token.text == "[":
            self.take("[")
            items = []
            while self.peek().text != "]":
                items.append(self.value())
                if self.peek().text == ",":
                    self.take(",")
            self.take("]")
            return items
        if token.kind == "symbol" and token.text == "{":
            return self.map()
        if token.kind == "symbol" and token.text == "(":
            self.take("(")
            labels = set()
            while self.peek().text == ":":
                self.take(":")
                labels.add(self.take().text)
            props = self.map() if self.peek().text == "{" else {}
            self.take(")")
            return ("node", frozenset(labels), props)
        raise Skip(f"expected value: {token.text!r} not covered (paths, NaN)")

    def map(self) -> dict[str, Any]:
        self.take("{")
        found = {}
        while self.peek().text != "}":
            key = self.take().text
            self.take(":")
            found[key] = self.value()
            if self.peek().text == ",":
                self.take(",")
        self.take("}")
        return found


def expected_normal(text: str, lists_unordered: bool) -> Any:
    parsed = _Expected(text)
    value = parsed.value()
    if parsed.peek().kind != "eof":
        raise Skip(f"expected value: {text!r}")

    def go(v: Any) -> Any:
        if isinstance(v, tuple) and v[0] == "node":
            return ("node", v[1], go(v[2]))
        if isinstance(v, tuple) and v[0] == "rel":
            return ("rel", v[1], go(v[2]))
        if v is None:
            return ("null",)
        if isinstance(v, bool):
            return ("bool", v)
        if isinstance(v, int):
            return ("int", v)
        if isinstance(v, Decimal):
            return ("float", v.normalize() if v else Decimal(0))
        if isinstance(v, str):
            return ("str", v)
        if isinstance(v, list):
            items = tuple(go(i) for i in v)
            return ("list", tuple(sorted(items, key=repr)) if lists_unordered else items)
        assert isinstance(v, dict)
        return ("map", tuple(sorted((k, go(i)) for k, i in v.items())))

    return go(value)


# Running


def run(scenario: Scenario) -> None:
    """Raises AssertionError on a wrong outcome, Skip when not covered."""
    world = build(scenario.setup)
    try:
        query = parse(scenario.query)
    except GraphCliError as exc:
        _expect_error(scenario, exc)
        return
    _mentioned(query, world)
    compiled = model(world)
    params = {
        k: _constant(parse(f"RETURN {v}").returns.projection.items[0].expr)
        for k, v in scenario.parameters.items()
    }
    try:
        resolved = resolve(query, compiled, params)
        result = execute(resolved, compiled, snapshot(world))
    except GraphCliError as exc:
        _expect_error(scenario, exc)
        return
    if scenario.expect != "rows":
        raise Mismatch(f"expected {scenario.error}, got rows")
    header, *rows = scenario.table
    if list(result.columns) != header:
        raise Mismatch(f"columns {list(result.columns)} instead of {header}")
    got = [tuple(normal(v, compiled, scenario.lists_unordered) for v in row) for row in result.rows]
    want = [tuple(expected_normal(cell, scenario.lists_unordered) for cell in row) for row in rows]
    same = got == want if scenario.ordered else Counter(got) == Counter(want)
    if not same:
        raise Mismatch(f"rows {got} instead of {want}")


def _expect_error(scenario: Scenario, exc: GraphCliError) -> None:
    if exc.code == "internal.error" or scenario.expect != "error":
        raise Mismatch(f"expected {scenario.expect}, got {exc.code}: {exc.message}", exc)
