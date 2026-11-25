"""Roles as a policy subject (contracts B6.6, B6.7, B6.13).

A role is declared by the installation policy, held by a server token and named by
grants and deny rules. The authored form is compiled and then evaluated with
``authorize()``, so the cases show what a policy means for a token with or without a role.
"""

import dataclasses

import pytest
from support.txbuilder import actor

from graph_cli.access_control.authorize import authorize
from graph_cli.access_control.compiler import compile_policy
from graph_cli.access_control.documents import declared_roles, document_problems
from graph_cli.access_control.effective import graph_level
from graph_cli.access_control.policy import policy_set
from graph_cli.kernel.actor import ActorContext, Assurance, ClientInfo, Entrypoint, Facet
from graph_cli.kernel.authz import AuthzContext
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GraphId, parse_id

EXAMPLE = GraphId(parse_id("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a"))
OTHER = GraphId(parse_id("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8b"))
GRAPHS = {"example_graph": EXAMPLE, "other_graph": OTHER}
CTX = AuthzContext(op_count=1, has_reason=True, dry_run=False)
RES_EXAMPLE = f"graph:{EXAMPLE}"
RES_OTHER = f"graph:{OTHER}"
TYPED = f"graph:{EXAMPLE}/type:ex:Finding"

INSTALLATION = b"""\
schema: graph_cli.policy/1
id: installation
roles:
  reader: {description: "Reporting apps: read every graph"}
  importer: {description: "Importers: write into example_graph"}
grants:
  - {entrypoint: server, principal: {role: reader}, graphs: "*", level: read}
  - {entrypoint: server, principal: {role: importer}, graphs: [example_graph], level: write}
  - {entrypoint: server, principal: {role: reader, project: example_project},
     graphs: [other_graph], level: write}
no_project: {"*": write}
deny:
  - {id: importer_no_retract, role: [importer], action: ["write.retract", "write.supersede"]}
"""

GRAPH_POLICY = b"""\
schema: graph_cli.policy/1
id: graph
deny:
  - {id: importer_findings_only, role: [importer], action: ["write.*"], type: ["ex:Decision"]}
"""

# The installation policy and graph policy of the byte-identity test: every authored feature
# that existed before roles (grants of all kinds, maintainers, scopes, maintain, deny rules,
# obligations). Their hashes were computed on the code before roles and are pinned here.
PRE_ROLES_INSTALLATION = b"""\
schema: graph_cli.policy/1
id: installation
grants:
  - {principal: {name: tester}, graphs: "*", level: write}
  - {entrypoint: server, principal: {project: example_project},
     graphs: [example_graph], level: write}
  - {entrypoint: server, principal: {id: "token:ci"}, graphs: [example_graph], level: read}
  - {entrypoint: [server, app], graphs: [other_graph], level: read}
maintainers:
  - {name: boss}
scopes:
  example_project: {example_graph: write, other_graph: read, third_graph: maintain}
  another_project: {other_graph: write}
no_project: {"*": read}
deny:
  - {id: no_explore_writes, agent_type: [explore], action: ["write.*"]}
  - {id: no_server_retract, entrypoint: server, principal_name: ci,
     action: ["write.retract"], graph: [example_graph]}
obligations:
  - {action: ["write.*"], max_batch: 100}
"""
PRE_ROLES_GRAPH = b"""\
schema: graph_cli.policy/1
id: graph
types:
  "files:File": read
obligations:
  - {action: ["write.transition"], require_reason: true}
deny:
  - {id: no_retract_by_explore, agent_type: explore, action: [write.retract], type: ["inv:*"]}
"""
THIRD = GraphId(parse_id("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8c"))
PRE_ROLES_INSTALLATION_HASH = (
    "sha256:341e48abf1fc83a583a6859197b554535c1469f0c9b43cedaa1fb4054cdb3bcb"
)
PRE_ROLES_GRAPH_HASH = "sha256:9bb9346e7c0b28fcdb1338df9220f26302b7817b37d5df7ddc2b0805311bc3c3"

