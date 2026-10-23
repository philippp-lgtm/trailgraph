"""describe: every term with a summary carries its deprecated flag (B11.5)."""

from dataclasses import replace
from typing import Any

from support.fixtures import compiled_model, deprecated_model
from support.skills import graph_entry

from graph_cli.query.cypher.parser import parse
from graph_cli.query.cypher.resolve import resolve
from graph_cli.services.describe import (
    describe_fields,
    describe_lifecycle,
    describe_model,
    describe_schema,
    describe_type,
)


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


def test_schema_writes_names_as_a_query_needs_them() -> None:
    model = compiled_model("notes_and_sources")
    schema = describe_schema(graph_entry(model), model)
    assert schema["schema"] == "graph_cli.model/1"
    labels = {item["type"]: item for item in schema["labels"]}
    assert labels["nas:Book"]["label"] == "Book"
    assert labels["nas:Book"]["supertypes"] == ["Source"]
    assert {"name": "title", "base": "string", "many": False} in labels["nas:Book"]["properties"]
    relationships = {item["relation"]: item for item in schema["relationships"]}
    assert relationships["nas:cites"]["pattern"] == "(:Note)-[:cites]->(:Source)"
    assert relationships["nas:cites"]["properties"] == [
        {"name": "locator", "base": "string", "many": False}
    ]
    assert relationships["core:supersedes"]["pattern"] == "()-[:supersedes]->()"
    assert "id" in schema["node_fields"] and schema["relationship_fields"] == ["edge_id"]


def test_schema_backticks_names_that_are_not_unique() -> None:
    model = compiled_model("notes_and_sources")
    model = replace(model, classes={**model.classes, "provenance:Note": model.classes["nas:Note"]})
    schema = describe_schema(graph_entry(model), model)
    labels = {item["type"]: item["label"] for item in schema["labels"]}
    assert labels["nas:Note"] == "`nas:Note`" and labels["provenance:Note"] == "`provenance:Note`"
    relationships = {item["relation"]: item["pattern"] for item in schema["relationships"]}
    assert relationships["nas:cites"] == "(:`nas:Note`)-[:cites]->(:Source)"


def test_schema_patterns_parse_and_resolve() -> None:
    for name in ("notes_and_sources", "inventory"):
        model = compiled_model(name)
        for item in describe_schema(graph_entry(model), model)["relationships"]:
            resolve(parse(f"MATCH {item['pattern']} RETURN 1"), model)


def test_a_type_by_its_unique_local_name() -> None:
    model = compiled_model("notes_and_sources")
    entry = graph_entry(model)
    assert describe_type(entry, model, "Note") == describe_type(entry, model, "nas:Note")
    assert describe_fields(entry, model, "Note")["type"] == "nas:Note"
    assert describe_lifecycle(entry, model, "Note") == describe_lifecycle(entry, model, "nas:Note")
