"""Resources below a type: attributes and relations (contracts B6.9, B6.13).

The grammar, ``fine_types`` and table-driven ``authorize`` decisions on compiled ``writes``
grants and term deny rules, with the scopes and type limits that narrow them.
"""

import dataclasses

import pytest
from support.txbuilder import actor

from graph_cli.access_control.authorize import authorize
from graph_cli.access_control.compiler import compile_policy
from graph_cli.access_control.documents import (
    RESOURCE_RE,
    document_problems,
    fine_types,
    glob,
    is_resource_glob,
)
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
NOTE = f"graph:{EXAMPLE}/type:nas:Note"
TOPIC = f"graph:{EXAMPLE}/type:nas:Topic"


def attr(name: str, type_resource: str = NOTE) -> str:
    return f"{type_resource}/attr:{name}"


def rel(name: str, type_resource: str = NOTE) -> str:
    return f"{type_resource}/rel:{name}"


# ---- grammar -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "resource",
    [
        f"graph:{EXAMPLE}",
        NOTE,
        attr("title"),
        attr("a_1"),
        rel("nas:about"),
        rel("core:supersedes", TOPIC),
    ],
)
def test_resources_name_a_graph_a_type_an_attribute_or_a_relation(resource: str) -> None:
    assert RESOURCE_RE.match(resource)


@pytest.mark.parametrize(
    "resource",
    [
        f"graph:{EXAMPLE}/attr:title",  # a term needs its type
        f"graph:{EXAMPLE}/rel:nas:about",
        attr("Title"),  # attributes are lower names
        attr("1st"),
        attr(""),
        attr("title/more"),
        rel("about"),  # a relation is a CURIE
        rel("nas:"),
        rel("nas:*"),  # concrete resources never carry a glob
        attr("*"),
        f"{NOTE}/state:published",
        f"{NOTE}/",
        f"graph:{EXAMPLE}/type:nas:Note/attr:title/rel:nas:about",
    ],
)
def test_resources_refuse_other_shapes(resource: str) -> None:
    assert not RESOURCE_RE.match(resource)


def test_authorize_refuses_a_malformed_resource_as_a_bug() -> None:
    policy = policy_set(compile_installation())
    with pytest.raises(GraphCliError) as info:
        authorize(token("importer"), "write.update", attr("Bad"), CTX, policy)
    assert info.value.code == "internal.error"


@pytest.mark.parametrize(
    "pattern",
    [
        f"graph:{EXAMPLE}*",
        f"graph:{EXAMPLE}/type:*",
        f"graph:{EXAMPLE}/type:nas:*",
        NOTE,
        f"{NOTE}/attr:*",
        f"{NOTE}/rel:*",
        attr("title"),
        rel("nas:about"),
    ],
)
def test_resource_globs_know_the_levels_below_a_type(pattern: str) -> None:
    assert is_resource_glob(pattern)


@pytest.mark.parametrize(
    "pattern",
    [
        f"{NOTE}/attr:ti*",  # a glob below a type ends at attr:* or rel:*
        f"{NOTE}/rel:nas:*",
        f"{NOTE}*",
        f"{NOTE}/attr:Title",
        f"{NOTE}/*",
        f"graph:{EXAMPLE}/type:*/attr:title",
        f"graph:{EXAMPLE}/attr:*",
    ],
)
def test_resource_globs_refuse_other_shapes(pattern: str) -> None:
    assert not is_resource_glob(pattern)


