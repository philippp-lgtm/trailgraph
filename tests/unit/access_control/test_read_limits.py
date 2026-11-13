"""Type-level read limits: ``reads`` on grants, ``none`` and read deny rules, ``read_limited``,
``visibility`` and ``read_allowance`` (type read limits plan V1-V4, V8).

The compiled documents are shown as they are and then evaluated with ``authorize()``: the
visibility of a type is the decision on ``read.get`` for ``graph:<id>/type:<T>``. Types are those
of the toy ontology ``notes_and_sources`` 1.1.0, whose model the model checks use.
"""

import dataclasses
from collections.abc import Callable

import pytest
from support.audit_events import maint_actor
from support.fixtures import compiled_model
from support.txbuilder import actor

from graph_cli.access_control.authorize import authorize
from graph_cli.access_control.compiler import Compiled, compile_policy
from graph_cli.access_control.documents import document_problems, read_limited
from graph_cli.access_control.effective import (
    read_allowance,
    visibility,
    visible_limits,
    write_grant_types,
)
from graph_cli.access_control.policy import BUILTIN_POLICY_SET, policy_set
from graph_cli.access_control.terms import (
    NamedReadType,
    TermModel,
    UnknownReadType,
    named_read_types,
)
from graph_cli.kernel.actor import ActorContext, Assurance, ClientInfo, Entrypoint, Facet
from graph_cli.kernel.authz import AuthzContext
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GraphId, parse_id
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.services.term_model import concrete_types, term_model

EXAMPLE = GraphId(parse_id("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a"))
OTHER = GraphId(parse_id("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8b"))
UNNAMED = GraphId(parse_id("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8c"))  # no rule names its types
GRAPHS = {"example_graph": EXAMPLE, "other_graph": OTHER}
CTX = AuthzContext(op_count=1, has_reason=True, dry_run=False)
MODEL: CompiledModel = compiled_model("notes_and_sources", "1.1.0")
TERMS: TermModel = term_model(MODEL)
CONCRETE = concrete_types(MODEL.classes)

INSTALLATION = """\
schema: graph_cli.policy/1
id: installation
roles:
  reporting: {description: "Reporting: notes and topics only"}
  auditor: {description: "Auditors: read everything"}
  curator: {description: "Curators: read and write books only"}
grants:
  - entrypoint: server
    principal: {role: reporting}
    graphs: [example_graph]
    level: read
    reads: [nas:Topic, nas:Note]
  - {entrypoint: server, principal: {role: auditor}, graphs: [example_graph], level: read}
  - entrypoint: server
    principal: {role: curator}
    graphs: [example_graph, other_graph]
    level: read
    reads: ["nas:*"]
    writes:
      nas:Book: {create: true}
scopes:
  frozen_project: {example_graph: none}
no_project: {"*": read}
"""
GRAPH_LAYER = """\
schema: graph_cli.policy/1
id: graph
types:
  "nas:Scan": none
deny:
  - {id: hide_notes, role: [reporting], action: [read.get], type: [nas:Note]}
"""
ROLES = frozenset({"reporting", "auditor", "curator"})


def compile_installation(
    source: str = INSTALLATION,
    models: Callable[[GraphId], TermModel | None] | None = None,
) -> Compiled:
    return compile_policy(source.encode(), file="installation.yaml", graphs=GRAPHS, models=models)


def compile_graph(source: str = GRAPH_LAYER, **options: object) -> Compiled:
    return compile_policy(
        source.encode(),
        file="graph.yaml",
        graphs=GRAPHS,
        graph=EXAMPLE,
        roles=ROLES,
        **options,  # type: ignore[arg-type]
    )


def rules_of(compiled: Compiled) -> dict[str, dict[str, JsonValue]]:
    found = compiled.document["rules"]
    assert isinstance(found, list)
    return {str(r["id"]): r for r in found if isinstance(r, dict)}


def problems_of(source: str, **options: object) -> list[tuple[str, str]]:
    with pytest.raises(GraphCliError) as info:
        compile_policy(source.encode(), file="p.yaml", graphs=GRAPHS, **options)  # type: ignore[arg-type]
    assert info.value.code == "policy.invalid"
    found = info.value.details["problems"]
    assert isinstance(found, list)
    return [(str(p["path"]), str(p["message"])) for p in found if isinstance(p, dict)]


