"""Base types ``blob`` and ``local_path``: shapes, model rules, export (files slice 1, step 1)."""

from typing import Any

import pytest
from jsonschema import Draft202012Validator
from support.writes import codes, judge

from graph_cli.kernel.canonical import JsonValue
from graph_cli.ontology_layer.bundle import LayeredSource, MappingSource
from graph_cli.ontology_layer.compiler import CompileOutcome, compile_bundle
from graph_cli.ontology_layer.exporters.jsonschema import export_jsonschema
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.ontology_layer.values import blob_problem, coerce_text, local_path_problem

HASH = "sha256:" + "ab" * 32

_MODULE = """\
format: graph_cli.ir/1
module:
  id: example_module
  prefix: ex
  version: 1.0.0
  summary: Things with stored content and a place they came from.
classes:
  Thing:
    summary: Something that is tracked.
    attributes:
      name:
        summary: Display name.
        type: string
        required: true
      content:
        summary: Stored bytes.
        type: blob
        unique: true
      origin:
        summary: Where the thing came from.
        type: local_path
"""


def _compile(text: str) -> CompileOutcome:
    return compile_bundle(
        "example_module", LayeredSource(MappingSource({"example_module": text.encode()}))
    )


def _model() -> CompiledModel:
    outcome = _compile(_MODULE)
    assert outcome.model is not None, [f.to_json() for f in outcome.findings]
    return outcome.model


@pytest.mark.parametrize(
    "value",
    [
        {"root": "notes", "path": "scans/report 2024.pdf"},
        {"root": "a", "path": "x"},
        {"root": "notes_2", "path": "Ünïcödé/naïve.txt"},
        {"root": "n" + "a" * 62, "path": "a/" * 2047 + "b"},
    ],
)
def test_valid_local_paths(value: JsonValue) -> None:
    assert local_path_problem(value) is None


@pytest.mark.parametrize(
    "value",
    [
        "notes/scans/a.pdf",
        {"root": "notes"},
        {"root": "notes", "path": "a", "extra": 1},
        {"root": "Notes", "path": "a"},
        {"root": "1notes", "path": "a"},
        {"root": "no-tes", "path": "a"},
        {"root": "n" * 64, "path": "a"},
        {"root": "notes", "path": ""},
        {"root": "notes", "path": 3},
        {"root": "notes", "path": "/abs/a.pdf"},
        {"root": "notes", "path": "../a.pdf"},
        {"root": "notes", "path": "a/../b"},
        {"root": "notes", "path": "a/./b"},
        {"root": "notes", "path": "./a"},
        {"root": "notes", "path": "a//b"},
        {"root": "notes", "path": "a/"},
        {"root": "notes", "path": "a\\b"},
        {"root": "notes", "path": "a\x00b"},
        {"root": "notes", "path": "a" * 4097},
    ],
)
def test_invalid_local_paths(value: JsonValue) -> None:
    assert local_path_problem(value) is not None


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        (HASH, True),
        ("sha256:" + "AB" * 32, False),
        ("sha256:" + "ab" * 31, False),
        ("sha512:" + "ab" * 32, False),
        ("ab" * 32, False),
        ({"hash": HASH}, False),
    ],
)
def test_blob_values(value: JsonValue, ok: bool) -> None:
    assert (blob_problem(value) is None) == ok


def test_text_coercion_passes_blobs_and_refuses_local_paths() -> None:
    assert coerce_text("blob", HASH) == HASH
    with pytest.raises(ValueError, match="cannot be given as text"):
        coerce_text("local_path", "notes/a.pdf")


def test_blob_may_be_unique_and_local_path_may_not() -> None:
    assert _model().classes["ex:Thing"].attributes["content"].unique
    outcome = _compile(
        _MODULE.replace("type: local_path", "type: local_path\n        unique: true")
    )
    assert [(f.code, f.target["path"]) for f in outcome.findings] == [
        ("model.semantic.unique_on_non_scalar", "classes.Thing.attributes.origin.unique")
    ]


@pytest.mark.parametrize(("base", "default"), [("blob", HASH), ("local_path", "x")])
def test_file_bases_take_no_default(base: str, default: str) -> None:
    text = _MODULE.replace(
        "        type: string\n        required: true",
        f"        type: {base}\n        default: '{default}'",
    )
    outcome = _compile(text)
    assert [(f.code, f.target["path"]) for f in outcome.findings] == [
        ("model.structural.invalid_default", "classes.Thing.attributes.name.default")
    ]


def test_datatypes_over_blob_take_no_facets() -> None:
    text = _MODULE + (
        "datatypes:\n  Digest:\n    summary: A digest.\n    base: blob\n    max_length: 80\n"
    )
    outcome = _compile(text)
    assert [(f.code, f.target["path"]) for f in outcome.findings] == [
        ("model.structural.facet_not_applicable", "datatypes.Digest.max_length")
    ]


def test_qualifiers_refuse_datatypes_over_file_bases() -> None:
    text = _MODULE + (
        "datatypes:\n  Where:\n    summary: A place.\n    base: local_path\n"
        "relations:\n  near:\n    summary: Close by.\n    domain: Thing\n    range: Thing\n"
        "    qualifiers:\n      where: {summary: Where., type: Where}\n"
    )
    outcome = _compile(text)
    assert [(f.code, f.target["path"]) for f in outcome.findings] == [
        ("model.semantic.base_not_applicable", "relations.near.qualifiers.where.type")
    ]


def _thing(**attrs: Any) -> dict[str, Any]:
    return {"op": "create", "type": "ex:Thing", "local": "x", "attrs": {"name": "T", **attrs}}


def test_data_shapes_are_checked() -> None:
    model = _model()
    good = {"root": "notes", "path": "a b/c.pdf"}
    assert codes(judge(model, _thing(content=HASH, origin=good))[1]) == []
    for attrs in (
        {"content": "sha256:" + "AB" * 32},
        {"content": 7},
        {"origin": {"root": "notes", "path": "../c.pdf"}},
        {"origin": "notes/c.pdf"},
        {"origin": [good]},
    ):
        found = codes(judge(model, _thing(**attrs))[1])
        assert found in (
            ["data.structural.datatype_mismatch"],
            ["data.structural.cardinality_many"],
        ), attrs


def test_json_schema_states_the_shapes() -> None:
    schema = export_jsonschema(_model())["ex.Thing.json"]
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    good = {"name": "T", "content": HASH, "origin": {"root": "notes", "path": "a.pdf"}}
    assert list(validator.iter_errors(good)) == []
    for bad in (
        {**good, "content": "sha256:" + "AB" * 32},
        {**good, "origin": {"root": "Notes", "path": "a.pdf"}},
        {**good, "origin": {"root": "notes"}},
        {**good, "origin": {"root": "notes", "path": "a.pdf", "x": 1}},
    ):
        assert list(validator.iter_errors(bad)), bad
    properties: Any = schema["properties"]
    assert properties["content"]["x-graph_cli-base"] == "blob"
    assert properties["origin"]["x-graph_cli-base"] == "local_path"
