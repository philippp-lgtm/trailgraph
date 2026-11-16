"""``need_decisions`` for a caller with an attribute view (field visibility plan F6, defence in
depth): an option whose terms name an attribute the caller does not see does not meet a need,
whether or not the policy has term rules for the type, and nothing else about the decisions
changes."""

import pytest
from support.services import app_actor

from graph_cli.access_control.policy import BUILTIN_POLICY_SET
from graph_cli.kernel.authz import AuthzContext
from graph_cli.kernel.ids import GraphId, parse_id
from graph_cli.kernel.read_view import ReadView
from graph_cli.kinetic.action_needs import FineNeed, NeedOptions
from graph_cli.services.action_rights import need_decisions

GRAPH = GraphId(parse_id("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a"))
CTX = AuthzContext(op_count=1, has_reason=True, dry_run=False)
NOTE: tuple[FineNeed, ...] = (("write.update", "attr", "note"),)
LINK: tuple[FineNeed, ...] = (("write.link", "rel", "x:near"),)
UPDATE = NeedOptions("write.update", "x:Base", (("x:A", NOTE), ("x:B", NOTE)))
RELATE = NeedOptions("write.link", "x:Base", (("x:A", LINK), ("x:B", LINK)))
BOTH = NeedOptions(
    "write.update",
    "x:Base",
    (
        (
            "x:A",
            (*NOTE, ("write.update", "attr", "tag")),
        ),
    ),
)
VIEW = ReadView.of(attributes={"x:A": {"name"}, "x:B": {"name", "note"}})


def result(need: NeedOptions, view: ReadView, fine: frozenset[str] = frozenset()):  # type: ignore[no-untyped-def]
    return need_decisions(
        app_actor(), GRAPH, need, CTX, BUILTIN_POLICY_SET, fine, view.types, keeps=view.keeps
    )


@pytest.mark.parametrize("fine", [frozenset(), frozenset({"x:A", "x:B"})])
def test_an_option_naming_a_hidden_attribute_is_skipped(fine: frozenset[str]) -> None:
    found = result(UPDATE, VIEW, fine)
    assert found.met and found.type == "x:B"
    assert found.decisions and all("type:x:B" in d.resource for d in found.decisions)


def test_a_need_met_only_through_a_hidden_attribute_is_not_met_and_decides_nothing() -> None:
    found = result(BOTH, VIEW)
    assert not found.met and found.decisions == () and found.type is None
    nothing = ReadView.of(attributes={"x:A": {"name"}, "x:B": {"name"}})
    found = result(UPDATE, nothing)
    assert not found.met and found.decisions == () and found.type is None


def test_a_type_outside_the_attribute_map_keeps_nothing() -> None:
    view = ReadView.of(attributes={"x:B": {"name", "note"}})
    assert result(UPDATE, view).type == "x:B"
    assert not result(NeedOptions("write.update", "x:Base", (("x:A", NOTE),)), view).met


def test_relations_and_needs_without_attribute_terms_are_not_judged() -> None:
    assert result(RELATE, VIEW).met and result(RELATE, VIEW).type == "x:A"
    bare = NeedOptions("write.create", "x:Plain", ())
    assert result(bare, VIEW).met


def test_without_an_attribute_half_nothing_changes() -> None:
    types_only = ReadView.of(frozenset({"x:A", "x:B"}))
    for need in (UPDATE, RELATE, BOTH):
        plain = need_decisions(
            app_actor(), GRAPH, need, CTX, BUILTIN_POLICY_SET, frozenset(), types_only.types
        )
        assert result(need, types_only) == plain
        assert result(need, ReadView()) == need_decisions(
            app_actor(), GRAPH, need, CTX, BUILTIN_POLICY_SET, frozenset()
        )


def test_an_attribute_view_and_a_type_limit_both_apply() -> None:
    view = ReadView.of(frozenset({"x:A"}), {"x:A": {"name"}, "x:B": {"name", "note"}})
    found = result(UPDATE, view)  # x:B is hidden as a type, x:A lacks the attribute
    assert not found.met and found.decisions == ()