@pytest.mark.parametrize(
    ("pattern", "resource", "matches"),
    [
        # the existing globs keep their meaning and cover the terms of the types they cover
        ("*", attr("title"), True),
        ("graph:*", attr("title"), True),
        (f"graph:{EXAMPLE}*", attr("title"), True),
        (f"graph:{EXAMPLE}*", rel("nas:about"), True),
        (f"graph:{EXAMPLE}/type:*", attr("title"), True),
        (f"graph:{EXAMPLE}/type:nas:*", rel("nas:about"), True),
        (f"graph:{EXAMPLE}/type:nasa:*", attr("title"), False),  # a prefix ends in ":"
        (f"graph:{OTHER}*", attr("title"), False),
        # an exact type resource matches the type only, never its terms (W3)
        (NOTE, NOTE, True),
        (NOTE, attr("title"), False),
        (NOTE, rel("nas:about"), False),
        # a term glob covers one type's terms of its kind
        (f"{NOTE}/attr:*", attr("title"), True),
        (f"{NOTE}/attr:*", rel("nas:about"), False),
        (f"{NOTE}/attr:*", NOTE, False),
        (f"{NOTE}/attr:*", attr("title", TOPIC), False),
        (f"{NOTE}/rel:*", rel("nas:about"), True),
        (attr("title"), attr("title"), True),
        (attr("title"), attr("title_2"), False),
    ],
)
def test_globs_match_terms_by_prefix(pattern: str, resource: str, matches: bool) -> None:
    assert glob(pattern, resource) is matches


def test_rules_on_terms_name_write_actions_only() -> None:
    def document(effect: str, action: str, resource: str) -> dict[str, JsonValue]:
        rule: dict[str, JsonValue] = {
            "id": "r",
            "effect": effect,
            "action": [action],
            "resource": resource,
        }
        return {
            "schema": "graph_cli.policy/1",
            "id": "installation",
            "layer": "installation",
            "projects": [],
            "rules": [rule],
            "obligations": [],
        }

    assert document_problems(document("deny", "write.update", attr("title"))) == []
    assert document_problems(document("allow", "write.create", f"{NOTE}/attr:*")) == []
    for action in ("read.get", "*", "ext.notes.tag", "maint.repair"):
        found = document_problems(document("deny", action, rel("nas:about")))
        assert [p.path for p in found] == ["rules[0].action"], action


# ---- fine_types (W7) ---------------------------------------------------------------------


def test_fine_types_are_the_types_some_rule_names_terms_of() -> None:
    installation = compile_installation()
    assert fine_types([installation], EXAMPLE) == {"nas:Note", "nas:Topic"}
    assert fine_types([installation], str(EXAMPLE)) == {"nas:Note", "nas:Topic"}
    assert fine_types([installation], OTHER) == frozenset()  # no rule names that graph
    assert fine_types([], EXAMPLE) == frozenset()
    layers = [installation, compile_graph(GRAPH_TERMS)]
    assert fine_types(layers, EXAMPLE) == {"nas:Note", "nas:Topic", "nas:Scan"}


def test_broad_globs_and_exact_types_name_no_terms() -> None:
    """Only a resource below an exact type counts: the type decision decides the rest."""
    limit = compile_graph(GRAPH_LIMIT)
    assert fine_types([limit], EXAMPLE) == frozenset()
    broad: dict[str, JsonValue] = {
        "rules": [
            {
                "id": "a",
                "effect": "deny",
                "action": ["write.*"],
                "resource": [
                    f"graph:{EXAMPLE}*",
                    f"graph:{EXAMPLE}/type:*",
                    f"graph:{EXAMPLE}/type:nas:*",
                ],
            }
        ]
    }
    assert fine_types([broad], EXAMPLE) == frozenset()