def type_at(graph: GraphId, curie: str) -> str:
    return f"graph:{graph}/type:{curie}"


def token(
    *roles: str, project: str | None = None, entrypoint: Entrypoint = Entrypoint.SERVER
) -> ActorContext:
    asserted = (
        Facet(Assurance.ASSERTED, id=project, source="pointer") if project else actor().project
    )
    return dataclasses.replace(
        actor(),
        principal=Facet(Assurance.VERIFIED, id="token:t", name="t", source="token"),
        project=asserted,
        client=ClientInfo("graph-server", "0.1.0", entrypoint),
        roles=roles,
    )


REPORTING = token("reporting")
AUDITOR = token("auditor")
BOTH = token("reporting", "auditor")
CURATOR = token("curator")
NOBODY = token()


def policy(*layers: str) -> PolicySet:
    documents = [compile_graph(text).document for text in layers]
    return policy_set(compile_installation().document, *documents)


def decide(who: ActorContext, action: str, resource: str, *layers: str) -> str:
    decision = authorize(who, action, resource, CTX, policy(*layers))
    return f"{decision.effect}:{decision.rule_id}"


# ---- the compiled form (V2) --------------------------------------------------------------


def test_a_reads_grant_names_the_exact_graph_and_one_rule_on_the_listed_types() -> None:
    compiled = compile_installation()
    assert document_problems(compiled.document) == []
    rules = rules_of(compiled)
    # the read rule keeps its level but loses the glob, which would cover every type resource
    assert rules["grant.1"]["resource"] == [f"graph:{EXAMPLE}"]
    assert rules["grant.1"]["principal"] == {"entrypoint": "server", "role": "reporting"}
    assert rules["grant.1.reads"] == {
        "id": "grant.1.reads",
        "effect": "allow",
        "principal": {"entrypoint": "server", "role": "reporting"},
        "action": ["read.get"],
        "resource": [type_at(EXAMPLE, "nas:Note"), type_at(EXAMPLE, "nas:Topic")],
    }
    # a grant without reads is compiled as before: the glob, no reads rule
    assert rules["grant.2"]["resource"] == [f"graph:{EXAMPLE}*"]
    assert "grant.2.reads" not in rules


def test_reads_and_writes_compile_side_by_side_across_graphs() -> None:
    rules = rules_of(compile_installation())
    ids = [i for i in rules if i.startswith("grant.3")]
    assert ids == ["grant.3", "grant.3.reads", "grant.3.create"]
    assert rules["grant.3"]["resource"] == [f"graph:{EXAMPLE}", f"graph:{OTHER}"]
    # a prefix glob is a resource glob; every named graph gets it
    assert rules["grant.3.reads"]["resource"] == [
        type_at(EXAMPLE, "nas:*"),
        type_at(OTHER, "nas:*"),
    ]
    assert rules["grant.3.create"]["resource"] == [
        f"{type_at(EXAMPLE, 'nas:Book')}",
        f"{type_at(EXAMPLE, 'nas:Book')}/attr:*",
        f"{type_at(OTHER, 'nas:Book')}",
        f"{type_at(OTHER, 'nas:Book')}/attr:*",
    ]


def test_the_compiled_document_does_not_depend_on_the_model() -> None:
    bare = compile_installation()
    checked = compile_installation(models=lambda graph: TERMS)
    empty = compile_installation(models=lambda graph: TermModel({}))
    assert checked.document == bare.document and checked.hash == bare.hash
    assert empty.hash == bare.hash
    assert checked.unknown_read_types == ()
    assert set(empty.unknown_read_types) == {
        UnknownReadType(EXAMPLE, "nas:Note"),
        UnknownReadType(EXAMPLE, "nas:Topic"),
        UnknownReadType(EXAMPLE, "nas:*"),
        UnknownReadType(OTHER, "nas:*"),
    }


def one_grant(extra: str, *, level: str = "read", graphs: str = "[example_graph]") -> str:
    return f"""\
schema: graph_cli.policy/1
id: installation
grants:
  - {{graphs: {graphs}, level: {level}, {extra}}}
no_project: {{"*": read}}
"""


