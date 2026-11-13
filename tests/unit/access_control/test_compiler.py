"""Authored policies compiled to graph_cli.policy/1 (contracts B6.13, C.7).

The compiled documents are evaluated with ``authorize()``, so the tests show
what an authored policy means rather than how its rules are spelled.
"""

import dataclasses

import pytest
from support.audit_events import maint_actor
from support.txbuilder import actor

from graph_cli.access_control.authorize import authorize
from graph_cli.access_control.compiler import BASE_OBLIGATION, compile_policy, layer_of_source
from graph_cli.access_control.documents import document_problems
from graph_cli.access_control.effective import graph_level
from graph_cli.access_control.policy import BUILTIN_POLICY_SET, policy_set
from graph_cli.kernel.actor import ActorContext, Assurance, ClientInfo, Entrypoint, Facet
from graph_cli.kernel.authz import AuthzContext
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GraphId, parse_id

EXAMPLE = GraphId(parse_id("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a"))
OTHER = GraphId(parse_id("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8b"))
THIRD = GraphId(parse_id("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8c"))
GRAPHS = {"example_graph": EXAMPLE, "other_graph": OTHER, "third_graph": THIRD}
CTX = AuthzContext(op_count=1, has_reason=True, dry_run=False)

INSTALLATION = b"""\
schema: graph_cli.policy/1
id: installation
grants:
  - {principal: {name: tester}, graphs: "*", level: write}
scopes:
  example_project: {example_graph: write, other_graph: read}
  another_project: {other_graph: write}
no_project: {"*": read}
deny:
  - {id: no_explore_writes, agent_type: [explore], action: ["write.*"]}
"""

GRAPH_POLICY = b"""\
schema: graph_cli.policy/1
id: graph
types:
  "files:File": read
obligations:
  - {action: ["write.transition"], require_reason: true}
  - {action: ["write.*"], max_batch: 200}
deny:
  - {id: no_retract_by_explore, agent_type: explore, action: [write.retract], type: ["inv:*"]}
"""


def who(project: str | None = None, *, agent_type: str | None = None) -> ActorContext:
    base = actor()  # principal "tester"
    if project is not None:
        base = dataclasses.replace(
            base, project=Facet(Assurance.ASSERTED, id=project, source="pointer")
        )
    if agent_type is not None:
        agent = Facet(Assurance.ASSERTED, id="agent-1", type=agent_type, source="env")
        base = dataclasses.replace(base, agent=agent)
    return base


def installation() -> dict[str, JsonValue]:
    return compile_policy(INSTALLATION, file="installation.yaml", graphs=GRAPHS).document


def levels(subject: ActorContext, *documents: dict[str, JsonValue]) -> list[str]:
    policy = policy_set(*documents)
    return [graph_level(subject, g, policy) for g in (EXAMPLE, OTHER, THIRD)]


SERVER_GRANTS = b"""\
schema: graph_cli.policy/1
id: installation
grants:
  - {principal: {name: tester}, graphs: "*", level: write}
  - {entrypoint: server, principal: {id: "token:ci"}, graphs: [example_graph], level: write}
  - {entrypoint: [server, app], graphs: [other_graph], level: read}
no_project: {"*": write}
"""


def served(subject: ActorContext) -> ActorContext:
    return dataclasses.replace(
        subject, client=ClientInfo("graph-server", "0.1.0", Entrypoint.SERVER)
    )