def test_fine_types_look_at_every_rule_effect_and_condition() -> None:
    document: dict[str, JsonValue] = {
        "rules": [
            {  # an allow rule
                "id": "a",
                "effect": "allow",
                "action": ["write.update"],
                "resource": f"graph:{EXAMPLE}/type:nas:Note/attr:*",
            },
            {  # a deny rule with the terms of two graphs; only this graph counts
                "id": "b",
                "effect": "deny",
                "action": ["write.link"],
                "resource": [
                    f"graph:{OTHER}/type:nas:Topic/rel:*",
                    f"graph:{EXAMPLE}/type:nas:Scan/rel:nas:about",
                ],
            },
            {  # a term only in an exception
                "id": "c",
                "effect": "deny",
                "action": ["write.*"],
                "resource": f"graph:{EXAMPLE}*",
                "unless": [
                    {"principal": {"role": "importer"}},
                    {"resource": f"graph:{EXAMPLE}/type:nas:Topic/attr:label"},
                ],
            },
        ]
    }
    assert fine_types([document], EXAMPLE) == {"nas:Note", "nas:Scan", "nas:Topic"}
    assert fine_types([document], OTHER) == {"nas:Topic"}
    # a document may come from another layer; each is read on its own
    assert fine_types([document, {"rules": []}], EXAMPLE) == {"nas:Note", "nas:Scan", "nas:Topic"}


# ---- decisions ---------------------------------------------------------------------------

INSTALLATION = """\
schema: graph_cli.policy/1
id: installation
roles:
  importer: {description: "Importers: notes and topics"}
  tagger: {description: "Taggers: retag notes"}
  editor: {description: "Editors: write every graph"}
grants:
  - entrypoint: server
    principal: {role: importer}
    graphs: [example_graph]
    level: read
    writes:
      nas:Note:
        create: [title, body, kind]
        update: [body]
        link: [nas:about]
        transition: true
      nas:Topic: {create: true}
  - entrypoint: server
    principal: {role: tagger}
    graphs: [example_graph]
    level: read
    writes:
      nas:Note: {update: [kind]}
  - {entrypoint: server, principal: {role: editor}, graphs: [example_graph], level: write}
scopes:
  example_project: {example_graph: write}
  another_project: {example_graph: read}
no_project: {"*": read}
"""
# Rule order of the grants above: grant.1.create, .update, .link, .edge, .transition.

GRAPH_TERMS = """\
schema: graph_cli.policy/1
id: graph
deny:
  - {id: no_body, role: [editor], action: [write.update], type: [nas:Note], attribute: [body]}
  - {id: no_scan_title, action: [write.update, write.create], type: [nas:Scan],
     attribute: [title]}
  - {id: no_broader, action: [write.link], type: [nas:Topic], relation: [nas:broader]}
  - {id: no_any_body, action: ["write.*"], type: [nas:Note], attribute: [kind], role: [tagger]}
"""

GRAPH_LIMIT = """\
schema: graph_cli.policy/1
id: graph
types:
  "nas:Note": read
"""


def compile_installation() -> dict[str, JsonValue]:
    return compile_policy(INSTALLATION.encode(), file="i.yaml", graphs=GRAPHS).document


def compile_graph(source: str) -> dict[str, JsonValue]:
    return compile_policy(
        source.encode(),
        file="g.yaml",
        graphs=GRAPHS,
        graph=EXAMPLE,
        roles=frozenset({"importer", "tagger", "editor"}),
    ).document


def token(*roles: str, project: str | None = None) -> ActorContext:
    bound = Facet(Assurance.VERIFIED, id=project, source="token") if project else actor().project
    return dataclasses.replace(
        actor(),
        principal=Facet(Assurance.VERIFIED, id="token:t", name="t", source="token"),
        project=bound,
        client=ClientInfo("graph-server", "0.1.0", Entrypoint.SERVER),
        roles=roles,
    )


def decide(who: ActorContext, action: str, resource: str, *layers: dict[str, JsonValue]) -> str:
    policy = policy_set(compile_installation(), *layers)
    decision = authorize(who, action, resource, CTX, policy)
    return f"{decision.effect}:{decision.rule_id}"


IMPORTER = token("importer", project="example_project")
TAGGER = token("tagger", project="example_project")
BOTH = token("importer", "tagger", project="example_project")
EDITOR = token("editor", project="example_project")
DENIED = "deny:default.deny"