# A grant-heavy pair: app and server grants, a project and role grants, named graphs and
# ``*``, both levels, every scope level, deny rules with and without a graph, obligations;
# on the graph layer type limits, a prefix limit and typed deny rules. Hashes computed on the
# code before ``writes`` and term deny rules existed: policies that use neither compile alike.
GRANT_HEAVY_INSTALLATION = b"""\
schema: graph_cli.policy/1
id: installation
roles:
  importer: {description: "Importers: findings in example_graph"}
  tagger: {description: "Taggers: read every graph"}
grants:
  - {principal: {name: tester}, graphs: "*", level: write}
  - {entrypoint: app, graphs: [example_graph, other_graph], level: read}
  - {entrypoint: server, principal: {project: example_project},
     graphs: [example_graph], level: write}
  - {entrypoint: server, principal: {role: importer}, graphs: [example_graph], level: write}
  - {entrypoint: server, principal: {role: tagger}, graphs: "*", level: read}
  - {entrypoint: server, principal: {role: tagger, project: example_project},
     graphs: [other_graph, third_graph], level: write}
  - {entrypoint: [server, app], principal: {id: "token:ci"}, graphs: [third_graph], level: read}
maintainers:
  - {name: boss}
scopes:
  example_project: {example_graph: write, other_graph: read, third_graph: maintain}
  another_project: {other_graph: write, "*": read}
no_project: {"*": read, third_graph: none}
deny:
  - {id: no_explore_writes, agent_type: [explore], action: ["write.*"]}
  - {id: no_server_retract, entrypoint: server, principal_name: ci,
     action: ["write.retract"], graph: [example_graph]}
  - {id: importer_no_retract, role: [importer], action: ["write.retract", "write.supersede"]}
  - {id: no_device_unlink, device_id: dev-1, action: ["write.unlink"], graph: "*"}
obligations:
  - {action: ["write.*"], max_batch: 100}
  - {action: ["write.retract"], require_reason: true}
"""
GRANT_HEAVY_GRAPH = b"""\
schema: graph_cli.policy/1
id: graph
types:
  "files:File": read
  "inv:*": read
  "ex:Finding": write
obligations:
  - {action: ["write.transition"], require_reason: true}
deny:
  - {id: no_retract_by_explore, agent_type: explore, action: [write.retract], type: ["inv:*"]}
  - {id: no_decision_writes, role: [importer], action: ["write.*"],
     type: ["ex:Decision", "ex:Note"]}
  - {id: no_alias, agent_type: [explore], action: ["write.alias", "write.supersede"],
     type: ["ex:*"]}
"""
GRANT_HEAVY_INSTALLATION_HASH = (
    "sha256:d9a8bc9c978df9be1d65073fd5da3c956c66f2ca93c73708ec22d779ace68700"
)
GRANT_HEAVY_GRAPH_HASH = "sha256:b380931d2493b70746327335f9ecbb999551e91028d92c16fbcaa80469a95268"


def token(*roles: str, project: str | None = None) -> ActorContext:
    """A server request whose token holds ``roles`` (and is bound to ``project``)."""
    bound = Facet(Assurance.VERIFIED, id=project, source="token") if project else actor().project
    return dataclasses.replace(
        actor(),
        principal=Facet(Assurance.VERIFIED, id="token:t", name="t", source="token"),
        project=bound,
        client=ClientInfo("graph-server", "0.1.0", Entrypoint.SERVER),
        roles=roles,
    )


def compiled() -> dict[str, JsonValue]:
    return compile_policy(INSTALLATION, file="installation.yaml", graphs=GRAPHS).document


def decide(who: ActorContext, action: str, resource: str, *documents: dict[str, JsonValue]) -> str:
    """``effect:rule`` of one decision under the installation policy plus ``documents``."""
    policy = policy_set(compiled(), *documents)
    decision = authorize(who, action, resource, CTX, policy)
    return f"{decision.effect}:{decision.rule_id}"


def graph_layer() -> dict[str, JsonValue]:
    return compile_policy(
        GRAPH_POLICY, file="graph.yaml", graphs=GRAPHS, graph=EXAMPLE, roles=frozenset({"importer"})
    ).document