@pytest.mark.parametrize(
    ("extra", "options", "path", "fragment"),
    [
        ("reads: [nas:Note]", {"level": "write"}, "grants[0].reads", "needs level: read"),
        ("reads: [nas:Note]", {"graphs": '"*"'}, "grants[0].graphs", "must name graphs"),
        ("reads: []", {}, "grants[0].reads", "non-empty list"),
        ("reads: [Note]", {}, "grants[0].reads[0]", "not a type CURIE"),
        ("reads: [nas:Note, nas:*x]", {}, "grants[0].reads[1]", "not a type CURIE"),
        ("reads: [nas:Note, 7]", {}, "grants[0].reads[1]", "not a type CURIE"),
        (
            "reads: [nas:Note], writes: {nas:Topic: {create: true}}",
            {},
            "grants[0].writes.nas:Topic",
            "not in reads",
        ),
        (
            "reads: [nas:Note], writes: {nas:Note: {create: true}, nas:Topic: {alias: true}}",
            {},
            "grants[0].writes.nas:Topic",
            "cannot write a type it hides",
        ),
    ],
)
def test_invalid_reads_list_their_problem(
    extra: str, options: dict[str, str], path: str, fragment: str
) -> None:
    found = problems_of(one_grant(extra, **options))
    assert any(p == path and fragment in m for p, m in found), found


def test_a_write_type_covered_by_a_reads_glob_is_fine() -> None:
    source = one_grant('reads: ["nas:*"], writes: {nas:Topic: {create: true}}')
    assert compile_installation(source).document["rules"]


def test_every_reads_problem_is_reported_at_once() -> None:
    source = one_grant(
        "reads: [Bad, nas:Note], writes: {nas:Topic: {}}", level="write", graphs='"*"'
    )
    assert {p for p, _ in problems_of(source)} == {
        "grants[0].reads[0]",  # not a type CURIE
        "grants[0].writes.nas:Topic",  # grants no verb
        "grants[0].writes",  # needs level: read
        "grants[0].reads",  # needs level: read
        "grants[0].graphs",  # must name graphs
    }


def test_reads_are_checked_against_the_model_of_each_graph() -> None:
    def models(graph: GraphId) -> TermModel | None:
        return TERMS if graph == EXAMPLE else TermModel({})

    source = one_grant(
        "reads: [nas:Note, nas:Gone, nas:*, zzz:*]", graphs="[example_graph, other_graph]"
    )
    checked = compile_installation(source, models=models)
    # a type or prefix the model lacks, per graph; the empty model of other_graph has none
    assert set(checked.unknown_read_types) == {
        UnknownReadType(EXAMPLE, "zzz:*"),
        UnknownReadType(EXAMPLE, "nas:Gone"),
        UnknownReadType(OTHER, "nas:*"),
        UnknownReadType(OTHER, "nas:Gone"),
        UnknownReadType(OTHER, "nas:Note"),
        UnknownReadType(OTHER, "zzz:*"),
    }


@pytest.mark.parametrize("curie", ["nas:Source", "files:File"])
def test_reads_of_an_abstract_or_extended_type_are_refused(curie: str) -> None:
    source = one_grant(f"reads: [nas:Note, {curie}]")
    found = problems_of(source, models=lambda graph: TERMS)
    assert [p for p, _ in found] == ["grants[0].reads[1]"]
    assert "abstract or has subclasses in graph example_graph" in found[0][1]


# ---- graph layer: none and read deny rules (V3) -------------------------------------------


def test_none_compiles_to_a_deny_that_spares_maintainers() -> None:
    compiled = compile_graph()
    assert document_problems(compiled.document) == []
    assert compiled.document["types"] == {"nas:Scan": "none"}
    rules = rules_of(compiled)
    assert rules["type.nas:Scan"] == {
        "id": "type.nas:Scan",
        "effect": "deny",
        "principal": {"entrypoint": ["app", "server"]},
        "action": ["read.get", "write.*"],
        "resource": [type_at(EXAMPLE, "nas:Scan")],
    }
    # a read limit compiles as before, without a principal
    both = compile_graph('schema: graph_cli.policy/1\nid: graph\ntypes:\n  "nas:Note": read\n')
    assert rules_of(both)["type.nas:Note"] == {
        "id": "type.nas:Note",
        "effect": "deny",
        "action": ["write.*"],
        "resource": [type_at(EXAMPLE, "nas:Note")],
    }