# (who, action, resource, expected "effect:rule"); no graph layer
GRANTS: list[tuple[ActorContext, str, str, str]] = [
    # a create list allows the type and the listed attributes, nothing else
    (IMPORTER, "write.create", NOTE, "allow:grant.1.create"),
    (IMPORTER, "write.create", attr("title"), "allow:grant.1.create"),
    (IMPORTER, "write.create", attr("kind"), "allow:grant.1.create"),
    (IMPORTER, "write.create", attr("status"), DENIED),
    (IMPORTER, "write.create", attr("notes"), DENIED),
    # an update list likewise; the type decision is allowed, an unlisted attribute is not
    (IMPORTER, "write.update", NOTE, "allow:grant.1.update"),
    (IMPORTER, "write.update", attr("body"), "allow:grant.1.update"),
    (IMPORTER, "write.update", attr("kind"), DENIED),
    # link, and the create right on the relation for an inline edge (the edge rule)
    (IMPORTER, "write.link", NOTE, "allow:grant.1.link"),
    (IMPORTER, "write.link", rel("nas:about"), "allow:grant.1.link"),
    (IMPORTER, "write.link", rel("nas:cites"), DENIED),
    (IMPORTER, "write.create", rel("nas:about"), "allow:grant.1.edge"),
    (IMPORTER, "write.create", rel("nas:cites"), DENIED),
    # unlink was not granted, at any level
    (IMPORTER, "write.unlink", NOTE, DENIED),
    (IMPORTER, "write.unlink", rel("nas:about"), DENIED),
    # transition: true allows the type and every lifecycle attribute
    (IMPORTER, "write.transition", NOTE, "allow:grant.1.transition"),
    (IMPORTER, "write.transition", attr("status"), "allow:grant.1.transition"),
    # a verb without terms decides the type only
    (IMPORTER, "write.retract", NOTE, DENIED),
    # create: true allows every attribute of the type, not of another one
    (IMPORTER, "write.create", TOPIC, "allow:grant.1.create"),
    (IMPORTER, "write.create", attr("label", TOPIC), "allow:grant.1.create"),
    (IMPORTER, "write.update", attr("label", TOPIC), DENIED),
    (IMPORTER, "write.create", f"graph:{EXAMPLE}/type:nas:Scan", DENIED),
    (IMPORTER, "write.create", attr("title", f"graph:{EXAMPLE}/type:nas:Scan"), DENIED),
    # the grant reads the graph; another graph is not granted at all
    (IMPORTER, "read.get", f"graph:{EXAMPLE}", "allow:grant.1"),
    (
        IMPORTER,
        "write.create",
        f"graph:{OTHER}/type:nas:Note/attr:title",
        "deny:scope.example_project.*",  # the project scope has no other graph
    ),
    # rights of two roles add up (two grants of one token)
    (BOTH, "write.update", attr("body"), "allow:grant.1.update"),
    (BOTH, "write.update", attr("kind"), "allow:grant.2.update"),
    (BOTH, "write.update", attr("status"), DENIED),
    (TAGGER, "write.update", attr("kind"), "allow:grant.2.update"),
    (TAGGER, "write.update", attr("body"), DENIED),
    (TAGGER, "write.create", attr("title"), DENIED),
    # a level write grant needs no list
    (EDITOR, "write.update", attr("anything"), "allow:grant.3"),
    (EDITOR, "write.link", rel("nas:cites"), "allow:grant.3"),
    # scopes still narrow, as they narrow the type: no project, and a read project
    (token("importer"), "write.create", attr("title"), "deny:no_project.*"),
    (token("importer"), "write.create", NOTE, "deny:no_project.*"),
    (
        token("importer", project="another_project"),
        "write.create",
        attr("title"),
        "deny:scope.another_project.example_graph",
    ),
    (
        token("importer", project="another_project"),
        "write.create",
        rel("nas:about"),
        "deny:scope.another_project.example_graph",
    ),
    (token("importer", project="another_project"), "read.get", f"graph:{EXAMPLE}", "allow:grant.1"),
]