def test_grants_name_the_server_entrypoint() -> None:
    """B6.13 (B14): a grant without entrypoint stays an app grant, with the same hash."""
    compiled = compile_policy(SERVER_GRANTS, file="installation.yaml", graphs=GRAPHS)
    rules = {r["id"]: r for r in compiled.document["rules"]}  # type: ignore[index, union-attr]
    assert rules["grant.1"]["principal"] == {"entrypoint": "app", "principal_name": "tester"}
    assert rules["grant.2"]["principal"] == {"entrypoint": "server", "principal_id": "token:ci"}
    assert rules["grant.3"]["principal"] == {"entrypoint": ["app", "server"]}
    token = served(
        dataclasses.replace(
            actor(), principal=Facet(Assurance.VERIFIED, id="token:ci", name="ci", source="token")
        )
    )
    assert levels(token, compiled.document) == ["write", "read", "none"]
    assert levels(served(actor()), compiled.document) == ["none", "read", "none"]
    assert levels(actor(), compiled.document) == ["write", "write", "write"]
    plain = INSTALLATION.replace(
        b"{principal: {name: tester},", b"{entrypoint: app, principal: {name: tester},"
    )
    assert (
        compile_policy(plain, file="i.yaml", graphs=GRAPHS).hash
        == compile_policy(INSTALLATION, file="i.yaml", graphs=GRAPHS).hash
    )


def test_grants_refuse_other_entrypoints() -> None:
    bad = SERVER_GRANTS.replace(b"entrypoint: server,", b"entrypoint: maint,")
    with pytest.raises(GraphCliError) as info:
        compile_policy(bad, file="installation.yaml", graphs=GRAPHS)
    paths = [p["path"] for p in info.value.details["problems"]]  # type: ignore[union-attr]
    assert paths == ["grants[1].entrypoint"]


def test_installation_levels_per_project() -> None:
    document = installation()
    assert document_problems(document) == []
    assert document["projects"] == ["another_project", "example_project"]
    assert levels(who("example_project"), document) == ["write", "read", "none"]
    assert levels(who("another_project"), document) == ["none", "write", "none"]
    assert levels(who(), document) == ["read", "read", "read"]  # no project
    assert levels(who("unknown_project"), document) == ["read", "read", "read"]  # Q3


def test_grants_match_verified_facets_only() -> None:
    document = installation()
    stranger = dataclasses.replace(
        who("example_project"),
        principal=Facet(Assurance.VERIFIED, id="os:999", name="stranger", source="os"),
    )
    assert levels(stranger, document) == ["none", "none", "none"]


def test_deny_rules_and_base_obligation() -> None:
    document = installation()
    policy = policy_set(document)
    typed = f"graph:{EXAMPLE}/type:inv:Item"
    explorer = who("example_project", agent_type="explore")
    decision = authorize(explorer, "write.create", typed, CTX, policy)
    assert (decision.effect, decision.rule_id) == ("deny", "deny.no_explore_writes")
    assert authorize(explorer, "read.get", f"graph:{EXAMPLE}", CTX, policy).effect == "allow"
    assert document["obligations"] == [dict(BASE_OBLIGATION)]
    unreasoned = dataclasses.replace(CTX, has_reason=False)
    assert (
        authorize(who("example_project"), "write.retract", typed, unreasoned, policy).code
        == "access.reason_required"
    )


def test_maintainers() -> None:
    default = policy_set(installation())
    assert authorize(maint_actor(), "maint.policy_change", "installation", CTX, default).effect == (
        "allow"
    )
    named = INSTALLATION + b"maintainers:\n  - {name: somebody_else}\n"
    policy = policy_set(compile_policy(named, file="p.yaml", graphs=GRAPHS).document)
    assert authorize(maint_actor(), "maint.policy_change", "installation", CTX, policy).code == (
        "access.denied"
    )


PROJECTS = INSTALLATION.replace(
    b"  another_project: {other_graph: write}\n",
    b"  another_project: {other_graph: maintain, example_graph: maintain}\n",
).replace(
    b"grants:\n",
    b"grants:\n"
    b"  - {entrypoint: server, principal: {project: another_project}, graphs: [other_graph],"
    b" level: write}\n",
)


def token_of(project: str | None, entrypoint: Entrypoint) -> ActorContext:
    """A server request with token ``t``, bound to ``project`` (verified) when given."""
    bound = Facet(Assurance.VERIFIED, id=project, source="token") if project else actor().project
    return dataclasses.replace(
        actor(),
        principal=Facet(Assurance.VERIFIED, id="token:t", name="t", source="token"),
        project=bound,
        client=ClientInfo("graph-server", "0.1.0", entrypoint),
    )


