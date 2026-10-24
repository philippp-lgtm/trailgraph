"""Actor context serialization (contracts B2.4, B2.9)."""

from dataclasses import replace

import pytest

from graph_cli.kernel.actor import (
    ABSENT,
    ActorContext,
    Assurance,
    ClientInfo,
    Entrypoint,
    Facet,
    is_valid_asserted_value,
)
from graph_cli.kernel.canonical import canonical_bytes
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.json_access import JsonShapeError


def make_actor() -> ActorContext:
    return ActorContext(
        principal=Facet(Assurance.VERIFIED, id="os:1000", name="user", source="os"),
        agent=Facet(Assurance.ASSERTED, id="agent-1", type="general", source="env"),
        session=ABSENT,
        device=Facet(Assurance.ASSERTED, id="device-1", source="config"),
        project=ABSENT,
        client=ClientInfo("graph", "0.1.0", Entrypoint.APP),
        pid=42,
    )


def test_round_trip_and_shape() -> None:
    actor = make_actor()
    data = actor.to_json()
    assert data["session"] == {"assurance": "absent"}
    assert data["client"] == {"name": "graph", "version": "0.1.0", "entrypoint": "app"}
    assert ActorContext.from_json(data) == actor
    assert canonical_bytes(ActorContext.from_json(data).to_json()) == canonical_bytes(data)


def test_server_entrypoint_round_trip() -> None:
    """B2.7, B14.12: the server's actor: a token principal, client facets, no pid."""
    actor = replace(
        make_actor(),
        principal=Facet(Assurance.VERIFIED, id="token:ci", name="ci", source="token"),
        project=Facet(Assurance.VERIFIED, id="example_project", source="token"),
        client=ClientInfo("graph-server", "0.1.0", Entrypoint.SERVER),
        pid=None,
    )
    data = actor.to_json()
    assert data["client"] == {"name": "graph-server", "version": "0.1.0", "entrypoint": "server"}
    assert ActorContext.from_json(data) == actor


def test_pid_is_omitted_when_unknown() -> None:
    data = replace(make_actor(), pid=None).to_json()
    assert "pid" not in data


def test_unknown_closed_enum_values_are_unsupported_versions() -> None:
    data = make_actor().to_json()
    for bad in (
        {**data, "agent": {"assurance": "probably"}},
        {**data, "client": {"name": "graph", "version": "1", "entrypoint": "root"}},
    ):
        with pytest.raises(GraphCliError) as info:
            ActorContext.from_json(bad)
        assert info.value.code == "format.unsupported_version"


def test_missing_facet_is_a_shape_error() -> None:
    data = dict(make_actor().to_json())
    del data["project"]
    with pytest.raises(JsonShapeError):
        ActorContext.from_json(data)


@pytest.mark.parametrize(
    ("value", "valid"),
    [("agent-7f3a", True), ("a:b.c_d-e", True), ("-lead", False), ("has space", False)],
)
def test_asserted_value_grammar(value: str, valid: bool) -> None:
    assert is_valid_asserted_value(value) is valid
