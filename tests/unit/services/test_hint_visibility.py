"""Rename hints for a type limited reader (migration plan M7, stage 3 read limits).

A hint is dropped when its current name is hidden from the reader, and also when any step of
its chain passes through a term of the model that the reader cannot see: the chain would reveal
that the hidden term exists. Names on the way that no term of the model carries stay.
"""

from support.fixtures import compiled_model

from graph_cli.services.names import known_in
from graph_cli.services.term_model import concrete_types
from graph_cli.services.visibility import filter_model
from graph_cli.temporal_knowledge.name_map import RenameHint, RenameStep

FULL = compiled_model("inventory", "1.1.0")
SEEN = filter_model(FULL, concrete_types(FULL.classes) - {"inv:Supplier"})


def chain(*steps: RenameStep) -> RenameHint:
    return RenameHint("old", steps)


def test_a_chain_through_a_hidden_class_is_dropped() -> None:
    hint = chain(RenameStep("inv:Supplier", "2.0.0"), RenameStep("inv:Item", "3.0.0"))
    assert known_in(SEEN)("class", hint)  # judged by the last name alone: the leak
    assert not known_in(SEEN, FULL)("class", hint)
    assert known_in(FULL, FULL)("class", hint)  # nothing hidden: kept


def test_a_name_on_the_way_that_no_term_carries_is_fine() -> None:
    hint = chain(RenameStep("inv:Gone", "2.0.0"), RenameStep("inv:Item", "3.0.0"))
    assert known_in(SEEN, FULL)("class", hint)


def test_a_chain_of_an_attribute_through_a_hidden_owner_is_dropped() -> None:
    hint = chain(
        RenameStep("title", "2.0.0", owner="inv:Supplier"),
        RenameStep("name", "3.0.0", owner="inv:Item"),
    )
    assert known_in(SEEN)("attribute", hint)
    assert not known_in(SEEN, FULL)("attribute", hint)
    visible = chain(RenameStep("title", "2.0.0", owner="inv:Gone"), hint.steps[-1])
    assert known_in(SEEN, FULL)("attribute", visible)


def test_a_chain_of_a_relation_through_a_hidden_relation_is_dropped() -> None:
    assert "inv:supplied_by" in FULL.relations and "inv:supplied_by" not in SEEN.relations
    hint = chain(RenameStep("inv:supplied_by", "2.0.0"), RenameStep("inv:located_in", "3.0.0"))
    assert known_in(SEEN)("relation", hint)
    assert not known_in(SEEN, FULL)("relation", hint)