def test_maintain_lets_a_project_admin_maintain_named_graphs_only() -> None:
    """Plan origin_lifecycle E8-E10: one allow rule for the project's admin tokens."""
    compiled = compile_policy(PROJECTS, file="i.yaml", graphs=GRAPHS)
    rules = {r["id"]: r for r in compiled.document["rules"]}  # type: ignore[index, union-attr]
    assert rules["scope.another_project:maintain"]["principal"] == {
        "entrypoint": "maint",
        "project": "another_project",
    }
    policy = policy_set(compiled.document)
    admin = token_of("another_project", Entrypoint.MAINT)
    for graph, effect in ((OTHER, "allow"), (EXAMPLE, "allow"), (THIRD, "deny")):
        decision = authorize(admin, "maint.ontology_release", f"graph:{graph}", CTX, policy)
        assert decision.effect == effect, graph
    assert authorize(admin, "read.get", f"graph:{OTHER}", CTX, policy).effect == "allow"
    for action, resource in (
        ("read.find", f"graph:{OTHER}"),  # findings quote data (read.get), nothing more
        ("write.create", f"graph:{OTHER}"),
        ("maint.graph_archive", f"graph:{OTHER}"),  # destructive: maintainers only
        ("maint.repair", f"graph:{OTHER}"),
        ("maint.token_create", "installation"),
        ("maint.token_update", "installation"),  # a role change widens rights: maintainers
        ("maint.audit_read", "installation"),
    ):
        decision = authorize(admin, action, resource, CTX, policy)
        assert (decision.rule_id, decision.code) == ("ceiling.project_maint", "access.ceiling")
    # maintain is write for the project's callers, nothing more
    assert levels(who("another_project"), compiled.document) == ["write", "write", "none"]
    # the maintainer rule (none listed: every maintainer) never reaches a project admin
    assert "maint.any" in rules
    elsewhere = token_of("example_project", Entrypoint.MAINT)
    decision = authorize(elsewhere, "maint.reindex", f"graph:{OTHER}", CTX, policy)
    assert (decision.effect, decision.rule_id) == ("deny", "default.deny")


def test_a_maintain_rule_needs_a_verified_project() -> None:
    policy = policy_set(compile_policy(PROJECTS, file="i.yaml", graphs=GRAPHS).document)
    asserted = dataclasses.replace(maint_actor(), project=who("another_project").project)
    unbound = dataclasses.replace(
        maint_actor(), principal=token_of(None, Entrypoint.MAINT).principal
    )
    named = INSTALLATION + b"maintainers:\n  - {name: somebody_else}\n"
    strict = policy_set(
        compile_policy(
            named.replace(
                b"another_project: {other_graph: write}",
                b"another_project: {other_graph: maintain}",
            ),
            file="i.yaml",
            graphs=GRAPHS,
        ).document
    )
    for subject in (asserted, unbound):
        decision = authorize(subject, "maint.reindex", f"graph:{OTHER}", CTX, strict)
        assert (decision.effect, decision.rule_id) == ("deny", "default.deny")
    # the built-in policy lets every maintainer maintain, but no project admin
    admin = token_of("another_project", Entrypoint.MAINT)
    assert authorize(admin, "maint.reindex", f"graph:{OTHER}", CTX, BUILTIN_POLICY_SET).effect == (
        "deny"
    )
    assert authorize(asserted, "maint.reindex", f"graph:{OTHER}", CTX, policy).effect == "allow"


def test_a_grant_to_a_project_matches_its_tokens_only() -> None:
    """E11: every token bound to the project, never an asserted project."""
    compiled = compile_policy(PROJECTS, file="i.yaml", graphs=GRAPHS)
    rules = {r["id"]: r for r in compiled.document["rules"]}  # type: ignore[index, union-attr]
    assert rules["grant.1"]["principal"] == {"entrypoint": "server", "project": "another_project"}
    bound = token_of("another_project", Entrypoint.SERVER)
    assert levels(bound, compiled.document) == ["none", "write", "none"]
    asserted = served(
        dataclasses.replace(
            token_of(None, Entrypoint.SERVER), project=who("another_project").project
        )
    )
    assert levels(asserted, compiled.document) == ["none", "none", "none"]
    plain = compile_policy(INSTALLATION, file="i.yaml", graphs=GRAPHS)
    assert "project" not in str(plain.document["rules"][0])  # type: ignore[index]