def test_none_names_prefix_globs_and_warns_about_unknown_types() -> None:
    source = 'schema: graph_cli.policy/1\nid: graph\ntypes:\n  "zzz:*": none\n  "nas:Gone": none\n'
    compiled = compile_graph(source, known_types=frozenset({"nas:Note"}))
    assert compiled.unknown_types == ("nas:Gone", "zzz:*")
    assert rules_of(compiled)["type.zzz:*"]["resource"] == [type_at(EXAMPLE, "zzz:*")]


def test_none_of_an_extended_type_is_refused() -> None:
    source = 'schema: graph_cli.policy/1\nid: graph\ntypes:\n  "nas:Source": none\n'
    found = problems_of(source, graph=EXAMPLE, extended_types=frozenset({"nas:Source"}))
    assert [p for p, _ in found] == ["types.nas:Source"]


def test_a_deny_rule_with_type_may_name_read_get_alone_or_with_writes() -> None:
    source = (
        "schema: graph_cli.policy/1\nid: graph\ndeny:\n"
        "  - {id: hide_alone, action: [read.get], type: [nas:Note]}\n"
        "  - {id: hide_mixed, role: [reporting], action: [write.create, read.get],"
        " type: [nas:Topic, nas:Book]}\n"
        "  - {id: hide_area, action: [read.*], type: ['nas:*']}\n"
    )
    rules = rules_of(compile_graph(source))
    assert rules["deny.hide_alone"] == {
        "id": "deny.hide_alone",
        "effect": "deny",
        "action": ["read.get"],
        "resource": [type_at(EXAMPLE, "nas:Note")],
    }
    assert rules["deny.hide_mixed"]["action"] == ["read.get", "write.create"]
    assert rules["deny.hide_mixed"]["resource"] == [
        type_at(EXAMPLE, "nas:Book"),
        type_at(EXAMPLE, "nas:Topic"),
    ]
    assert rules["deny.hide_mixed"]["principal"] == {"role": ["reporting"]}
    assert rules["deny.hide_area"]["action"] == ["read.*"]
    assert rules["deny.hide_area"]["resource"] == [type_at(EXAMPLE, "nas:*")]


@pytest.mark.parametrize(
    ("rule", "path"),
    [
        ("action: [read.find], type: [nas:Note]", "deny[0].action"),
        ("action: [read.get, read.query], type: [nas:Note]", "deny[0].action"),
        ("action: [read.list], type: [nas:Note]", "deny[0].action"),
        ('action: ["*"], type: [nas:Note]', "deny[0].action"),
        ("action: [ext.*], type: [nas:Note]", "deny[0].action"),
        ("action: [read.get], type: [nas:Note], attribute: [body]", "deny[0].action"),
        ("action: [read.*], type: [nas:Note], relation: [nas:about]", "deny[0].action"),
    ],
)
def test_other_reads_on_types_and_reads_on_terms_are_problems(rule: str, path: str) -> None:
    source = f"schema: graph_cli.policy/1\nid: graph\ndeny:\n  - {{id: bad, {rule}}}\n"
    found = problems_of(source, graph=EXAMPLE)
    assert [p for p, _ in found] == [path], found


def test_a_deny_rule_on_the_whole_graph_may_still_name_any_read() -> None:
    source = (
        "schema: graph_cli.policy/1\nid: graph\ndeny:\n  - {id: no_find, action: [read.find]}\n"
    )
    assert rules_of(compile_graph(source))["deny.no_find"]["resource"] == [f"graph:{EXAMPLE}*"]


# ---- the grammar (V1) --------------------------------------------------------------------


def document(effect: str, action: list[str], resource: JsonValue) -> dict[str, JsonValue]:
    return {
        "schema": "graph_cli.policy/1",
        "id": "installation",
        "layer": "installation",
        "projects": [],
        "rules": [{"id": "r", "effect": effect, "action": action, "resource": resource}],
        "obligations": [],
    }


