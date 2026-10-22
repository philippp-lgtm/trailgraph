"""describe: every term with a summary carries its deprecated flag (B11.5)."""

from typing import Any

from support.fixtures import compiled_model, deprecated_model
from support.skills import graph_entry

from graph_cli.services.describe import describe_lifecycle, describe_model


def by(items: Any, key: str) -> dict[str, bool]:
    return {item[key]: item["deprecated"] for item in items}


def test_the_flag_follows_the_model() -> None:
    model = deprecated_model()
    documents = describe_model(graph_entry(model), model)
    assert by(documents["types"]["types"], "curie") == {
        "inv:Category": False,
        "inv:Item": True,
        "inv:Location": False,
    }
    item = documents["type"]["inv:Item"]["type"]
    assert item["deprecated"] is True
    assert by(item["fields"], "name")["name"] is True
    assert not any(f["deprecated"] for f in item["fields"] if f["name"] != "name")
    assert by(item["relations_out"], "curie") == {
        "inv:in_category": False,
        "inv:located_in": True,
        "inv:part_of": False,
    }
    assert by(item["rules"], "id") == {"inv.serial_single_unit": True}
    assert by(item["lifecycles"], "lifecycle") == {"inv:ItemLifecycle": False}
    location = documents["type"]["inv:Location"]["type"]
    assert by(location["relations_in"], "curie")["inv:located_in"] is True
    relations = by(documents["relations"]["relations"], "curie")
    assert relations["inv:located_in"] is True
    assert sum(relations.values()) == 1


def test_lifecycles_carry_the_flag() -> None:
    model = compiled_model("inventory")
    [lifecycle] = describe_lifecycle(graph_entry(model), model, "inv:Item")["lifecycles"]
    assert lifecycle["deprecated"] is False