def test_graph_policy() -> None:
    compiled = compile_policy(
        GRAPH_POLICY,
        file="graph.yaml",
        graphs=GRAPHS,
        graph=EXAMPLE,
        known_types=frozenset({"inv:Item", "inv:Location"}),
    )
    assert compiled.layer == "graph"
    assert compiled.unknown_types == ("files:File",)  # not in this model: a warning, not an error
    layer = compiled.document
    assert layer["types"] == {"files:File": "read"}
    policy = policy_set(installation(), layer)
    writer = who("example_project")
    file_type = f"graph:{EXAMPLE}/type:files:File"
    assert authorize(writer, "write.create", file_type, CTX, policy).rule_id == "type.files:File"
    assert authorize(writer, "read.get", file_type, CTX, policy).effect == "allow"
    item = f"graph:{EXAMPLE}/type:inv:Item"
    unreasoned = dataclasses.replace(CTX, has_reason=False)
    assert authorize(writer, "write.transition", item, unreasoned, policy).code == (
        "access.reason_required"
    )
    big = dataclasses.replace(CTX, op_count=201)
    assert authorize(writer, "write.create", item, big, policy).code == "access.batch_too_large"
    explorer = who("example_project", agent_type="explore")
    assert authorize(explorer, "write.retract", item, CTX, policy).rule_id == (
        "deny.no_explore_writes"  # the installation deny comes first
    )
    assert levels(writer, installation(), layer) == ["write", "read", "none"]


def test_compiling_is_deterministic() -> None:
    first = compile_policy(INSTALLATION, file="a.yaml", graphs=GRAPHS)
    second = compile_policy(INSTALLATION, file="b.yaml", graphs=dict(reversed(GRAPHS.items())))
    assert first.hash == second.hash


def test_layer_of_source() -> None:
    assert layer_of_source(INSTALLATION) == "installation"
    assert layer_of_source(GRAPH_POLICY) == "graph"
    assert layer_of_source(b"id: [") is None
    assert layer_of_source(b"id: other") is None


