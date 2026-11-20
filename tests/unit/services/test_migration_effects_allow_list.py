"""The migration item of a reader limited to some attributes keeps effects by an allow list
(field visibility plan R4; review fixes of stage 4): an effect kind that carries no attribute
stays, the attribute-bearing kinds are judged per attribute, and any other kind, one a later
version adds included, is left out."""

from types import SimpleNamespace
from typing import Any, get_args

from support.snapshots import eid, revision

from graph_cli.kernel.read_view import ReadView
from graph_cli.kernel.records import Revision
from graph_cli.ontology_layer.migration import Step
from graph_cli.services.read import migration_effects
from graph_cli.services.read.migration_effects import attribute_effects

EMPLOYEE = "stf:Employee"
VIEW = SimpleNamespace(read_view=ReadView.of(None, {EMPLOYEE: frozenset({"name", "shift"})}))


class Names:
    """The pin's names are those the migration wrote: nothing is renamed after it."""

    def unmapped(self, probe: Revision) -> bool:
        return False

    def map(self, probe: Revision) -> Revision:
        return probe


def mapping(written: Revision | None) -> Any:
    return SimpleNamespace(
        raw=SimpleNamespace(get_revision=lambda entity, rev: written), names=Names()
    )


def effects(applied: list[Any], written: Revision | None) -> list[Any]:
    return attribute_effects(VIEW, mapping(written), eid("e"), 2, applied)  # type: ignore[arg-type]


WRITTEN = revision("e", EMPLOYEE, {"name": "Ada", "shift": "day", "email": "a@example.org"}, rev=2)
FREE = [
    {"kind": "rename_class", "from": "stf:Worker", "to": EMPLOYEE},
    {"kind": "rename_relation", "from": "stf:in", "to": "stf:member_of"},
    {"kind": "rename_qualifier", "relation": "stf:member_of", "from": "r", "to": "role"},
    {
        "kind": "map_qualifier_values",
        "relation": "stf:member_of",
        "qualifier": "role",
        "values": {},
    },
    {"kind": "drop_qualifier", "relation": "stf:member_of", "qualifier": "since"},
    {"kind": "drop_edges", "relation": "stf:old"},
]


def test_effects_without_attributes_stay() -> None:
    assert effects(FREE, WRITTEN) == FREE
    assert effects(FREE, None) == FREE  # nothing to probe: still nothing to hide


def test_an_unknown_effect_kind_is_left_out() -> None:
    unknown = {"kind": "split_attribute", "type": EMPLOYEE, "attribute": "email", "into": "x"}
    assert effects([unknown, FREE[0], {"no": "kind"}, "not an object"], WRITTEN) == [FREE[0]]


def test_attribute_effects_are_judged_per_attribute() -> None:
    shown = {"kind": "fill", "type": EMPLOYEE, "attribute": "shift", "value": "day", "index": 0}
    hidden = {"kind": "convert", "type": EMPLOYEE, "attribute": "email", "to": "many", "index": 1}
    renamed = {"kind": "rename_attribute", "class": EMPLOYEE, "from": "nick", "to": "name"}
    secret = {"kind": "rename_attribute", "class": EMPLOYEE, "from": "mail", "to": "email"}
    dropped = {"kind": "drop", "type": EMPLOYEE, "attribute": "shift"}
    retype = {
        "kind": "retype",
        "from": "stf:Worker",
        "to": EMPLOYEE,
        "attributes": {"nick": "name", "mail": "email"},
    }
    found = effects([shown, hidden, renamed, secret, dropped, retype], WRITTEN)
    assert found == [shown, renamed, {**retype, "attributes": {"nick": "name"}}]


def test_every_effect_kind_a_migration_writes_has_a_decision() -> None:
    """A new step kind must be placed in one of the lists on purpose (or it stays hidden)."""
    steps = {step.NAME for step in get_args(Step)}
    kinds = steps | {"rename_class", "rename_relation", "rename_attribute"}
    judged = {"rename_attribute", "retype", "drop"} | migration_effects.ATTRIBUTE_STEPS
    assert kinds == migration_effects.ATTRIBUTE_FREE_EFFECTS | judged
