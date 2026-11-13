"""Enrollment codes: the document, codes, lifetimes and token names (contracts B14.21)."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from graph_cli.catalog.enrollments import (
    ENROLLMENTS_SCHEMA,
    EnrollmentRecord,
    code_hash,
    default_token_name,
    enrollments_document,
    find,
    is_code,
    new_code,
    new_id,
    parse_enrollments,
    parse_lifetime,
)
from graph_cli.catalog.tokens import TOKEN_NAME_RE
from graph_cli.kernel.canonical import pretty_bytes
from graph_cli.kernel.errors import GraphCliError

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def record(code: str, **changes: str) -> EnrollmentRecord:
    fields = {
        "id": new_id(),
        "hash": code_hash(code),
        "project": "p1",
        "scope": "app",
        "expires_at": "2026-10-25T12:15:00.000Z",
        "created_at": "2026-10-25T12:00:00.000Z",
        **changes,
    }
    return EnrollmentRecord(**fields)


def test_codes_are_random_and_typed() -> None:
    first, second = new_code(), new_code()
    assert first != second and is_code(first) and len(first) == 4 + 26
    assert not is_code(first.upper()) and not is_code("gct_" + first[4:])
    assert len(new_id()) == 12


def test_a_code_is_found_by_its_hash_until_it_expires() -> None:
    code = new_code()
    records = (record(new_code()), record(code), record(new_code()))
    found = find(records, code)
    assert found is records[1]
    assert find(records, new_code()) is None and find(records, "") is None
    assert find(records, "not a code") is None
    assert found.valid_at(NOW) and not found.valid_at(NOW + timedelta(minutes=15))


def test_the_document_round_trips_and_damage_fails_closed() -> None:
    records = (record(new_code()), record(new_code(), scope="admin"))
    document = enrollments_document(records)
    assert document["schema"] == ENROLLMENTS_SCHEMA
    assert sorted(parse_enrollments(pretty_bytes(document)), key=lambda r: r.id) == sorted(
        records, key=lambda r: r.id
    )
    assert parse_enrollments(None) == ()
    entry = records[0].to_json()
    for broken in (
        b"not json",
        json.dumps({"schema": "x", "enrollments": []}).encode(),
        json.dumps({"schema": ENROLLMENTS_SCHEMA, "enrollments": [entry, entry]}).encode(),
        json.dumps({"schema": ENROLLMENTS_SCHEMA, "enrollments": [{**entry, "x": 1}]}).encode(),
        json.dumps({"schema": ENROLLMENTS_SCHEMA, "enrollments": [{**entry, "id": "X"}]}).encode(),
        json.dumps(
            {"schema": ENROLLMENTS_SCHEMA, "enrollments": [{**entry, "scope": "root"}]}
        ).encode(),
        json.dumps(
            {"schema": ENROLLMENTS_SCHEMA, "enrollments": [{**entry, "expires_at": "soon"}]}
        ).encode(),
    ):
        with pytest.raises(GraphCliError) as caught:
            parse_enrollments(broken)
        assert caught.value.code == "backend.unavailable"


def test_lifetimes_are_minutes_or_hours_up_to_a_day() -> None:
    assert parse_lifetime("15m") == timedelta(minutes=15)
    assert parse_lifetime("24h") == timedelta(hours=24)
    for text in ("0m", "25h", "1441m", "1d", "15", "m", "-5m", "1.5h"):
        assert parse_lifetime(text) is None, text


@pytest.mark.parametrize(
    ("project", "label", "expected"),
    [
        ("p1", "laptop", "p1.laptop"),
        ("Team:Alpha", "Jo's MacBook Pro", "team-alpha.jo-s-macbook-pro"),
        ("p1", "", "p1.client"),
        ("p1", "...", "p1.client"),
        ("x" * 100, "y" * 100, "x" * 40 + "." + "y" * 23),
    ],
)
def test_default_token_names_are_token_names(project: str, label: str, expected: str) -> None:
    name = default_token_name(project, label)
    assert name == expected and TOKEN_NAME_RE.match(name)


def test_roles_are_listed_only_when_a_code_has_any() -> None:
    """B14.21: a code without roles serializes exactly as before roles existed."""
    plain = record(new_code())
    assert "roles" not in plain.to_json()
    held = replace(record(new_code()), roles=("reader",))
    assert held.to_json()["roles"] == ["reader"]
    document = enrollments_document((plain, held))
    parsed = parse_enrollments(pretty_bytes(document))
    assert sorted(parsed, key=lambda r: r.id) == sorted((plain, held), key=lambda r: r.id)
    old = {"schema": ENROLLMENTS_SCHEMA, "enrollments": [plain.to_json()]}
    assert parse_enrollments(json.dumps(old).encode()) == (plain,)


@pytest.mark.parametrize(
    "roles",
    [
        "reader",
        [],
        ["Reader"],
        ["reader", 1],
        ["reader", "reader"],
        ["reader", "importer"],
        [f"role_{n:02d}" for n in range(17)],
    ],
)
def test_malformed_roles_fail_closed(roles: object) -> None:
    entry = {**record(new_code()).to_json(), "roles": roles}
    with pytest.raises(GraphCliError) as caught:
        parse_enrollments(
            json.dumps({"schema": ENROLLMENTS_SCHEMA, "enrollments": [entry]}).encode()
        )
    assert caught.value.code == "backend.unavailable"
    assert "roles" in caught.value.message