@pytest.mark.parametrize(
    ("source", "path"),
    [
        (INSTALLATION.replace(b'no_project: {"*": read}\n', b""), "no_project"),
        (INSTALLATION.replace(b"level: write", b"level: admin"), "grants[0].level"),
        (
            INSTALLATION.replace(b"other_graph: read}", b"missing_graph: read}"),
            "scopes.example_project.missing_graph",
        ),
        (INSTALLATION.replace(b"example_project:", b"'bad project':"), "scopes.bad project"),
        (
            INSTALLATION.replace(b'action: ["write.*"]', b'action: ["write.teleport"]'),
            "deny[0].action",
        ),
        (INSTALLATION.replace(b"grants:", b"granted:"), "granted"),
        (INSTALLATION + b"deny_more: []\n", "deny_more"),
        (INSTALLATION.replace(b"id: installation", b"id: everything"), "id"),
        (INSTALLATION.replace(b"graph_cli.policy/1", b"graph_cli.policy/2"), "schema"),
        (
            INSTALLATION.replace(b"{name: tester}", b"{agent_type: explore}"),
            "grants[0].principal.agent_type",
        ),
        (INSTALLATION.replace(b"id: no_explore_writes", b"id: No-Explore"), "deny[0].id"),
        (GRAPH_POLICY.replace(b'"files:File": read', b'"files:File": hidden'), "types.files:File"),
        (GRAPH_POLICY.replace(b"max_batch: 200", b"max_batch: 0"), "obligations[1].max_batch"),
        (GRAPH_POLICY.replace(b'type: ["inv:*"]', b'graph: "*"'), "deny[0].graph"),
        # a type decides writes and read.get only, obligations decide writes only
        (
            GRAPH_POLICY.replace(b"action: [write.retract]", b"action: [read.find]"),
            "deny[0].action",
        ),
        (GRAPH_POLICY.replace(b"action: [write.retract]", b'action: ["*"]'), "deny[0].action"),
        (GRAPH_POLICY.replace(b'action: ["write.*"]', b'action: ["*"]'), "obligations[1].action"),
        (
            INSTALLATION + b'obligations:\n  - {action: ["read.get"], require_reason: true}\n',
            "obligations[0].action",
        ),
        (
            INSTALLATION.replace(b"other_graph: read}", b'"*": maintain}'),
            "scopes.example_project.*",
        ),
        (
            INSTALLATION.replace(b'{"*": read}', b"{example_graph: maintain}"),
            "no_project.example_graph",
        ),
        (
            INSTALLATION.replace(b"{name: tester}", b"{project: example_project}"),
            "grants[0].entrypoint",
        ),
        (
            INSTALLATION.replace(b"{name: tester}", b"{project: 'bad project'}"),
            "grants[0].principal.project",
        ),
        (
            INSTALLATION + b"maintainers:\n  - {project: example_project}\n",
            "maintainers[0].project",
        ),
        (b"[1, 2]", ""),
        (b"schema: graph_cli.policy/1\nid: installation\nid: graph\n", ""),
    ],
)
def test_invalid_policies_list_their_problems(source: bytes, path: str) -> None:
    with pytest.raises(GraphCliError) as info:
        compile_policy(
            source, file="p.yaml", graphs=GRAPHS, graph=EXAMPLE if b"id: graph" in source else None
        )
    assert info.value.code == "policy.invalid"
    assert info.value.details["file"] == "p.yaml"
    problems = info.value.details["problems"]
    assert isinstance(problems, list)
    assert path in [p["path"] for p in problems if isinstance(p, dict)], problems


def test_a_type_limit_names_concrete_types() -> None:
    """A limit matches the exact type, so a superclass would miss its subclasses."""
    for source, path in (
        (GRAPH_POLICY.replace(b'"files:File": read', b'"inv:Thing": read'), "types.inv:Thing"),
        (GRAPH_POLICY.replace(b'type: ["inv:*"]', b'type: ["inv:Thing"]'), "deny[0].type"),
    ):
        with pytest.raises(GraphCliError) as info:
            compile_policy(
                source,
                file="graph.yaml",
                graphs=GRAPHS,
                graph=EXAMPLE,
                known_types=frozenset({"inv:Thing", "inv:Item"}),
                extended_types=frozenset({"inv:Thing"}),
            )
        problems = info.value.details["problems"]
        assert isinstance(problems, list)
        assert path in [p["path"] for p in problems if isinstance(p, dict)], problems


def test_a_graph_named_default_gets_its_own_rule() -> None:
    graphs = {**GRAPHS, "default": GraphId(parse_id("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8d"))}
    source = INSTALLATION.replace(b"{other_graph: write}", b"{default: read}")
    compiled = compile_policy(source, file="p.yaml", graphs=graphs)
    ids = [r["id"] for r in compiled.document["rules"] if isinstance(r, dict)]
    assert "scope.another_project.default" in ids
    assert "scope.another_project.*" in ids


def test_every_problem_is_reported_at_once() -> None:
    broken = INSTALLATION.replace(b"level: write", b"level: admin").replace(
        b'no_project: {"*": read}\n', b""
    )
    with pytest.raises(GraphCliError) as info:
        compile_policy(broken, file="p.yaml", graphs=GRAPHS)
    paths = [p["path"] for p in info.value.details["problems"]]  # type: ignore[index, union-attr]
    assert {"grants[0].level", "no_project"} <= set(paths)


def test_a_graph_policy_needs_its_graph() -> None:
    with pytest.raises(GraphCliError) as info:
        compile_policy(GRAPH_POLICY, file="p.yaml", graphs=GRAPHS)
    assert info.value.code == "policy.invalid"
