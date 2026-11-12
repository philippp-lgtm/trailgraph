"""The actor of a server request (contracts B2, B14.12)."""

from typing import Any

import pytest

from graph_cli.catalog.identity.request import request_actor
from graph_cli.catalog.tokens import TokenRecord
from graph_cli.kernel.actor import ClientInfo, Entrypoint
from graph_cli.kernel.errors import GraphCliError

CLIENT = ClientInfo("graph-server", "0.1.0", Entrypoint.SERVER)
TOKEN = TokenRecord("ci", "sha256:" + "0" * 64)
BOUND = TokenRecord("ci", "sha256:" + "0" * 64, project="example_project")


def actor_json(asserted: Any, token: TokenRecord = TOKEN) -> tuple[dict[str, Any], list[str]]:
    actor, warnings = request_actor(asserted, token=token, client=CLIENT)
    return actor.to_json(), [w.code for w in warnings]


def test_token_principal_and_client_facets() -> None:
    data, warnings = actor_json(
        {
            "agent": {"id": "agent-1", "type": "coding_agent"},
            "session": {"id": "sess-1", "parent": "sess-0"},
            "device": {"id": "device-1"},
            "project": "example_project",
        }
    )
    assert warnings == []
    assert data == {
        "principal": {"assurance": "verified", "id": "token:ci", "name": "ci", "source": "token"},
        "agent": {
            "assurance": "asserted",
            "id": "agent-1",
            "type": "coding_agent",
            "source": "client",
        },
        "session": {
            "assurance": "asserted",
            "id": "sess-1",
            "parent": "sess-0",
            "source": "client",
        },
        "device": {"assurance": "asserted", "id": "device-1", "source": "client"},
        "project": {"assurance": "asserted", "id": "example_project", "source": "client"},
        "client": {"name": "graph-server", "version": "0.1.0", "entrypoint": "server"},
    }


def test_nothing_asserted() -> None:
    data, warnings = actor_json(None)
    assert warnings == []
    assert {k: v for k, v in data.items() if k in ("agent", "session", "device", "project")} == {
        name: {"assurance": "absent"} for name in ("agent", "session", "device", "project")
    }


def test_malformed_values_are_absent_with_a_warning() -> None:
    data, warnings = actor_json({"agent": {"id": "bad id"}, "project": "-x"})
    assert data["agent"] == {"assurance": "absent"}
    assert data["project"] == {"assurance": "absent"}
    assert warnings == ["actor.facet_malformed", "actor.facet_malformed"]


def test_a_bound_token_verifies_the_project() -> None:
    for asserted in (None, {"project": "example_project"}):
        data, _ = actor_json(asserted, BOUND)
        assert data["project"] == {
            "assurance": "verified",
            "id": "example_project",
            "source": "token",
        }
    with pytest.raises(GraphCliError) as info:
        request_actor({"project": "another_project"}, token=BOUND, client=CLIENT)
    assert info.value.code == "access.project_mismatch"
    assert info.value.details == {"project": "example_project"}


@pytest.mark.parametrize(
    "asserted",
    [
        {"pid": 1},
        {"agent": "agent-1"},
        {"agent": {"id": "agent-1", "name": "x"}},
        {"project": 7},
    ],
)
def test_shape_errors_are_usage_errors(asserted: Any) -> None:
    with pytest.raises(GraphCliError) as info:
        request_actor(asserted, token=TOKEN, client=CLIENT)
    assert info.value.code == "usage.invalid_input"


def test_the_actor_holds_the_roles_of_its_token() -> None:
    """B6.7: roles come from the token record only; a client cannot assert them."""
    held = TokenRecord("ci", "sha256:" + "0" * 64, roles=("importer", "reader"))
    data, warnings = actor_json(None, held)
    assert warnings == [] and data["roles"] == ["importer", "reader"]
    actor, _ = request_actor(None, token=held, client=CLIENT)
    assert actor.roles == ("importer", "reader")
    assert "roles" not in actor_json(None)[0]  # a token without roles: the actor is as before
    for asserted in ({"roles": ["reader"]}, {"agent": {"id": "a-1", "roles": ["reader"]}}):
        with pytest.raises(GraphCliError) as info:
            request_actor(asserted, token=TOKEN, client=CLIENT)
        assert info.value.code == "usage.invalid_input"
