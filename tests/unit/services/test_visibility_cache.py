"""``Visibility``: what each actor sees per graph, cached (type read limits plan V4)."""

import dataclasses

from support.fixtures import compiled_model
from support.txbuilder import actor as plain_actor

from graph_cli.access_control.compiler import compile_policy
from graph_cli.access_control.policy import BUILTIN_POLICY_SET, policy_set
from graph_cli.kernel.actor import ActorContext, Assurance, ClientInfo, Entrypoint, Facet
from graph_cli.kernel.ids import GraphId, parse_id
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.records import OntologyPin
from graph_cli.services.visibility import Visibility, actor_key

EXAMPLE = GraphId(parse_id("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a"))
OTHER = GraphId(parse_id("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8b"))
MODEL = compiled_model("notes_and_sources", "1.1.0")
SOURCE = b"""\
schema: graph_cli.policy/1
id: installation
roles:
  reporting: {description: "Reporting: notes only"}
  auditor: {description: "Auditors: everything"}
grants:
  - entrypoint: server
    principal: {role: reporting}
    graphs: [example_graph]
    level: read
    reads: [nas:Note]
  - {entrypoint: server, principal: {role: auditor}, graphs: [example_graph], level: read}
no_project: {"*": read}
"""
POLICY = policy_set(
    compile_policy(
        SOURCE, file="installation.yaml", graphs={"example_graph": EXAMPLE, "other_graph": OTHER}
    ).document
)


def entry(graph: GraphId = EXAMPLE, artifact: str = "sha256:a") -> GraphEntry:
    return GraphEntry(
        graph,
        "example_graph",
        "active",
        OntologyPin("notes_and_sources", "1.1.0", artifact),
        {},
        (),
    )


def token(*roles: str, session: str | None = None) -> ActorContext:
    found = dataclasses.replace(
        plain_actor(),
        principal=Facet(Assurance.VERIFIED, id="token:t", name="t", source="token"),
        client=ClientInfo("graph-server", "0.1.0", Entrypoint.SERVER),
        roles=roles,
    )
    if session is not None:
        found = dataclasses.replace(found, session=Facet(Assurance.ASSERTED, id=session))
    return found


def test_a_limited_actor_gets_its_types_and_an_unlimited_one_none() -> None:
    cache = Visibility()
    assert cache.of(token("reporting"), entry(), POLICY, MODEL) == {"nas:Note"}
    assert cache.of(token("auditor"), entry(), POLICY, MODEL) is None
    assert cache.of(token("reporting", "auditor"), entry(), POLICY, MODEL) is None
    assert cache.of(token(), entry(), POLICY, MODEL) == frozenset()


def test_a_graph_that_no_rule_limits_is_not_filtered() -> None:
    cache = Visibility()
    assert cache.of(token("reporting"), entry(OTHER), POLICY, MODEL) is None
    assert cache.of(token(), entry(), BUILTIN_POLICY_SET, MODEL) is None
    assert not cache.limited(BUILTIN_POLICY_SET, EXAMPLE)
    assert cache.limited(POLICY, EXAMPLE)


def test_the_answer_is_cached_per_policy_graph_pin_and_actor() -> None:
    cache = Visibility()
    first = cache.of(token("reporting"), entry(), POLICY, MODEL)
    assert cache.of(token("reporting"), entry(), POLICY, MODEL) is first
    assert len(cache._visible) == 1
    cache.of(token("reporting"), entry(artifact="sha256:b"), POLICY, MODEL)  # another pin
    cache.of(token("auditor"), entry(), POLICY, MODEL)  # another actor
    cache.of(token("reporting", session="s-1"), entry(), POLICY, MODEL)  # another asserted fact
    assert len(cache._visible) == 4


def test_the_actor_key_covers_what_authorize_reads() -> None:
    base = token("reporting")
    assert actor_key(base) == actor_key(token("reporting"))
    assert actor_key(base) != actor_key(token("auditor"))
    assert actor_key(base) != actor_key(token("reporting", session="s-1"))
    verified = dataclasses.replace(base, session=Facet(Assurance.VERIFIED, id="s-1"))
    asserted = dataclasses.replace(base, session=Facet(Assurance.ASSERTED, id="s-1"))
    assert actor_key(verified) != actor_key(asserted)  # allow rules see verified facts only
    app_caller = dataclasses.replace(base, client=ClientInfo("graph", "0.1.0", Entrypoint.APP))
    assert actor_key(base) != actor_key(app_caller)


def test_the_actor_cache_is_bounded() -> None:
    cache = Visibility()
    for number in range(600):
        cache.of(token("reporting", session=f"s-{number}"), entry(), POLICY, MODEL)
    assert len(cache._visible) <= 512


def test_the_filtered_model_is_cached_per_pin_and_visible_set() -> None:
    cache = Visibility()
    visible = frozenset({"nas:Note"})
    first = cache.model(entry(), MODEL, visible)
    assert cache.model(entry(), MODEL, visible) is first
    assert cache.model(entry(), MODEL, frozenset({"nas:Topic"})) is not first
    assert cache.model(entry(artifact="sha256:b"), MODEL, visible) is not first
    assert set(first.classes) == {"nas:Note"}