@pytest.mark.parametrize(("who", "action", "resource", "expected"), GRANTS)
def test_a_writes_grant_decides_terms(
    who: ActorContext, action: str, resource: str, expected: str
) -> None:
    assert decide(who, action, resource) == expected


# (who, action, resource, graph layer, expected)
DENIES: list[tuple[ActorContext, str, str, str, str]] = [
    # a term deny wins over a level write grant, for that term only
    (EDITOR, "write.update", attr("body"), GRAPH_TERMS, "deny:deny.no_body"),
    (EDITOR, "write.update", attr("kind"), GRAPH_TERMS, "allow:grant.3"),
    (EDITOR, "write.update", NOTE, GRAPH_TERMS, "allow:grant.3"),
    (EDITOR, "write.create", attr("body"), GRAPH_TERMS, "allow:grant.3"),  # another action
    # a deny rule names its roles: the importer's listed attribute is not the editors'
    (IMPORTER, "write.update", attr("body"), GRAPH_TERMS, "allow:grant.1.update"),  # editors only
    (TAGGER, "write.update", attr("kind"), GRAPH_TERMS, "deny:deny.no_any_body"),
    (TAGGER, "write.update", NOTE, GRAPH_TERMS, "allow:grant.2.update"),
    (BOTH, "write.update", attr("kind"), GRAPH_TERMS, "deny:deny.no_any_body"),
    (BOTH, "write.update", attr("body"), GRAPH_TERMS, "allow:grant.1.update"),
    # a deny on a term of another type never reaches this one
    (IMPORTER, "write.create", attr("title"), GRAPH_TERMS, "allow:grant.1.create"),
    (
        EDITOR,
        "write.create",
        attr("title", f"graph:{EXAMPLE}/type:nas:Scan"),
        GRAPH_TERMS,
        "deny:deny.no_scan_title",
    ),
    (
        EDITOR,
        "write.update",
        attr("title", f"graph:{EXAMPLE}/type:nas:Scan"),
        GRAPH_TERMS,
        "deny:deny.no_scan_title",
    ),
    (
        EDITOR,
        "write.transition",
        attr("title", f"graph:{EXAMPLE}/type:nas:Scan"),
        GRAPH_TERMS,
        "allow:grant.3",
    ),
    # a relation deny on write.link also denies the inline edge (write.create on the relation)
    (EDITOR, "write.link", rel("nas:broader", TOPIC), GRAPH_TERMS, "deny:deny.no_broader"),
    (EDITOR, "write.create", rel("nas:broader", TOPIC), GRAPH_TERMS, "deny:deny.no_broader"),
    (EDITOR, "write.unlink", rel("nas:broader", TOPIC), GRAPH_TERMS, "allow:grant.3"),
    (EDITOR, "write.link", TOPIC, GRAPH_TERMS, "allow:grant.3"),
    # an exact type limit denies the type resource and NOT its terms (W3): the type decision
    # still decides every op, so the outcome is unchanged
    (EDITOR, "write.update", NOTE, GRAPH_LIMIT, "deny:type.nas:Note"),
    (EDITOR, "write.update", attr("kind"), GRAPH_LIMIT, "allow:grant.3"),
    (IMPORTER, "write.create", NOTE, GRAPH_LIMIT, "deny:type.nas:Note"),
    (IMPORTER, "write.create", attr("title"), GRAPH_LIMIT, "allow:grant.1.create"),
    (EDITOR, "write.update", TOPIC, GRAPH_LIMIT, "allow:grant.3"),
    (EDITOR, "read.get", f"graph:{EXAMPLE}", GRAPH_LIMIT, "allow:grant.3"),
]


@pytest.mark.parametrize(("who", "action", "resource", "layer", "expected"), DENIES)
def test_graph_layer_rules_narrow_terms(
    who: ActorContext, action: str, resource: str, layer: str, expected: str
) -> None:
    assert decide(who, action, resource, compile_graph(layer)) == expected