@pytest.mark.parametrize(
    ("action", "resource", "valid"),
    [
        (["read.get"], type_at(EXAMPLE, "nas:Note"), True),
        (["read.*"], type_at(EXAMPLE, "nas:Note"), True),
        (
            ["read.get", "write.create"],
            [type_at(EXAMPLE, "nas:Note"), type_at(EXAMPLE, "nas:*")],
            True,
        ),
        (["read.*"], f"graph:{EXAMPLE}/type:*", True),
        (["read.find"], type_at(EXAMPLE, "nas:Note"), False),
        (["*"], type_at(EXAMPLE, "nas:Note"), False),
        (["ext.*"], type_at(EXAMPLE, "nas:Note"), False),
        (["read.get"], f"{type_at(EXAMPLE, 'nas:Note')}/attr:title", False),
        (["read.*"], f"{type_at(EXAMPLE, 'nas:Note')}/rel:nas:about", False),
        (
            ["read.get"],
            [type_at(EXAMPLE, "nas:Note"), f"{type_at(EXAMPLE, 'nas:Note')}/attr:*"],
            False,
        ),
        (["write.update"], f"{type_at(EXAMPLE, 'nas:Note')}/attr:title", True),
        (["read.get"], f"graph:{EXAMPLE}*", True),
    ],
)
def test_a_type_resource_may_name_read_get_and_a_term_resource_writes_only(
    action: list[str], resource: JsonValue, valid: bool
) -> None:
    problems = document_problems(document("deny", action, resource))
    assert (problems == []) is valid, problems
    assert all(p.path == "rules[0].action" for p in problems)


# ---- read_limited (V4) -------------------------------------------------------------------


def test_a_policy_set_is_read_limited_when_a_rule_decides_the_visibility_of_a_type() -> None:
    installation = compile_installation().document
    layer = compile_graph().document
    assert read_limited([installation], EXAMPLE)  # grant.1.reads
    assert read_limited([installation], OTHER)  # grant.3 names its types, too
    assert not read_limited([installation], UNNAMED)
    assert read_limited([installation, layer], EXAMPLE)
    assert not read_limited(BUILTIN_POLICY_SET.documents, EXAMPLE)
    plain = compile_installation(one_grant("writes: {nas:Note: {create: true}}")).document
    assert not read_limited([plain], EXAMPLE)  # write rules on types are no read limit
    assert not read_limited([], EXAMPLE)


@pytest.mark.parametrize(
    ("source", "limited"),
    [
        ('types:\n  "nas:Note": read\n', False),  # a write limit, not a read limit
        ('types:\n  "nas:Note": write\n', False),
        ('types:\n  "nas:Note": none\n', True),
        ('types:\n  "nas:*": none\n', True),
        ("deny:\n  - {id: d, action: [write.*], type: [nas:Note]}\n", False),
        ("deny:\n  - {id: d, action: [read.get], type: [nas:Note]}\n", True),
        ("deny:\n  - {id: d, action: [read.*], type: ['nas:*']}\n", True),
        ("deny:\n  - {id: d, action: [read.get, write.*], type: [nas:Note]}\n", True),
        # a graph-wide deny names no type: it hides nothing by itself
        ("deny:\n  - {id: d, action: [read.get]}\n", False),
        ("deny:\n  - {id: d, action: [read.*]}\n", False),
        ("deny:\n  - {id: d, action: [write.*]}\n", False),
        ("types:\n  {}\n", False),
    ],
)
def test_read_limited_of_graph_layers(source: str, limited: bool) -> None:
    layer = compile_graph(f"schema: graph_cli.policy/1\nid: graph\n{source}").document
    assert read_limited([layer], EXAMPLE) is limited
    assert not read_limited([layer], OTHER)


def test_a_graph_level_glob_does_not_make_a_graph_read_limited() -> None:
    for resource in (f"graph:{EXAMPLE}*", "graph:*", "*", f"graph:{EXAMPLE}"):
        assert not read_limited([document("allow", ["read.*"], resource)], EXAMPLE)
        assert not read_limited([document("deny", ["*"], resource)], EXAMPLE)
    for resource in (
        f"graph:{EXAMPLE}/type:*",
        type_at(EXAMPLE, "nas:*"),
        type_at(EXAMPLE, "nas:Note"),
    ):
        assert read_limited([document("allow", ["*"], resource)], EXAMPLE)
        assert not read_limited([document("allow", ["write.*"], resource)], EXAMPLE)
    # terms below a type are no type resource
    assert not read_limited(
        [document("deny", ["write.update"], f"{type_at(EXAMPLE, 'nas:Note')}/attr:body")], EXAMPLE
    )


