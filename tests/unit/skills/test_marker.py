"""The install marker: canonical, deterministic, strict on read (D15 Q5)."""

import pytest

from graph_cli.kernel.canonical import canonical_bytes, hash_bytes, parse_json
from graph_cli.kernel.skill_marker import MARKER_SCHEMA
from graph_cli.skills.marker import (
    Marker,
    decode_marker,
    encode_marker,
    file_hashes,
)

FILES = {"SKILL.md": b"a", "references/x.md": b"b"}


def marker() -> Marker:
    return Marker(
        "graph-cli",
        "app",
        {"name": "graph", "version": "0.1.0"},
        {"inventory_hash": hash_bytes(b"i")},
        file_hashes(FILES),
    )


def test_round_trip_and_determinism() -> None:
    data = encode_marker(marker())
    assert decode_marker(data) == marker()
    assert encode_marker(marker()) == data
    assert data == canonical_bytes(parse_json(data))
    assert b"ts" not in data and b"time" not in data
    document = parse_json(data)
    assert isinstance(document, dict) and document["schema"] == MARKER_SCHEMA


def test_file_hashes_are_sha256_of_the_bytes() -> None:
    assert file_hashes(FILES) == {"SKILL.md": hash_bytes(b"a"), "references/x.md": hash_bytes(b"b")}


@pytest.mark.parametrize(
    "data",
    [
        b"not json",
        b"[]",
        b'{"schema": "other/1"}',
        encode_marker(marker()).replace(b'"app"', b'"tool"'),
        encode_marker(marker()).replace(b"sha256:", b"md5:"),
        b'{"schema": "graph_cli.skill_install/1", "kind": "app"}',
    ],
)
def test_unreadable_markers_are_none(data: bytes) -> None:
    assert decode_marker(data) is None
