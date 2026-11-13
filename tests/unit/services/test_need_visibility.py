"""``need_decisions`` for a caller with type read limits (type read limits plan V7): an option
the caller does not see does not meet a need, and nothing else about the decisions changes."""

import pytest
from support.services import app_actor

from graph_cli.access_control.policy import BUILTIN_POLICY_SET
from graph_cli.kernel.authz import AuthzContext
from graph_cli.kernel.ids import GraphId, parse_id
from graph_cli.kinetic.action_needs import NeedOptions
from graph_cli.services.action_rights import need_decisions

GRAPH = GraphId(parse_id("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a"))
CTX = AuthzContext(op_count=1, has_reason=True, dry_run=False)
LINK = NeedOptions("write.link", "x:Base", (("x:A", ()), ("x:B", ())))
ONE = NeedOptions("write.transition", "x:Base", (("x:A", ()),))
BARE = NeedOptions("write.create", "x:Plain", ())  # a declared class without concrete subtypes
ANY = NeedOptions("write.unlink", None, ())


def result(need: NeedOptions, visible: frozenset[str] | None):  # type: ignore[no-untyped-def]
    return need_decisions(app_actor(), GRAPH, need, CTX, BUILTIN_POLICY_SET, frozenset(), visible)


def test_without_a_limit_nothing_changes() -> None:
    everything = frozenset({"x:A", "x:B", "x:Plain"})
    for need in (LINK, ONE, BARE, ANY):
        assert result(need, None) == result(need, everything)


def test_an_option_outside_the_visible_set_is_skipped() -> None:
    found = result(LINK, frozenset({"x:B"}))
    assert found.met and found.type == "x:B"
    assert [d.resource.rpartition("type:")[2] for d in found.decisions] == ["x:B"]
    assert result(LINK, frozenset({"x:A"})).type == "x:A"


@pytest.mark.parametrize("need", [LINK, ONE])
def test_a_need_with_no_visible_option_is_not_met_and_decides_nothing(need: NeedOptions) -> None:
    found = result(need, frozenset({"x:Other"}))
    assert not found.met and found.decisions == () and found.type is None


def test_a_declared_class_without_options_must_be_visible_itself() -> None:
    assert result(BARE, frozenset({"x:Plain"})).met
    hidden = result(BARE, frozenset({"x:A"}))
    assert not hidden.met and hidden.decisions == () and hidden.type == "x:Plain"
    assert result(ANY, frozenset()).met  # a need on no class at all is not about a type
