"""Node kinds of the compiled expression AST and walkers over it (B7.15).

Shared by the evaluator, the loader (which refuses node kinds it does not
know) and the write path (which needs the relations of aggregates).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Final

from graph_cli.kernel.canonical import JsonValue

COMPARISONS: Final = frozenset({"eq", "ne", "lt", "le", "gt", "ge", "in"})
TESTS: Final = frozenset({"exists", "nonempty"})
TRUTH_NODES: Final = frozenset({"and", "or", "not", "implies", *TESTS, *COMPARISONS})
AGGREGATES: Final = frozenset({"count", "sum"})
OPERAND_NODES: Final = frozenset({"attr", "const", "self", *AGGREGATES})


def is_aggregate(node: JsonValue) -> bool:
    return isinstance(node, Mapping) and len(node) == 1 and next(iter(node)) in AGGREGATES


def walk(node: JsonValue, truth: bool = True) -> Iterator[tuple[str, JsonValue, bool]]:
    """``(kind, arg, is_truth_node)`` of every node, outermost first; skips bad shapes."""
    if not isinstance(node, Mapping) or len(node) != 1:
        return
    ((kind, arg),) = node.items()
    yield kind, arg, truth
    if not truth:
        return
    if kind == "not":
        yield from walk(arg)
    elif kind in ("and", "or", "implies") and isinstance(arg, list | tuple):
        for item in arg:
            yield from walk(item)
    elif kind in TESTS:
        yield from walk(arg, False)
    elif kind in COMPARISONS and isinstance(arg, list | tuple):
        for item in arg:
            yield from walk(item, False)


def unknown_nodes(expr: JsonValue) -> list[str]:
    """Node kinds this AST version does not have, in AST order."""
    return [
        kind
        for kind, _, truth in walk(expr)
        if kind not in (TRUTH_NODES if truth else OPERAND_NODES)
    ]


def aggregate_relations(expr: JsonValue) -> frozenset[tuple[str, str]]:
    """``(rel, direction)`` of every aggregate operand in ``expr``."""
    return frozenset(
        (str(arg.get("rel")), str(arg.get("direction", "out")))
        for kind, arg, truth in walk(expr)
        if not truth and kind in AGGREGATES and isinstance(arg, Mapping)
    )
