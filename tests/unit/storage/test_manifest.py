"""``manifest.toml`` codec (contracts B12, C.8, V.3, V.4; plan M1)."""

import tomllib
from datetime import UTC, datetime
from uuid import UUID

import pytest
from support.fixtures import GOLDENS, assert_golden

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GraphId, TermRef
from graph_cli.kernel.ports.catalog import GraphEntry, ManifestData, StoreInfo
from graph_cli.kernel.records import OntologyPin
from graph_cli.storage.files.manifest import format_manifest, parse_manifest
from graph_cli.storage.files.tomlw import dumps

GRAPH_ID = GraphId(UUID("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a"))
HASH = "sha256:" + "5d0e" * 16


def manifest(
    *, title: str | None = 'Example "graph"\tone', language: str | None = None
) -> ManifestData:
    entry = GraphEntry(
        id=GRAPH_ID,
        name="example_graph",
        state="active",
        pin=OntologyPin("inventory", "1.1.0", HASH),
        alias_prefixes={TermRef("inv:Item"): "ITEM", TermRef("inv:Location"): "LOC"},
        extensions=(),
        title=title,
        language=language,
    )
    return ManifestData(
        entry=entry,
        created_at=datetime(2026, 10, 14, 19, 0, tzinfo=UTC),
        store=StoreInfo("files", 1),
    )


def test_round_trip_and_golden() -> None:
    data = format_manifest(manifest())
    assert_golden(GOLDENS / "storage" / "manifest.toml", data)
    parsed, warnings = parse_manifest(data, source="manifest.toml")
    assert parsed == manifest()
    assert warnings == ()


def test_key_order_is_fixed() -> None:
    text = format_manifest(manifest()).decode()
    headers = [line for line in text.splitlines() if line.startswith("[")]
    assert headers == ["[graph]", "[ontology]", "[store]", "[alias.prefixes]", "[extensions]"]
    assert text.startswith("format_version = 1\n")


def test_title_is_optional() -> None:
    parsed, _ = parse_manifest(format_manifest(manifest(title=None)), source="m")
    assert parsed.entry.title is None


def test_language_round_trip() -> None:
    data = format_manifest(manifest(language="de-CH"))
    assert '\nlanguage = "de-CH"\nstate = ' in data.decode()  # after title (C.8)
    parsed, warnings = parse_manifest(data, source="m")
    assert parsed == manifest(language="de-CH")
    assert warnings == ()


def test_manifest_without_language_loads_unchanged() -> None:
    # Graphs created before Q18 stage 2 have no key: no default, no migration.
    data = (GOLDENS / "storage" / "manifest.toml").read_bytes()
    assert b"language" not in data
    parsed, warnings = parse_manifest(data, source="m")
    assert parsed.entry.language is None
    assert format_manifest(parsed) == data
    assert warnings == ()


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("7", "must be a string"),
        ('"de_CH"', "well-formed"),
        ('"x-foo"', "well-formed"),
        ('""', "well-formed"),
        ('"de-ch"', "canonical case ('de-CH')"),
    ],
)
def test_bad_language_is_config_invalid(value: str, message: str) -> None:
    text = format_manifest(manifest(language="de-CH")).decode()
    text = text.replace('language = "de-CH"', f"language = {value}", 1)
    with pytest.raises(GraphCliError) as caught:
        parse_manifest(text.encode(), source="m")
    assert (caught.value.code, caught.value.details["key"]) == ("config.invalid", "graph.language")
    assert message in caught.value.message


@pytest.mark.parametrize(
    "text",
    ["plain", 'quote " and \\ backslash', "tab\tnew\nline", "control \x01 \x7f", "umlaut äöü 🙂"],
)
def test_writer_escapes_strings(text: str) -> None:
    rendered = dumps([(None, [("key", text), ("weird key.x", ("a", text))])])
    assert tomllib.loads(rendered) == {"key": text, "weird key.x": ["a", text]}


def test_unknown_keys_are_warnings() -> None:
    data = format_manifest(manifest()).decode()
    data = data.replace("[graph]\n", '[graph]\ncolour = "blue"\n') + "\n[later]\nx = 1\n"
    parsed, warnings = parse_manifest(data.encode(), source="graphs/g/manifest.toml")
    assert parsed == manifest()
    assert [(w.code, w.details["key"]) for w in warnings] == [
        ("config.unknown_key", "later"),
        ("config.unknown_key", "graph.colour"),
    ]


def test_unknown_major_is_unsupported() -> None:
    data = format_manifest(manifest()).replace(b"format_version = 1\n", b"format_version = 2\n", 1)
    with pytest.raises(GraphCliError) as caught:
        parse_manifest(data, source="m")
    assert caught.value.code == "format.unsupported_version"


@pytest.mark.parametrize(
    ("old", "new", "key"),
    [
        ("format_version = 1\n", "", "format_version"),
        ('name = "example_graph"', 'name = "Example"', "graph.name"),
        ('state = "active"', 'state = "frozen"', "graph.state"),
        ('id = "0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a"', 'id = "nope"', "graph.id"),
        (
            'id = "0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a"',
            'id = "0192F0A1-7C3E-7A10-8B2C-1F0E5D6C7B8A"',
            "graph.id",
        ),
        (
            'created_at = "2026-10-14T19:00:00.000Z"',
            'created_at = "2026-10-14"',
            "graph.created_at",
        ),
        (
            'created_at = "2026-10-14T19:00:00.000Z"',
            'created_at = "2026-10-14T21:00:00+02:00"',
            "graph.created_at",
        ),
        ('bundle = "inventory"', "bundle = 3", "ontology.bundle"),
        (f'artifact_hash = "{HASH}"', 'artifact_hash = "md5:1"', "ontology.artifact_hash"),
        ("format_version = 1\n\n[alias", "format_version = true\n\n[alias", "store.format_version"),
        ('"inv:Item" = "ITEM"', '"inv:Item" = "item"', "alias.prefixes.inv:Item"),
        ('"inv:Item" = "ITEM"', '"Item" = "ITEM"', "alias.prefixes.Item"),
        ("enabled = []", 'enabled = "x"', "extensions.enabled"),
        ("[ontology]", "ontology = 1\n[ontology_x]", "ontology"),
        ('title = "', 'title = 7\nx = "', "graph.title"),
        ("[store]", "[store]\n[store]", ""),
    ],
)
def test_defects_are_config_invalid(old: str, new: str, key: str) -> None:
    text = format_manifest(manifest()).decode()
    assert old in text
    with pytest.raises(GraphCliError) as caught:
        parse_manifest(text.replace(old, new, 1).encode(), source="graphs/g/manifest.toml")
    assert caught.value.code == "config.invalid"
    assert caught.value.details["key"] == key
    assert caught.value.details["file"] == "graphs/g/manifest.toml"


def test_not_utf8_is_config_invalid() -> None:
    with pytest.raises(GraphCliError) as caught:
        parse_manifest(b"\xff", source="m")
    assert caught.value.code == "config.invalid"
