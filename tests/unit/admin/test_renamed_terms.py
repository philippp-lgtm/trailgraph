"""``renamed_terms``: which rules name a term a release renames, and which deny rules would
fail open (migration plan M8). Resources are matched the way the authorizer matches them."""

from collections.abc import Mapping

from support.fixtures import compiled_model

from graph_cli.access_control.terms import RuleEntry
from graph_cli.ontology_layer.diff import Renames, derive_renames
from graph_cli.ontology_layer.migration import Retype
from graph_cli_admin.services_admin import error_codes
from graph_cli_admin.services_admin.renamed_terms import hazards, probes, refusal

GRAPH = "0192f3c0-11aa-7e01-b2c3-d4e5f6a7b8c9"
P = f"graph:{GRAPH}/type:"
OLD, NEW = compiled_model("inventory", "1.1.0"), compiled_model("inventory", "2.0.0")
ATTRIBUTES = derive_renames(OLD, NEW)  # inv:Item.name -> label


def deny(
    resource: str, *, rule: str = "r", actions: tuple[str, ...] = ("write.update",)
) -> RuleEntry:
    return RuleEntry("graph", rule, "deny", resource, actions)


def renames(
    classes: Mapping[str, str] | None = None, relations: Mapping[str, str] | None = None
) -> Renames:
    return Renames(classes or {}, relations or {}, {})


def test_an_attribute_rename_derived_from_the_models_is_found() -> None:
    assert ATTRIBUTES.attributes == {("inv:Item", "name"): "label"}
    [probe] = probes([deny(f"{P}inv:Item/attr:name")], GRAPH, ATTRIBUTES, (), NEW)
    assert (probe.kind, probe.source, probe.target) == (
        "attribute",
        "inv:Item.name",
        "inv:Item.label",
    )
    assert probe.target_resource == f"{P}inv:Item/attr:label" and probe.covered is False
    assert hazards([deny(f"{P}inv:Item/attr:name")], GRAPH, ATTRIBUTES, (), NEW) != ()


def test_a_rule_naming_the_new_term_covers_the_old_one() -> None:
    old, new = deny(f"{P}inv:Item/attr:name", rule="a"), deny(f"{P}inv:Item/attr:label", rule="b")
    assert hazards([old, new], GRAPH, ATTRIBUTES, (), NEW) == ()
    # for the same actions at least: a rule on other actions does not cover
    other = deny(f"{P}inv:Item/attr:label", rule="b", actions=("read.get",))
    assert hazards([old, other], GRAPH, ATTRIBUTES, (), NEW) != ()
    broader = deny(f"{P}inv:Item/attr:label", rule="b", actions=("write.*",))
    assert hazards([old, broader], GRAPH, ATTRIBUTES, (), NEW) == ()


def test_unrenamed_terms_and_other_graphs_are_not_touched() -> None:
    entries = [
        deny(f"{P}inv:Item/attr:quantity"),
        deny(f"{P}inv:Location"),
        deny("graph:other/type:inv:Item/attr:name"),
        deny("graph:*", rule="scope"),
        deny(f"graph:{GRAPH}*", rule="scope_graph"),
        deny(f"{P}*", rule="all_types"),
    ]
    assert probes(entries, GRAPH, ATTRIBUTES, (), NEW) == ()


def test_a_class_rename_carries_its_terms_along() -> None:
    moved = renames({"inv:Item": "stock:Item"})
    entries = [
        deny(f"{P}inv:Item"),
        deny(f"{P}inv:Item/attr:quantity"),
        deny(f"{P}inv:Item/rel:inv:part_of"),
    ]
    found = probes(entries, GRAPH, moved, (), NEW)
    assert {p.target_resource for p in found} == {
        f"{P}stock:Item",
        f"{P}stock:Item/attr:quantity",
        f"{P}stock:Item/rel:inv:part_of",
    }
    assert {p.kind for p in found} == {"class"}


def test_a_relation_rename() -> None:
    found = hazards(
        [deny(f"{P}inv:Item/rel:inv:located_in", actions=("write.link",))],
        GRAPH,
        renames(relations={"inv:located_in": "inv:stored_in"}),
        (),
        NEW,
    )
    [probe] = found
    assert (probe.kind, probe.source, probe.target) == (
        "relation",
        "inv:located_in",
        "inv:stored_in",
    )
    assert probe.target_resource == f"{P}inv:Item/rel:inv:stored_in"


def test_a_glob_is_a_hazard_only_when_it_stops_matching() -> None:
    moved = renames({"inv:Supplier": "sup:Supplier", "inv:Item": "inv:Product"})
    entries = [deny(f"{P}inv:*", actions=("read.get", "write.*"))]
    [probe] = hazards(entries, GRAPH, moved, (), NEW)
    assert probe.source == "inv:Supplier"  # inv:Product still matches inv:*
    # another rule covers the new prefix
    entries.append(deny(f"{P}sup:*", rule="sup", actions=("read.get", "write.*")))
    assert hazards(entries, GRAPH, moved, (), NEW) == ()


def test_a_retype_is_a_rename_with_an_attribute_map() -> None:
    retype = Retype("inv:Item", "inv:Product", {"name": "label"})
    found = probes(
        [deny(f"{P}inv:Item/attr:name"), deny(f"{P}inv:Item")], GRAPH, renames(), (retype,), NEW
    )
    assert {(p.kind, p.source, p.target) for p in found} == {
        ("attribute", "inv:Item.name", "inv:Product.label"),
        ("class", "inv:Item", "inv:Product"),
    }


def test_allow_rules_are_listed_but_never_a_hazard() -> None:
    allow = RuleEntry(
        "installation", "grant.0.reads", "allow", f"{P}inv:Item/attr:name", ("read.get",)
    )
    assert [p.entry.effect for p in probes([allow], GRAPH, ATTRIBUTES, (), NEW)] == ["allow"]
    assert hazards([allow], GRAPH, ATTRIBUTES, (), NEW) == ()
    assert probes([allow], GRAPH, ATTRIBUTES, (), NEW)[0].to_json()["covered"] is None


def test_the_refusal_names_the_rules_and_the_way_out() -> None:
    found = hazards([deny(f"{P}inv:Item/attr:name", rule="no_names")], GRAPH, ATTRIBUTES, (), NEW)
    error_codes.register()
    error = refusal(found, {"from": "1.1.0", "to": "2.0.0"})
    assert error.code == "admin.policy_renamed_term"
    assert "no_names" in error.message and "names both" in error.message
    assert error.details["count"] == 1