# ---- authorize on type resources (V1) ----------------------------------------------------

NOTE = type_at(EXAMPLE, "nas:Note")
TOPIC = type_at(EXAMPLE, "nas:Topic")
BOOK = type_at(EXAMPLE, "nas:Book")
SCAN = type_at(EXAMPLE, "nas:Scan")
GRAPH = f"graph:{EXAMPLE}"


@pytest.mark.parametrize(
    ("who", "resource", "expected"),
    [
        # an allow list: the graph is read as ever, only the listed types are visible
        (REPORTING, GRAPH, "allow:grant.1"),
        (REPORTING, NOTE, "allow:grant.1.reads"),
        (REPORTING, TOPIC, "allow:grant.1.reads"),
        (REPORTING, BOOK, "deny:default.deny"),
        (REPORTING, SCAN, "deny:default.deny"),
        (NOBODY, NOTE, "deny:default.deny"),
        # an unlimited reader sees every type through the glob of its grant
        (AUDITOR, BOOK, "allow:grant.2"),
        (AUDITOR, NOTE, "allow:grant.2"),
        # rights of two roles add up
        (BOTH, BOOK, "allow:grant.2"),
        (BOTH, NOTE, "allow:grant.1.reads"),
        # a prefix glob of reads
        (CURATOR, BOOK, "allow:grant.3.reads"),
        (CURATOR, type_at(EXAMPLE, "files:File"), "deny:default.deny"),
    ],
)
def test_an_allow_list_decides_the_visibility_of_types(
    who: ActorContext, resource: str, expected: str
) -> None:
    assert decide(who, "read.get", resource) == expected


def test_the_graph_is_decided_on_the_graph_resource_only() -> None:
    """Every read but the visibility of a type is decided on ``graph:<id>``: an allow list
    grant still covers all of them."""
    for action in ("list", "get", "find", "traverse", "history", "describe", "query", "function"):
        assert decide(REPORTING, f"read.{action}", GRAPH) == "allow:grant.1"
    # and it does not reach another graph
    assert decide(REPORTING, "read.get", f"graph:{OTHER}") == "deny:default.deny"


def test_a_scope_still_narrows_an_allow_list() -> None:
    frozen = token("reporting", project="frozen_project")
    assert decide(frozen, "read.get", NOTE) == "deny:scope.frozen_project.example_graph"
    assert decide(frozen, "read.get", GRAPH) == "deny:scope.frozen_project.example_graph"
    unscoped = token("reporting", project="other_project")
    assert decide(unscoped, "read.get", NOTE) == "allow:grant.1.reads"


def test_a_graph_deny_wins_over_every_allow() -> None:
    layer = "schema: graph_cli.policy/1\nid: graph\ndeny:\n"
    deny = layer + "  - {id: hide_notes, role: [reporting], action: [read.get], type: [nas:Note]}\n"
    # the denied role loses the type, also with another role that allows it
    assert decide(REPORTING, "read.get", NOTE, deny) == "deny:deny.hide_notes"
    assert decide(BOTH, "read.get", NOTE, deny) == "deny:deny.hide_notes"
    assert decide(REPORTING, "read.get", TOPIC, deny) == "allow:grant.1.reads"
    # a reader without that role is not hit
    assert decide(AUDITOR, "read.get", NOTE, deny) == "allow:grant.2"
    # the deny names the type, not the graph
    assert decide(REPORTING, "read.get", GRAPH, deny) == "allow:grant.1"
    # read.* covers read.get; an unrelated read action decides nothing on a type
    area = layer + "  - {id: hide_all, action: [read.*], type: ['nas:*']}\n"
    assert decide(AUDITOR, "read.get", BOOK, area) == "deny:deny.hide_all"
    assert decide(AUDITOR, "read.find", GRAPH, area) == "allow:grant.2"


