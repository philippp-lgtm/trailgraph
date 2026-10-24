"""Bearer tokens: the document, hashing and the server's store (contracts B14.11)."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from support.stores import RecordingSyncer

from graph_cli.catalog.tokens import (
    TOKENS_SCHEMA,
    TokenRecord,
    TokenStore,
    hash_prefix,
    new_token,
    parse_tokens,
    token_hash,
    tokens_document,
)
from graph_cli.kernel.canonical import pretty_bytes
from graph_cli.kernel.errors import GraphCliError
from graph_cli.storage.files.token_files import tokens_path, write_tokens_file

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


def install(root: Path, *records: TokenRecord) -> None:
    write_tokens_file(tokens_path(root), tokens_document(records), RecordingSyncer())


def test_new_tokens_are_random_and_hashed() -> None:
    first, second = new_token(), new_token()
    assert first != second
    assert first.startswith("gct_") and len(first) == 4 + 43
    assert token_hash(first).startswith("sha256:") and len(token_hash(first)) == 7 + 64
    assert hash_prefix(token_hash(first)) == token_hash(first)[:23]


def test_lookup_by_hash_project_and_expiry(tmp_path: Path) -> None:
    store = TokenStore(tmp_path)
    secret, other = new_token(), new_token()
    assert store.lookup(secret, NOW) is None  # no file: no tokens
    install(
        tmp_path,
        TokenRecord("ci", token_hash(secret), "example_project", "2026-10-24"),
        TokenRecord("old", token_hash(other), None, "2026-10-23"),
    )
    found = store.lookup(secret, NOW)
    assert found is not None and (found.name, found.project) == ("ci", "example_project")
    assert store.lookup(other, NOW) is None  # expired the day before
    assert store.lookup("gct_unknown", NOW) is None
    assert store.lookup(secret.removeprefix("gct_"), NOW) is None
    assert store.lookup(secret, datetime(2026, 10, 25, tzinfo=UTC)) is None
    assert stat_mode(tokens_path(tmp_path)) == 0o600


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_the_store_rereads_a_changed_file(tmp_path: Path) -> None:
    store = TokenStore(tmp_path)
    secret = new_token()
    install(tmp_path, TokenRecord("ci", token_hash(secret)))
    assert store.lookup(secret, NOW) is not None
    install(tmp_path)  # revoked
    assert store.lookup(secret, NOW) is None


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"[]",
        pretty_bytes({"schema": "other/1", "tokens": []}),
        pretty_bytes({"schema": TOKENS_SCHEMA, "tokens": [{"name": "Bad Name", "hash": "x"}]}),
        pretty_bytes(
            {
                "schema": TOKENS_SCHEMA,
                "tokens": [{"name": "ci", "hash": "sha256:" + "0" * 64, "expires": "2026-13-01"}],
            }
        ),
        pretty_bytes(
            {
                "schema": TOKENS_SCHEMA,
                "tokens": [{"name": "ci", "hash": "sha256:" + "0" * 64}] * 2,
            }
        ),
    ],
)
def test_a_damaged_file_fails_closed(tmp_path: Path, content: bytes) -> None:
    path = tokens_path(tmp_path)
    path.parent.mkdir()
    path.write_bytes(content)
    with pytest.raises(GraphCliError) as info:
        TokenStore(tmp_path).lookup(new_token(), NOW)
    assert info.value.code == "backend.unavailable"
    assert "token" in info.value.message


def test_a_directory_in_the_way_fails_closed(tmp_path: Path) -> None:
    os.makedirs(tokens_path(tmp_path))
    with pytest.raises(GraphCliError) as info:
        TokenStore(tmp_path).lookup(new_token(), NOW)
    assert info.value.code == "backend.unavailable"


def test_document_round_trip() -> None:
    records = (
        TokenRecord("web", "sha256:" + "1" * 64, None, None, "2026-10-24T10:00:00.000Z"),
        TokenRecord("ci", "sha256:" + "2" * 64, "example_project", "2027-01-31"),
    )
    document = tokens_document(records)
    assert [t["name"] for t in document["tokens"]] == ["ci", "web"]  # type: ignore[index, union-attr]
    assert parse_tokens(json.dumps(document).encode()) == tuple(
        sorted(records, key=lambda r: r.name)
    )
    assert parse_tokens(None) == ()