# (who, action, resource, expected "effect:rule")
TABLE: list[tuple[ActorContext, str, str, str]] = [
    # a role grant allows what it names, nothing else
    (token("reader"), "read.get", RES_EXAMPLE, "allow:grant.1"),
    (token("reader"), "read.get", RES_OTHER, "allow:grant.1"),
    (token("reader"), "write.create", RES_EXAMPLE, "deny:default.deny"),
    (token("importer"), "write.create", RES_EXAMPLE, "allow:grant.2"),
    (token("importer"), "write.create", RES_OTHER, "deny:default.deny"),
    # role and project must both match
    (token("reader", project="example_project"), "write.create", RES_OTHER, "allow:grant.3"),
    (token("reader", project="another_project"), "write.create", RES_OTHER, "deny:default.deny"),
    (token("importer", project="example_project"), "write.create", RES_OTHER, "deny:default.deny"),
    # a deny rule by role wins over the grant of the same role
    (token("importer"), "write.retract", RES_EXAMPLE, "deny:deny.importer_no_retract"),
    (token("importer"), "write.update", RES_EXAMPLE, "allow:grant.2"),
    # holding one of several roles is enough; a rule of a role the token lacks is not met
    (token("importer", "reader"), "read.get", RES_OTHER, "allow:grant.1"),
    (token("importer", "reader"), "write.retract", RES_EXAMPLE, "deny:deny.importer_no_retract"),
    (token("reader"), "write.retract", RES_EXAMPLE, "deny:default.deny"),
    # a token without the role, and an actor without roles, never match a role rule
    (token(), "read.get", RES_EXAMPLE, "deny:default.deny"),
    (token("other_role"), "read.get", RES_EXAMPLE, "deny:default.deny"),
    (actor(), "read.get", RES_EXAMPLE, "deny:default.deny"),
]


@pytest.mark.parametrize(("who", "action", "resource", "expected"), TABLE)
@pytest.mark.doc_claim("security_model.roles_verified")
def test_role_decisions(who: ActorContext, action: str, resource: str, expected: str) -> None:
    assert decide(who, action, resource) == expected


def test_a_graph_layer_deny_names_roles() -> None:
    layer = graph_layer()
    assert document_problems(layer) == []
    importer = token("importer")
    assert decide(importer, "write.create", TYPED, layer) == "allow:grant.2"
    decision = decide(importer, "write.create", f"graph:{EXAMPLE}/type:ex:Decision", layer)
    assert decision == "deny:deny.importer_findings_only"
    # another role and a token without roles are not affected by the importer's rule
    assert decide(token("reader"), "read.get", RES_EXAMPLE, layer) == "allow:grant.1"


def test_levels_follow_the_roles() -> None:
    policy = policy_set(compiled())
    levels = [
        [graph_level(who, g, policy) for g in (EXAMPLE, OTHER)]
        for who in (token("reader"), token("importer"), token())
    ]
    # the importer's own deny rule takes write.retract away from its write grant
    assert levels == [["read", "read"], ["read", "none"], ["none", "none"]]


def test_the_role_matcher_is_a_verified_key() -> None:
    """An allow rule may name a role (B6.7); an actor's roles are matched as they are."""
    document = compiled()
    assert document_problems(document) == []
    assert document["roles"] == {
        "importer": {"description": "Importers: write into example_graph"},
        "reader": {"description": "Reporting apps: read every graph"},
    }
    assert declared_roles(document) == {"importer", "reader"}
    rules = {r["id"]: r for r in document["rules"]}  # type: ignore[index, union-attr]
    assert rules["grant.1"]["principal"] == {"entrypoint": "server", "role": "reader"}
    assert rules["deny.importer_no_retract"]["principal"] == {"role": ["importer"]}
    assert rules["grant.3"]["principal"] == {
        "entrypoint": "server",
        "role": "reader",
        "project": "example_project",
    }


