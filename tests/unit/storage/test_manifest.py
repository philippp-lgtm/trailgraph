"""``manifest.toml`` codec (contracts B12, C.8, V.3, V.4; plan M1; format 2 with
``requires`` for stored files in an object store)."""

import tomllib
from datetime import UTC, datetime
from uuid import UUID

import pytest
from support.fixtures import GOLDENS, assert_golden

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GraphId, TermRef
from graph_cli.kernel.ports.catalog import GraphEntry, ManifestData, StoreInfo
from graph_cli.kernel.records import OntologyPin
from graph_cli.storage.files.manifest import (
    KNOWN_FEATURES,
    format_manifest,
    manifest_features,
    parse_manifest,
)
from graph_cli.storage.files.tomlw import dumps

GRAPH_ID = GraphId(UUID("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a"))
HASH = "sha256:" + "5d0e" * 16
STORE_ID = "4b0c9f3e-2d1a-4c5b-9e8f-7a6b5c4d3e2f"
S3_STORE = StoreInfo("files", 1, "s3", STORE_ID)


def manifest(
    *,
    title: str | None = 'Example "graph"\tone',
    language: str | None = None,
    store: StoreInfo | None = None,
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
        store=store or StoreInfo("files", 1),
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


@pytest.mark.parametrize("version", ["3", "0", "true", '"2"', "2.0", "1.0"])
def test_unknown_major_is_unsupported(version: str) -> None:
    data = format_manifest(manifest()).replace(
        b"format_version = 1\n", f"format_version = {version}\n".encode(), 1
    )
    with pytest.raises(GraphCliError) as caught:
        parse_manifest(data, source="m")
    assert caught.value.code == "format.unsupported_version"
    assert caught.value.details["expected"] == [1, 2]


# ---- format 2: requires and [store.blobs] ------------------------------------


def test_a_local_graph_stays_format_1_byte_for_byte() -> None:
    """The local default writes exactly what 0.21 wrote: no ``requires``, no
    ``[store.blobs]``, and a 0.21 manifest reads and writes back unchanged."""
    golden = (GOLDENS / "storage" / "manifest.toml").read_bytes()
    local = manifest(store=StoreInfo("files", 1, "local", None))
    assert format_manifest(local) == format_manifest(manifest()) == golden
    parsed, warnings = parse_manifest(golden, source="m")
    assert (parsed.store, warnings) == (StoreInfo("files", 1), ())
    assert format_manifest(parsed) == golden
    assert b"requires" not in golden
    assert b"blobs" not in golden
    assert manifest_features(parsed.store) == ()


def test_object_store_round_trip_and_golden() -> None:
    data = format_manifest(manifest(store=S3_STORE))
    assert_golden(GOLDENS / "storage" / "manifest_s3.toml", data)
    text = data.decode()
    assert text.startswith('format_version = 2\nrequires = ["blobs.s3"]\n\n[graph]\n')
    headers = [line for line in text.splitlines() if line.startswith("[")]
    assert headers == [
        "[graph]",
        "[ontology]",
        "[store]",
        "[store.blobs]",
        "[alias.prefixes]",
        "[extensions]",
    ]
    parsed, warnings = parse_manifest(data, source="m")
    assert parsed == manifest(store=S3_STORE)
    assert warnings == ()
    assert format_manifest(parsed) == data
    assert manifest_features(parsed.store) == ("blobs.s3",)


def read_as_0_21(data: bytes) -> None:
    """The first check of every manifest reader of 0.21 and older (app, server,
    maintenance, ``audit verify``, the manifest rewrite): the top-level ``format_version``
    must be exactly 1."""
    version = tomllib.loads(data.decode()).get("format_version")
    if version != 1:
        raise GraphCliError("format.unsupported_version", f"format_version {version!r}")


def test_programs_of_0_21_refuse_a_graph_in_an_object_store() -> None:
    """An image rolled back below format 2 refuses such a graph entirely instead of reading
    its files as missing local ones or rewriting its manifest without the location."""
    with pytest.raises(GraphCliError) as caught:
        read_as_0_21(format_manifest(manifest(store=S3_STORE)))
    assert caught.value.code == "format.unsupported_version"
    read_as_0_21(format_manifest(manifest()))  # a local graph stays readable by them


@pytest.mark.parametrize(
    "store", [StoreInfo("files", 1, "s3", None), StoreInfo("files", 1, "local", STORE_ID)]
)
def test_a_store_id_is_written_with_an_object_store_only(store: StoreInfo) -> None:
    """Never dropped silently, never written without its location."""
    with pytest.raises(GraphCliError) as caught:
        format_manifest(manifest(store=store))
    assert caught.value.code == "internal.error"


@pytest.mark.parametrize(
    "requires",
    ['["blobs.s3", "sealed"]', '["future.thing"]', '["sealed", "blobs.s3"]'],
)
def test_an_unknown_feature_is_unsupported_and_named(requires: str) -> None:
    text = format_manifest(manifest(store=S3_STORE)).decode()
    text = text.replace('requires = ["blobs.s3"]', f"requires = {requires}", 1)
    text = text.replace("[ontology]", "[ontology]\nsealed_key = 1", 1)  # unknown: not reached
    with pytest.raises(GraphCliError) as caught:
        parse_manifest(text.encode(), source="graphs/g/manifest.toml")
    assert caught.value.code == "format.unsupported_version"
    feature = next(f for f in ("sealed", "future.thing") if f in requires)
    assert caught.value.details["feature"] == feature
    assert feature in caught.value.message
    assert caught.value.details["known"] == sorted(KNOWN_FEATURES)


@pytest.mark.parametrize(
    ("old", "new", "key"),
    [
        # requires and [store.blobs] disagree
        ('requires = ["blobs.s3"]\n', "", "requires"),
        ("[store.blobs]\n", "[unrelated]\n", "store.blobs"),
        ('location = "s3"', 'location = "local"', "store.blobs.location"),
        ('location = "s3"\n', "", "store.blobs.location"),
        # the shape of requires and of the store id
        ('requires = ["blobs.s3"]', "requires = []", "requires"),
        ('requires = ["blobs.s3"]', 'requires = "blobs.s3"', "requires"),
        ('requires = ["blobs.s3"]', 'requires = ["blobs.s3", "blobs.s3"]', "requires"),
        ('requires = ["blobs.s3"]', 'requires = [""]', "requires"),
        (f'store_id = "{STORE_ID}"', 'store_id = "bucket-1"', "store.blobs.store_id"),
        (f'store_id = "{STORE_ID}"', f'store_id = "{STORE_ID.upper()}"', "store.blobs.store_id"),
        (f'store_id = "{STORE_ID}"\n', "", "store.blobs.store_id"),
    ],
)
def test_format_2_defects_are_config_invalid(old: str, new: str, key: str) -> None:
    text = format_manifest(manifest(store=S3_STORE)).decode()
    assert old in text
    with pytest.raises(GraphCliError) as caught:
        parse_manifest(text.replace(old, new, 1).encode(), source="graphs/g/manifest.toml")
    assert caught.value.code == "config.invalid"
    assert caught.value.details["key"] == key


@pytest.mark.parametrize(
    ("addition", "key"),
    [
        ('requires = ["blobs.s3"]\n', "requires"),
        (None, "store.blobs"),
    ],
)
def test_format_1_with_format_2_keys_is_refused(addition: str | None, key: str) -> None:
    """A format-1 manifest never carries a location: one would be dropped silently."""
    text = format_manifest(manifest()).decode()
    if addition is not None:
        text = text.replace("format_version = 1\n", "format_version = 1\n" + addition, 1)
    else:
        text = text.replace(
            "[alias.prefixes]",
            f'[store.blobs]\nlocation = "s3"\nstore_id = "{STORE_ID}"\n\n[alias.prefixes]',
            1,
        )
    with pytest.raises(GraphCliError) as caught:
        parse_manifest(text.encode(), source="m")
    assert (caught.value.code, caught.value.details["key"]) == ("config.invalid", key)


def test_format_2_unknown_keys_still_warn() -> None:
    text = format_manifest(manifest(store=S3_STORE)).decode()
    text = text.replace("[store.blobs]\n", "[store.blobs]\nregion = 'x'\n", 1)
    parsed, warnings = parse_manifest(text.encode(), source="m")
    assert parsed.store == S3_STORE
    assert [w.details["key"] for w in warnings] == ["store.blobs.region"]


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
