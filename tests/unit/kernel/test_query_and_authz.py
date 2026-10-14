"""Query AST invariants and authz decision shapes (contracts B13, B8.8, B6)."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from graph_cli.kernel.actions import Action
from graph_cli.kernel.authz import Decision, Obligations
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId
from graph_cli.kernel.query_ast import AsOf, NeighborQuery, Query


def test_query_defaults() -> None:
    query = Query()
    assert query.record_status == frozenset({"active"})
    assert (query.limit, query.offset, query.as_of) == (50, 0, None)


def test_as_of_needs_exactly_one_coordinate() -> None:
    assert AsOf(tx_seq=3).tx_seq == 3
    for bad in ({}, {"tx_seq": 1, "recorded_at": datetime(2026, 1, 1, tzinfo=UTC)}):
        with pytest.raises(GraphCliError):
            AsOf(**bad)  # type: ignore[arg-type]


def test_neighbor_depth_is_bounded() -> None:
    start = EntityId(UUID(int=1))
    NeighborQuery(start, None, "both", depth=3)
    with pytest.raises(GraphCliError):
        NeighborQuery(start, None, "both", depth=4)


def test_negative_paging_is_refused() -> None:
    with pytest.raises(GraphCliError):
        Query(limit=-1)


def test_decision_code_iff_deny() -> None:
    allow = Decision("allow", Action("read.get"), "graph:x", "builtin.allow_app", "sha256:p")
    assert "code" not in allow.to_json()
    deny = Decision(
        "deny",
        Action("maint.repair"),
        "graph:x",
        "ceiling.app_no_maint",
        "sha256:p",
        Obligations(),
        "access.ceiling",
    )
    assert deny.to_json()["code"] == "access.ceiling"
    with pytest.raises(GraphCliError):
        Decision("deny", Action("read.get"), "graph:x", "r", "sha256:p")
    assert Obligations(True, 10).to_json() == {"require_reason": True, "max_batch": 10}