@pytest.mark.parametrize(
    ("source", "path"),
    [
        # an undeclared role in a grant and in a deny rule is a typo
        (
            INSTALLATION.replace(b'role: reader}, graphs: "*"', b'role: reeder}, graphs: "*"'),
            "grants[0].principal.role",
        ),
        (INSTALLATION.replace(b"role: [importer]", b"role: [importer, nobody]"), "deny[0].role"),
        # a role grant needs the server entrypoint
        (
            INSTALLATION.replace(
                b"entrypoint: server, principal: {role: reader},", b"principal: {role: reader},"
            ),
            "grants[0].entrypoint",
        ),
        (
            INSTALLATION.replace(
                b"entrypoint: server, principal: {role: reader},",
                b"entrypoint: app, principal: {role: reader},",
            ),
            "grants[0].entrypoint",
        ),
        # the name grammar, for declarations, grants and deny rules
        (INSTALLATION.replace(b"  reader: {", b"  Bad-Role: {"), "roles.Bad-Role"),
        (INSTALLATION.replace(b"  reader: {", b"  " + b"r" * 65 + b": {"), "roles." + "r" * 65),
        (INSTALLATION.replace(b"role: [importer]", b"role: [Importer]"), "deny[0].role"),
        # a declaration needs a description
        (
            INSTALLATION.replace(
                b'reader: {description: "Reporting apps: read every graph"}', b"reader: {}"
            ),
            "roles.reader.description",
        ),
        (
            INSTALLATION.replace(
                b'description: "Reporting apps: read every graph"', b'description: ""'
            ),
            "roles.reader.description",
        ),
        (
            INSTALLATION.replace(
                b'reader: {description: "Reporting apps: read every graph"}',
                b"reader: {description: x, color: red}",
            ),
            "roles.reader.color",
        ),
        (
            INSTALLATION.replace(
                b'reader: {description: "Reporting apps: read every graph"}', b"reader: text"
            ),
            "roles.reader",
        ),
        # maintainers are not roles (stage 1 is for app tokens)
        (INSTALLATION + b"maintainers:\n  - {role: reader}\n", "maintainers[0].role"),
        # a policy without declarations cannot name a role
        (
            b"\n".join(
                line
                for line in INSTALLATION.split(b"\n")
                if b"roles:" not in line
                and b"reader: {d" not in line
                and b"importer: {d" not in line
            ),
            "grants[0].principal.role",
        ),
    ],
)
def test_invalid_role_policies_list_their_problems(source: bytes, path: str) -> None:
    with pytest.raises(GraphCliError) as info:
        compile_policy(source, file="p.yaml", graphs=GRAPHS)
    assert info.value.code == "policy.invalid"
    problems = info.value.details["problems"]
    assert isinstance(problems, list)
    assert path in [p["path"] for p in problems if isinstance(p, dict)], problems


def test_roles_are_refused_on_the_graph_layer() -> None:
    source = GRAPH_POLICY + b"roles:\n  reader: {description: x}\n"
    with pytest.raises(GraphCliError) as info:
        compile_policy(source, file="g.yaml", graphs=GRAPHS, graph=EXAMPLE)
    problems = info.value.details["problems"]
    assert isinstance(problems, list)
    assert [p["path"] for p in problems if isinstance(p, dict)] == ["roles"]
    # the compiled grammar refuses it, too
    layer = graph_layer()
    assert [p.path for p in document_problems({**layer, "roles": {}})] == ["roles"]


def test_graph_layer_roles_are_collected_not_refused() -> None:
    unchecked = compile_policy(GRAPH_POLICY, file="g.yaml", graphs=GRAPHS, graph=EXAMPLE)
    assert unchecked.unknown_roles == ()  # roles=None: not checked
    declared = compile_policy(
        GRAPH_POLICY, file="g.yaml", graphs=GRAPHS, graph=EXAMPLE, roles=frozenset({"importer"})
    )
    assert declared.unknown_roles == ()
    missing = compile_policy(
        GRAPH_POLICY, file="g.yaml", graphs=GRAPHS, graph=EXAMPLE, roles=frozenset()
    )
    assert missing.unknown_roles == ("importer",)
    assert missing.hash == unchecked.hash  # the warning does not change the document
    bad = GRAPH_POLICY.replace(b"role: [importer]", b"role: [Not-A-Role]")
    with pytest.raises(GraphCliError):  # the grammar is still checked
        compile_policy(bad, file="g.yaml", graphs=GRAPHS, graph=EXAMPLE, roles=frozenset())


