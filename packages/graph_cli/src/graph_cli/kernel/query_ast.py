"""Backend-neutral query AST (contracts B13, B8.8).

Storage receives only resolved queries: types are concrete CURIEs (subtypes
expanded) and fields are validated by ``graph_cli.query`` before the port.
Tags are attribute predicates, not an AST concept.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, TermRef
from graph_cli.kernel.records import Revision

FieldName = str  # attribute name or reserved system field (B7.8)
CmpOp = Literal["eq", "ne", "lt", "le", "gt", "ge", "contains", "prefix", "exists", "in", "lang"]

MAX_NEIGHBOR_DEPTH: Final = 3


def _invalid(message: str) -> GraphCliError:
    return GraphCliError("internal.error", f"invalid query: {message}")


@dataclass(frozen=True, slots=True)
class AsOf:
    """Exactly one of ``tx_seq`` and ``recorded_at`` is set."""

    tx_seq: int | None = None
    recorded_at: datetime | None = None

    def __post_init__(self) -> None:
        if (self.tx_seq is None) == (self.recorded_at is None):
            raise _invalid("as_of needs exactly one of tx_seq and recorded_at")


@dataclass(frozen=True, slots=True)
class Cmp:
    """``numeric`` compares decimal strings as numbers, ``tagged`` the text of
    language-tagged values; the planner sets both (B13.13)."""

    field: FieldName
    op: CmpOp
    value: JsonValue = None
    numeric: bool = False
    tagged: bool = False


@dataclass(frozen=True, slots=True)
class And:
    items: tuple[Pred, ...]


@dataclass(frozen=True, slots=True)
class Or:
    items: tuple[Pred, ...]


@dataclass(frozen=True, slots=True)
class Not:
    item: Pred


Pred = Cmp | And | Or | Not


@dataclass(frozen=True, slots=True)
class RelConstraint:
    rel: TermRef
    direction: Literal["out", "in"]
    targets: tuple[EntityId, ...] = ()  # the other end is any of these; () = any entity
    exists: bool = True


@dataclass(frozen=True, slots=True)
class OrderKey:
    field: FieldName
    descending: bool = False
    numeric: bool = False
    tagged: bool = False


@dataclass(frozen=True, slots=True)
class Query:
    types: tuple[TermRef, ...] = ()  # concrete, subtype-expanded; () = any
    where: Pred | None = None
    rels: tuple[RelConstraint, ...] = ()
    record_status: frozenset[str] = frozenset({"active"})
    changed_since_seq: int | None = None
    order: tuple[OrderKey, ...] = ()  # "id" is always appended as the final key
    limit: int = 50
    offset: int = 0
    fields: tuple[FieldName, ...] | None = None  # rendering hint for services; ignored by storage
    as_of: AsOf | None = None  # v1: must be None (B8.8, services enforce)
    # Entity types the caller may see; None = no limit. An empty set sees nothing. Narrows
    # ``types`` (never widens it), hides edge ends of other types and strips the revisions
    # handed out (``query_eval`` "Visibility").
    visible: frozenset[TermRef] | None = None

    def __post_init__(self) -> None:
        if self.limit < 0 or self.offset < 0:
            raise _invalid("limit and offset must not be negative")


@dataclass(frozen=True, slots=True)
class QueryPage:
    items: tuple[Revision, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class NeighborQuery:
    start: EntityId
    rels: tuple[TermRef, ...] | None
    direction: Literal["out", "in", "both"]
    depth: int = 1
    visible: frozenset[TermRef] | None = None  # as ``Query.visible``: hidden nodes are not walked

    def __post_init__(self) -> None:
        if not 1 <= self.depth <= MAX_NEIGHBOR_DEPTH:
            raise _invalid(f"depth must be 1..{MAX_NEIGHBOR_DEPTH}")