def test_none_hides_a_type_from_the_app_and_the_server_but_not_from_maintainers() -> None:
    layer = GRAPH_LAYER
    for who in (AUDITOR, BOTH, token("auditor", entrypoint=Entrypoint.APP)):
        assert decide(who, "read.get", SCAN, layer) == "deny:type.nas:Scan"
    assert decide(AUDITOR, "read.get", BOOK, layer) == "allow:grant.2"
    assert decide(AUDITOR, "read.get", GRAPH, layer) == "allow:grant.2"
    # the maintenance entrypoint reads it (the live view's file download, project admins)
    assert decide(maint_actor(), "read.get", SCAN, layer) == "allow:maint.any"
    assert decide(maint_actor(), "read.get", NOTE, layer) == "allow:maint.any"


def test_none_also_stops_writes_of_the_type() -> None:
    source = INSTALLATION.replace(
        "principal: {role: auditor}, graphs: [example_graph], level: read",
        "principal: {role: auditor}, graphs: [example_graph], level: write",
    ).replace('no_project: {"*": read}', 'no_project: {"*": write}')
    layered = policy_set(compile_installation(source).document, compile_graph().document)
    assert authorize(AUDITOR, "write.create", BOOK, CTX, layered).effect == "allow"
    denied = authorize(AUDITOR, "write.create", SCAN, CTX, layered)
    assert (denied.effect, denied.rule_id) == ("deny", "type.nas:Scan")
    assert authorize(maint_actor(), "write.create", SCAN, CTX, layered).effect == "allow"


def test_a_project_admin_reads_a_hidden_type_through_its_maintain_scope() -> None:
    source = INSTALLATION.replace(
        "frozen_project: {example_graph: none}", "ops: {example_graph: maintain}"
    )
    installation = compile_installation(source).document
    admin = dataclasses.replace(
        maint_actor(), project=Facet(Assurance.VERIFIED, id="ops", source="token")
    )
    layer = compile_graph().document
    decision = authorize(admin, "read.get", SCAN, CTX, policy_set(installation, layer))
    assert (decision.effect, decision.rule_id) == ("allow", "scope.ops:maintain")


# ---- visibility and read_allowance (V4, V8) ----------------------------------------------


def test_concrete_types_are_the_types_an_entity_can_have() -> None:
    assert {"nas:Note", "nas:Topic", "nas:Book", "nas:Scan", "files:File"} <= CONCRETE
    assert "nas:Source" not in CONCRETE  # abstract: no entity has it


def test_visibility_of_an_allow_list() -> None:
    limited = policy_set(compile_installation().document)
    assert visibility(REPORTING, EXAMPLE, limited, CONCRETE) == {"nas:Note", "nas:Topic"}
    # two roles add up; an unlimited role leaves nothing to filter
    assert visibility(BOTH, EXAMPLE, limited, CONCRETE) is None
    assert visibility(AUDITOR, EXAMPLE, limited, CONCRETE) is None
    assert visibility(CURATOR, EXAMPLE, limited, CONCRETE) == {
        t for t in CONCRETE if t.startswith("nas:")
    }
    assert visibility(NOBODY, EXAMPLE, limited, CONCRETE) == frozenset()


def test_visibility_is_none_for_a_graph_that_is_not_read_limited() -> None:
    limited = policy_set(compile_installation().document)
    assert visibility(REPORTING, UNNAMED, limited, CONCRETE) is None
    assert visibility(NOBODY, EXAMPLE, BUILTIN_POLICY_SET, CONCRETE) is None
    plain = policy_set(compile_installation(one_grant("entrypoint: server")).document)
    assert visibility(NOBODY, EXAMPLE, plain, CONCRETE) is None


def test_a_type_outside_the_given_types_is_not_visible() -> None:
    """The caller passes the types of the pinned model: a type it lacks is left out of the set,
    so a caller that gets a set (it is limited) does not see entities of such a type."""
    limited = policy_set(compile_installation().document)
    assert visibility(REPORTING, EXAMPLE, limited, ["nas:Note", "nas:Gone"]) == {"nas:Note"}
    # every type it is given is visible: nothing to filter
    assert visibility(REPORTING, EXAMPLE, limited, ["nas:Note", "nas:Topic"]) is None
    assert visibility(REPORTING, EXAMPLE, limited, []) is None


