"""JSON Schema export: valid draft 2020-12 documents that accept and reject fixture attrs."""

import pytest
from jsonschema import Draft202012Validator
from support.fixtures import TOY_ONTOLOGIES, compiled_model

from graph_cli.kernel.canonical import canonical_bytes
from graph_cli.ontology_layer.exporters.jsonschema import (
    ROLE_KEYWORD,
    export_jsonschema,
    schema_file_name,
)


@pytest.mark.parametrize("name", TOY_ONTOLOGIES)
def test_every_concrete_class_has_a_valid_schema(name: str) -> None:
    model = compiled_model(name)
    files = export_jsonschema(model)
    concrete = {c for c, cls in model.classes.items() if not cls.abstract}
    assert set(files) == {schema_file_name(c) for c in concrete}
    for schema in files.values():
        Draft202012Validator.check_schema(schema)
        canonical_bytes(schema)  # JSON data model only (no floats)


def validator(name: str, curie: str) -> Draft202012Validator:
    return Draft202012Validator(export_jsonschema(compiled_model(name))[schema_file_name(curie)])


def test_inventory_item_attrs() -> None:
    item = validator("inventory", "inv:Item")
    valid = {"name": "Drill", "quantity": 1, "status": "ordered", "serial_number": "SN-0001"}
    assert list(item.iter_errors(valid)) == []
    for bad in (
        {**valid, "quantity": -1},
        {**valid, "serial_number": "bad serial"},
        {**valid, "condition": "broken"},
        {**valid, "status": "lost"},
        {**valid, "colour": "red"},
        {key: value for key, value in valid.items() if key != "name"},
    ):
        assert list(item.iter_errors(bad)), bad


def test_inherited_attributes_and_dates() -> None:
    page = validator("notes_and_sources", "nas:WebPage")
    valid = {"title": "Home", "url": "https://example.org/", "retrieved_at": "2026-10-14"}
    assert list(page.iter_errors(valid)) == []
    assert list(page.iter_errors({**valid, "retrieved_at": "14.10.2026"}))


def test_defaults_and_descriptions_are_exported() -> None:
    schema = export_jsonschema(compiled_model("inventory"))["inv.Item.json"]
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert properties["quantity"] == {
        "type": "integer",
        "minimum": 0,
        "maximum": 2**53 - 1,
        "description": "Number of units.",
        "default": 1,
    }
    assert schema["$id"] == "urn:graph_cli:onto:inventory/Item/attrs"
    assert schema["$comment"] == "attrs of urn:graph_cli:onto:inventory#Item"


def test_label_roles_are_annotations() -> None:
    files = export_jsonschema(compiled_model("notes_and_sources"))
    topic = files[schema_file_name("nas:Topic")]["properties"]
    assert isinstance(topic, dict)
    assert topic["label"][ROLE_KEYWORD] == "label"  # type: ignore[index]
    assert topic["synonyms"][ROLE_KEYWORD] == "alt_label"  # type: ignore[index]
    assert topic["synonyms"]["type"] == "array"  # type: ignore[index]
    assert topic["misspellings"][ROLE_KEYWORD] == "hidden_label"  # type: ignore[index]
    assert ROLE_KEYWORD not in topic["description"]  # type: ignore[operator]
    page = files[schema_file_name("nas:WebPage")]["properties"]
    assert page["title"][ROLE_KEYWORD] == "label"  # type: ignore[index]  # inherited
    # An annotation, not an assertion: values validate as before.
    attrs = {"label": "Physics", "synonyms": ["Natural philosophy"]}
    assert list(validator("notes_and_sources", "nas:Topic").iter_errors(attrs)) == []