def test_a_policy_without_roles_compiles_to_the_same_hash_as_before() -> None:
    """Pinned on the code before roles existed: installed policies keep their hash and a
    compiled document without declared roles carries no ``roles`` key."""
    graphs = {**GRAPHS, "third_graph": THIRD}
    installation = compile_policy(PRE_ROLES_INSTALLATION, file="i.yaml", graphs=graphs)
    assert installation.hash == PRE_ROLES_INSTALLATION_HASH
    assert "roles" not in installation.document
    graph = compile_policy(PRE_ROLES_GRAPH, file="g.yaml", graphs=graphs, graph=EXAMPLE)
    assert graph.hash == PRE_ROLES_GRAPH_HASH
    empty = PRE_ROLES_INSTALLATION.replace(b"grants:", b"roles: {}\ngrants:")
    assert compile_policy(empty, file="i.yaml", graphs=graphs).hash == PRE_ROLES_INSTALLATION_HASH


def test_a_grant_heavy_policy_compiles_to_the_same_hash_as_before_writes() -> None:
    """Pinned on the code before write allow lists: no ``writes`` and no term deny keys
    means the same compiled document, hence the same hash."""
    graphs = {**GRAPHS, "third_graph": THIRD}
    installation = compile_policy(GRANT_HEAVY_INSTALLATION, file="i.yaml", graphs=graphs)
    assert installation.hash == GRANT_HEAVY_INSTALLATION_HASH
    graph = compile_policy(
        GRANT_HEAVY_GRAPH,
        file="g.yaml",
        graphs=graphs,
        graph=EXAMPLE,
        roles=frozenset({"importer"}),
    )
    assert graph.hash == GRANT_HEAVY_GRAPH_HASH


# A policy that uses ``reads``, ``writes`` and ``markings`` together (field visibility, step 2).
# The hash is pinned: the compiled form of ``markings`` (rule ``grant.<n>.markings`` after the
# read rules and before the write rules, sorted exact marking resources) must not drift, as a
# policy installed with markings keeps its hash through later releases.
MARKINGS_INSTALLATION = b"""\
schema: graph_cli.policy/1
id: installation
roles:
  hr: {description: "HR: personal data of people"}
  reporting: {description: "Reporting: people without personal data"}
grants:
  - {principal: {name: tester}, graphs: "*", level: write}
  - entrypoint: server
    principal: {role: hr}
    graphs: [example_graph, other_graph]
    level: read
    reads: ["ex:*", "files:File"]
    writes:
      ex:Person:
        create: [name, email]
        update: [email, team]
        supersede: true
    markings: [ex:team_internal, ex:personal]
  - {entrypoint: server, principal: {role: reporting}, graphs: [example_graph], level: write,
     markings: [ex:personal]}
maintainers:
  - {name: boss}
no_project: {"*": read}
"""
MARKINGS_INSTALLATION_HASH = (
    "sha256:c499430a79c979581e87e3d0aa8306a6431e8b444c5278d011e36ed6112839fe"
)


def test_a_policy_with_reads_writes_and_markings_keeps_its_pinned_hash() -> None:
    compiled = compile_policy(MARKINGS_INSTALLATION, file="i.yaml", graphs=GRAPHS)
    assert compiled.hash == MARKINGS_INSTALLATION_HASH
    ids = [r["id"] for r in compiled.document["rules"]]  # type: ignore[union-attr,index]
    assert ids[:6] == [
        "grant.1",
        "grant.2",
        "grant.2.reads",
        "grant.2.markings",
        "grant.2.create",
        "grant.2.update",
    ]
    assert "grant.3.markings" in ids
