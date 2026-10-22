"""Closed action taxonomy (contracts B6.1, B6.2, B6.11)."""

import pytest

from graph_cli.kernel.actions import (
    ACTIONS,
    MAINT_ACTIONS,
    READ_ACTIONS,
    RESERVED_ACTIONS,
    WRITE_ACTIONS,
    capability_class,
    extension_state_action,
    is_known_action,
    level_actions,
    require_known_action,
)
from graph_cli.kernel.errors import GraphCliError


def test_taxonomy_sizes_and_disjointness() -> None:
    assert (len(READ_ACTIONS), len(WRITE_ACTIONS), len(MAINT_ACTIONS)) == (9, 8, 15)
    assert len(ACTIONS) == 32
    assert RESERVED_ACTIONS <= ACTIONS


def test_known_and_unknown_actions() -> None:
    assert is_known_action("write.create")
    assert not is_known_action("write.delete")
    assert not is_known_action("ext.probe.ping")
    assert is_known_action("ext.probe.ping", {"ext.probe.ping"})
    assert not is_known_action("ext.Bad.ping", {"ext.Bad.ping"})
    with pytest.raises(GraphCliError) as info:
        require_known_action("maint.ontology_change")  # dropped from the taxonomy
    assert info.value.code == "internal.error"


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("read.get", "read"),
        ("write.supersede", "write"),
        ("maint.ontology_release", "schema_admin"),
        ("maint.migrate", "schema_admin"),
        ("maint.graph_create", "graph_admin"),
        ("ext.probe.state", "extension:probe"),
    ],
)
def test_capability_classes(action: str, expected: str) -> None:
    assert capability_class(action) == expected


def test_extension_state_action() -> None:
    assert extension_state_action("probe") == "ext.probe.state"
    with pytest.raises(GraphCliError):
        extension_state_action("Probe")


def test_query_is_part_of_the_read_level() -> None:
    """``graph query`` reads with its own action, so a deny rule can withhold it alone."""
    assert "read.query" in level_actions("read")
    assert "read.query" not in RESERVED_ACTIONS
    assert "read.native" not in level_actions("read")
