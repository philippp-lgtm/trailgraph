"""Strict YAML loader (contracts C.7)."""

import pytest

from graph_cli.ontology_layer.yaml_strict import (
    YamlError,
    YamlMap,
    YamlScalar,
    YamlSeq,
    load_strict,
)


def test_tree_keeps_scalar_text_and_style() -> None:
    root = load_strict("version: 1.0\nflag: no\nquoted: 'true'\nlist: [1, two]\n")
    assert isinstance(root, YamlMap)
    version = root.items["version"]
    assert isinstance(version, YamlScalar)
    assert (version.text, version.plain) == ("1.0", True)
    flag = root.items["flag"]
    assert isinstance(flag, YamlScalar)
    assert flag.as_bool() is None  # YAML 1.1 "no" is not a boolean in 1.2 core
    quoted = root.items["quoted"]
    assert isinstance(quoted, YamlScalar)
    assert not quoted.plain and quoted.as_bool() is None
    items = root.items["list"]
    assert isinstance(items, YamlSeq)
    assert [i.text for i in items.items if isinstance(i, YamlScalar)] == ["1", "two"]
    assert root.key_lines["list"] == 4


def test_core_schema_conversions() -> None:
    root = load_strict("a: true\nb: FALSE\nc: -12\nd: ~\ne: 0x1F\nf: '7'\n")
    assert isinstance(root, YamlMap)
    scalars = {k: v for k, v in root.items.items() if isinstance(v, YamlScalar)}
    assert scalars["a"].as_bool() is True
    assert scalars["b"].as_bool() is False
    assert scalars["c"].as_int() == -12
    assert scalars["d"].is_null()
    assert scalars["e"].as_int() is None
    assert scalars["f"].as_int() is None


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("a: 1\na: 2\n", "duplicate key"),
        ("a: &x 1\nb: *x\n", "anchors"),
        ("a: !!str 1\n", "explicit tags"),
        ("? [a, b]\n: 1\n", "keys must be scalars"),
        ("a: 1\n---\nb: 2\n", "one YAML document"),
        ("a: [1, 2\n", "line"),
    ],
)
def test_refused(text: str, message: str) -> None:
    with pytest.raises(YamlError, match=message):
        load_strict(text)


def test_flow_mapping_entry_without_value_is_refused() -> None:
    text = "values:\n  x: {summary: Used, still works.}\n"
    with pytest.raises(YamlError, match=r"'still works\.' has no value") as info:
        load_strict(text)
    assert (info.value.line, info.value.column) == (2, 22)
    assert str(info.value).startswith("line 2, column 22: ")
    with pytest.raises(YamlError, match="'b' has no value"):
        load_strict("a: {b:}\n")


def test_flow_mappings_with_values_and_quoted_commas_load() -> None:
    root = load_strict("a: {x: null, y: ~, z: ''}\nb: {summary: 'Used, still works.'}\nc:\n")
    assert isinstance(root, YamlMap)
    a = root.items["a"]
    assert isinstance(a, YamlMap)
    assert all(isinstance(v, YamlScalar) and v.is_null() for k, v in a.items.items() if k != "z")
    b = root.items["b"]
    assert isinstance(b, YamlMap)
    summary = b.items["summary"]
    assert isinstance(summary, YamlScalar) and summary.text == "Used, still works."
    assert isinstance(root.items["c"], YamlScalar)  # block mapping: an empty value stays null


def test_empty_and_invalid_utf8() -> None:
    assert load_strict("") is None
    with pytest.raises(YamlError):
        load_strict(b"a: \xff\n")
