"""Identity providers and actor resolution (contracts B2.2-B2.9, A6)."""

import pytest

from graph_cli.catalog.identity import IdentityInputs, resolve_actor, system_inputs
from graph_cli.kernel.actor import ABSENT, Assurance, ClientInfo, Entrypoint, Facet
from graph_cli.kernel.canonical import canonical_bytes
from graph_cli.kernel.errors import GraphCliError

APP = ClientInfo("graph", "0.1.0", Entrypoint.APP)
GENERIC = {
    "GRAPH_CLI_AGENT_ID": "agent-7f3a",
    "GRAPH_CLI_AGENT_TYPE": "general",
    "GRAPH_CLI_SESSION_ID": "sess-41c2",
    "GRAPH_CLI_PARENT_SESSION_ID": "sess-0001",
}
HARNESS = {
    "CLAUDE_AGENT_ID": "harness-agent",
    "CLAUDE_AGENT_TYPE": "explore",
    "CLAUDE_CODE_SESSION_ID": "harness-session",
}


def inputs(env: dict[str, str] | None = None) -> IdentityInputs:
    return IdentityInputs(env=env or {}, uid=501, user_name="user", pid=48211)


def test_os_only() -> None:
    actor, warnings = resolve_actor(inputs(), client=APP)
    assert warnings == ()
    assert actor.to_json() == {
        "principal": {"id": "os:501", "name": "user", "assurance": "verified", "source": "os"},
        "agent": {"assurance": "absent"},
        "session": {"assurance": "absent"},
        "device": {"assurance": "absent"},
        "project": {"assurance": "absent"},
        "client": {"name": "graph", "version": "0.1.0", "entrypoint": "app"},
        "pid": 48211,
    }


def test_generic_env() -> None:
    actor, _ = resolve_actor(inputs(GENERIC), client=APP)
    assert actor.agent == Facet(
        Assurance.ASSERTED, id="agent-7f3a", type="general", source="env:GRAPH_CLI_AGENT_ID"
    )
    assert actor.session == Facet(
        Assurance.ASSERTED, id="sess-41c2", parent="sess-0001", source="env:GRAPH_CLI_SESSION_ID"
    )


def test_harness_variables() -> None:
    actor, _ = resolve_actor(inputs(HARNESS), client=APP)
    assert actor.agent == Facet(
        Assurance.ASSERTED, id="harness-agent", type="explore", source="claude_code"
    )
    assert actor.session == Facet(Assurance.ASSERTED, id="harness-session", source="claude_code")
    fallback, _ = resolve_actor(inputs({"CLAUDE_SESSION_ID": "older-name"}), client=APP)
    assert fallback.session.id == "older-name"


# (env, expected agent id, expected session id)
PRECEDENCE = [
    ({**HARNESS, **GENERIC}, "agent-7f3a", "sess-41c2"),
    ({**HARNESS, "GRAPH_CLI_AGENT_ID": "agent-7f3a"}, "agent-7f3a", "harness-session"),
    ({**HARNESS, "GRAPH_CLI_SESSION_ID": "sess-41c2"}, "harness-agent", "sess-41c2"),
    ({**HARNESS, "GRAPH_CLI_AGENT_ID": ""}, "harness-agent", "harness-session"),
    ({"GRAPH_CLI_AGENT_TYPE": "general"}, None, None),
]


@pytest.mark.parametrize(("env", "agent", "session"), PRECEDENCE)
def test_precedence_per_facet(env: dict[str, str], agent: str | None, session: str | None) -> None:
    actor, warnings = resolve_actor(inputs(env), client=APP)
    assert (actor.agent.id, actor.session.id) == (agent, session)
    assert warnings == ()


def test_a_facet_comes_whole_from_one_provider() -> None:
    env = {"GRAPH_CLI_AGENT_ID": "agent-7f3a", "CLAUDE_AGENT_TYPE": "explore"}
    actor, _ = resolve_actor(inputs(env), client=APP)
    assert actor.agent.type is None


@pytest.mark.parametrize(
    "variable",
    ["GRAPH_CLI_AGENT_ID", "GRAPH_CLI_AGENT_TYPE"],
)
def test_malformed_value_makes_the_facet_absent_without_fallback(variable: str) -> None:
    secret = "has spaces/and-$ecret"
    env = {**HARNESS, **GENERIC, variable: secret}
    actor, warnings = resolve_actor(inputs(env), client=APP)
    assert actor.agent == ABSENT
    assert actor.session.id == "sess-41c2"
    (warning,) = warnings
    assert warning.code == "actor.facet_malformed"
    assert warning.details == {"facet": "agent", "source": variable}
    assert secret.encode() not in canonical_bytes(warning.to_json())


def test_disabling_the_harness_provider() -> None:
    actor, _ = resolve_actor(inputs(HARNESS), client=APP, providers=["generic_env"])
    assert actor.agent == ABSENT and actor.session == ABSENT
    with pytest.raises(GraphCliError) as info:
        resolve_actor(inputs(), client=APP, providers=["ldap"])
    assert info.value.code == "config.invalid"


def test_device_and_project() -> None:
    actor, warnings = resolve_actor(
        inputs(), client=APP, device_id="0192e000-0000-7000-8000-00000000d001", project="kb"
    )
    assert actor.device == Facet(
        Assurance.ASSERTED, id="0192e000-0000-7000-8000-00000000d001", source="config"
    )
    assert actor.project == Facet(Assurance.ASSERTED, id="kb", source="pointer")
    assert warnings == ()
    actor, warnings = resolve_actor(inputs(), client=APP, project="bad project")
    assert actor.project == ABSENT
    assert [w.details["facet"] for w in warnings] == ["project"]


def test_entrypoint_never_comes_from_env() -> None:
    """B2.7: the binary passes its entrypoint; no variable can change it."""
    env = {"GRAPH_CLI_ENTRYPOINT": "maint", "GRAPH_CLI_CLIENT": "graph-admin"}
    actor, _ = resolve_actor(inputs(env), client=APP)
    assert actor.client == APP


def test_unknown_user_name() -> None:
    actor, _ = resolve_actor(IdentityInputs(env={}, uid=4242, user_name=None, pid=1), client=APP)
    assert actor.principal == Facet(Assurance.VERIFIED, id="os:4242", source="os")


def test_system_inputs_read_the_process() -> None:
    import os

    found = system_inputs()
    assert (found.uid, found.pid) == (os.getuid(), os.getpid())
    assert found.env == dict(os.environ)
    actor, _ = resolve_actor(found, client=APP)
    assert actor.principal.assurance is Assurance.VERIFIED
