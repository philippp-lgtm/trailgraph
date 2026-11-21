"""The pure helpers of ``services.catalog_access``: how a decision becomes the ``missing`` a caller
is told (the resource format that ``describe`` prints as JSON)."""

from graph_cli.kernel.authz import Decision
from graph_cli.services.catalog_access import (
    Classified,
    Missing,
    first_denied,
    missing_of,
    relative_resource,
)

GRAPH = "0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a"


def decision(effect: str, action: str, resource: str) -> Decision:
    return Decision(
        effect,  # type: ignore[arg-type]
        action,  # type: ignore[arg-type]
        resource,
        "rule",
        "sha256:" + "0" * 64,
        code="access.denied" if effect == "deny" else None,
    )


def test_a_resource_is_given_relative_to_the_graph() -> None:
    assert relative_resource(f"graph:{GRAPH}") == "graph"
    assert relative_resource(f"graph:{GRAPH}/type:ex:T") == "type:ex:T"
    assert relative_resource(f"graph:{GRAPH}/type:ex:T/attr:a") == "type:ex:T/attr:a"
    assert relative_resource(f"graph:{GRAPH}/type:ex:T/rel:ex:r") == "type:ex:T/rel:ex:r"
    assert relative_resource("installation") == "installation"  # another shape is left as it is


def test_a_denial_becomes_a_missing_right_and_prints_as_text_and_json() -> None:
    found = missing_of(decision("deny", "write.create", f"graph:{GRAPH}/type:ex:T"))
    assert found == Missing("write.create", "type:ex:T")
    assert str(found) == "write.create on type:ex:T"
    assert found.to_json() == {"action": "write.create", "resource": "type:ex:T"}
    assert Missing("read.function").to_json() == {"action": "read.function", "resource": "graph"}


def test_the_first_denying_decision_is_the_one_kept() -> None:
    allow = decision("allow", "write.update", f"graph:{GRAPH}/type:ex:T")
    first = decision("deny", "write.update", f"graph:{GRAPH}/type:ex:T/attr:a")
    second = decision("deny", "write.update", f"graph:{GRAPH}/type:ex:T/attr:b")
    assert first_denied([allow, first, second]) == Missing("write.update", "type:ex:T/attr:a")
    assert first_denied([allow]) is None
    assert first_denied([]) is None


def test_an_entry_is_allowed_exactly_when_it_misses_nothing() -> None:
    assert Classified("x").allowed and Classified("x").missing == ()
    missing = (Missing("read.find"),)
    denied = Classified("x", missing)
    assert not denied.allowed
    assert denied.missing_json() == [{"action": "read.find", "resource": "graph"}]