def test_visibility_follows_denies_of_both_layers() -> None:
    combined = policy(GRAPH_LAYER)
    # hide_notes denies Note to the role reporting; none hides Scan from the server callers
    assert visibility(REPORTING, EXAMPLE, combined, CONCRETE) == {"nas:Topic"}
    assert visibility(AUDITOR, EXAMPLE, combined, CONCRETE) == CONCRETE - {"nas:Scan"}
    assert visibility(BOTH, EXAMPLE, combined, CONCRETE) == CONCRETE - {"nas:Scan", "nas:Note"}
    assert visibility(maint_actor(), EXAMPLE, combined, CONCRETE) is None


def test_read_allowance_lists_the_visible_types_of_a_limited_caller() -> None:
    limited = policy_set(compile_installation().document)
    assert read_allowance(REPORTING, EXAMPLE, limited, CONCRETE) == ["nas:Note", "nas:Topic"]
    assert read_allowance(NOBODY, EXAMPLE, limited, CONCRETE) == []
    assert read_allowance(AUDITOR, EXAMPLE, limited, CONCRETE) is None
    assert read_allowance(BOTH, EXAMPLE, limited, CONCRETE) is None
    assert read_allowance(REPORTING, UNNAMED, limited, CONCRETE) is None
    combined = policy(GRAPH_LAYER)
    assert read_allowance(AUDITOR, EXAMPLE, combined, CONCRETE) == sorted(CONCRETE - {"nas:Scan"})


def test_visibility_records_no_decision_and_changes_nothing() -> None:
    """Pure: the same answer twice, whatever order the types come in."""
    limited = policy_set(compile_installation().document)
    first = visibility(REPORTING, EXAMPLE, limited, sorted(CONCRETE))
    second = visibility(REPORTING, EXAMPLE, limited, sorted(CONCRETE, reverse=True))
    assert first == second


# ---- write_grant_types, named_read_types -------------------------------------------------


def test_reads_rules_are_no_write_grants() -> None:
    limited = policy_set(compile_installation().document)
    assert write_grant_types(REPORTING, EXAMPLE, limited) == ()  # grant.1.reads names types
    assert write_grant_types(AUDITOR, EXAMPLE, limited) == ()
    # a grant with both: only the write types count
    assert write_grant_types(CURATOR, EXAMPLE, limited) == ("nas:Book",)


def test_named_read_types_are_the_exact_types_of_reads_grants_and_read_denies() -> None:
    source = one_grant("reads: [nas:Note, 'nas:*'], writes: {nas:Note: {create: true}}")
    installation = compile_installation(source).document
    layer = compile_graph(
        'schema: graph_cli.policy/1\nid: graph\ntypes:\n  "nas:Scan": none\n  "nas:Book": read\n'
        "deny:\n"
        "  - {id: a, action: [read.get], type: [nas:Topic]}\n"
        "  - {id: b, action: [write.*], type: [nas:Note]}\n"
        "  - {id: c, action: [read.*], type: ['nas:*']}\n"
    ).document
    assert named_read_types([installation, layer], EXAMPLE) == (
        NamedReadType("nas:Note", True),
        NamedReadType("nas:Scan", False),
        NamedReadType("nas:Topic", False),
    )
    assert named_read_types([installation, layer], OTHER) == ()
    assert named_read_types(BUILTIN_POLICY_SET.documents, EXAMPLE) == ()


# ---- visible_limits (V8) -------------------------------------------------------------------


def test_visible_limits_keep_only_the_limits_that_name_a_visible_class() -> None:
    limits: dict[str, JsonValue] = {
        "nas:Note": "read",
        "nas:Topic": "none",
        "files:*": "write",
        "provenance:*": "read",
        "nas:Gone": "read",
    }
    seen = ["nas:Note", "nas:Source", "provenance:Claim"]
    assert visible_limits(limits, seen) == {"nas:Note": "read", "provenance:*": "read"}
    assert visible_limits(limits, []) == {}
    assert visible_limits(limits, ["files:File", "nas:Topic"]) == {
        "nas:Topic": "none",
        "files:*": "write",
    }
    assert visible_limits({}, seen) == {}
