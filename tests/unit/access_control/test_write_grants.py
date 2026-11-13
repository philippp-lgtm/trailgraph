"""Write allow lists: ``writes`` on grants and term deny rules (contracts B6.9, B6.13).

The compiled documents are shown as they are (rules and resources) and then evaluated with
``authorize()`` in ``test_fine_resources.py``. Types, attributes and relations are those of
the toy ontology ``notes_and_sources`` 1.1.0, whose model the warning tests use.
"""

from collections.abc import Callable

import pytest
from support.fixtures import compiled_model

from graph_cli.access_control.compiler import Compiled, compile_policy
from graph_cli.access_control.documents import document_problems
from graph_cli.access_control.terms import (
    TermModel,
    UnknownTerm,
    UnknownWriteType,
    UnusableWrite,
)
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GraphId, parse_id
from graph_cli.services.term_model import term_model

EXAMPLE = GraphId(parse_id("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a"))
OTHER = GraphId(parse_id("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8b"))
GRAPHS = {"example_graph": EXAMPLE, "other_graph": OTHER}
NOTE = "nas:Note"
TOPIC = "nas:Topic"

INSTALLATION = """\
schema: graph_cli.policy/1
id: installation
roles:
  importer: {description: "Importers: notes and topics"}
  tagger: {description: "Taggers: update notes"}
grants:
  - entrypoint: server
    principal: {role: importer}
    graphs: [example_graph]
    level: read
    writes:
      nas:Note:
        create: [title, body, kind]
        update: [body, kind]
        link: [nas:about]
        unlink: false
        transition: [status]
        retract: true
      nas:Topic:
        create: true
        link: true
        supersede: true
        alias: true
  - entrypoint: server
    principal: {role: tagger}
    graphs: [example_graph, other_graph]
    level: read
    writes:
      nas:Note: {update: [kind]}
no_project: {"*": read}
"""


def compile_installation(
    source: str = INSTALLATION,
    models: Callable[[GraphId], TermModel | None] | None = None,
) -> Compiled:
    return compile_policy(source.encode(), file="installation.yaml", graphs=GRAPHS, models=models)


def rules_of(compiled: Compiled) -> dict[str, dict[str, JsonValue]]:
    found = compiled.document["rules"]
    assert isinstance(found, list)
    return {str(r["id"]): r for r in found if isinstance(r, dict)}


def at(graph: GraphId, *tail: str) -> list[str]:
    return [f"graph:{graph}{t}" for t in tail]


def problems_of(source: str, **options: object) -> list[tuple[str, str]]:
    with pytest.raises(GraphCliError) as info:
        compile_policy(source.encode(), file="p.yaml", graphs=GRAPHS, **options)  # type: ignore[arg-type]
    assert info.value.code == "policy.invalid"
    found = info.value.details["problems"]
    assert isinstance(found, list)
    return [(str(p["path"]), str(p["message"])) for p in found if isinstance(p, dict)]


# ---- the compiled form (W5) --------------------------------------------------------------


def test_a_writes_grant_compiles_to_the_read_rule_and_one_rule_per_verb() -> None:
    compiled = compile_installation()
    assert document_problems(compiled.document) == []
    ids = list(rules_of(compiled))
    # the read rule first, then the verbs in a fixed order; ``edge`` right after ``link``
    assert ids[:11] == [
        "grant.1",
        "grant.1.create",
        "grant.1.update",
        "grant.1.link",
        "grant.1.edge",
        "grant.1.transition",
        "grant.1.supersede",
        "grant.1.retract",
        "grant.1.alias",
        "grant.2",
        "grant.2.update",
    ]
    rules = rules_of(compiled)
    principal = {"entrypoint": "server", "role": "importer"}
    assert rules["grant.1"]["action"] == [
        f"read.{v}"
        for v in ("list", "get", "find", "traverse", "history", "describe", "query", "function")
    ]
    assert rules["grant.1"]["resource"] == [f"graph:{EXAMPLE}*"]
    expected: dict[str, tuple[str, list[str]]] = {
        "grant.1.create": (
            "write.create",
            at(
                EXAMPLE,
                f"/type:{NOTE}",
                f"/type:{NOTE}/attr:body",
                f"/type:{NOTE}/attr:kind",
                f"/type:{NOTE}/attr:title",
                f"/type:{TOPIC}",
                f"/type:{TOPIC}/attr:*",
            ),
        ),
        "grant.1.update": (
            "write.update",
            at(EXAMPLE, f"/type:{NOTE}", f"/type:{NOTE}/attr:body", f"/type:{NOTE}/attr:kind"),
        ),
        "grant.1.link": (
            "write.link",
            at(
                EXAMPLE,
                f"/type:{NOTE}",
                f"/type:{NOTE}/rel:nas:about",
                f"/type:{TOPIC}",
                f"/type:{TOPIC}/rel:*",
            ),
        ),
        "grant.1.edge": (
            "write.create",
            at(EXAMPLE, f"/type:{NOTE}/rel:nas:about", f"/type:{TOPIC}/rel:*"),
        ),
        "grant.1.transition": (
            "write.transition",
            at(EXAMPLE, f"/type:{NOTE}", f"/type:{NOTE}/attr:status"),
        ),
        "grant.1.supersede": ("write.supersede", at(EXAMPLE, f"/type:{TOPIC}")),
        "grant.1.retract": ("write.retract", at(EXAMPLE, f"/type:{NOTE}")),
        "grant.1.alias": ("write.alias", at(EXAMPLE, f"/type:{TOPIC}")),
    }
    for rule_id, (action, resources) in expected.items():
        rule = rules[rule_id]
        assert rule["effect"] == "allow", rule_id
        assert rule["principal"] == principal, rule_id
        assert (rule["action"], rule["resource"]) == ([action], resources), rule_id
    # no rule for a verb that is not granted
    assert "grant.1.unlink" not in rules
    assert "grant.2.create" not in rules


def test_a_link_grant_adds_an_edge_rule_on_the_relations_only() -> None:
    """An inline edge of a create is decided as ``write.create`` on its relation, so the
    link right becomes a create right on the relation resources and never on the type."""
    rules = rules_of(compile_installation())
    edge = rules["grant.1.edge"]
    assert edge["action"] == ["write.create"]
    resources = edge["resource"]
    assert isinstance(resources, list)
    assert all("/rel:" in str(r) for r in resources)
    assert f"graph:{EXAMPLE}/type:{NOTE}" not in resources
    # no link verb, no edge rule
    assert "grant.2.edge" not in rules


def test_writes_of_several_graphs_and_grants_number_their_rules_by_grant() -> None:
    rules = rules_of(compile_installation())
    assert rules["grant.2"]["resource"] == [f"graph:{EXAMPLE}*", f"graph:{OTHER}*"]
    assert rules["grant.2.update"]["principal"] == {"entrypoint": "server", "role": "tagger"}
    assert rules["grant.2.update"]["resource"] == at(
        EXAMPLE, f"/type:{NOTE}", f"/type:{NOTE}/attr:kind"
    ) + at(OTHER, f"/type:{NOTE}", f"/type:{NOTE}/attr:kind")


def test_lists_are_sorted_and_deduplicated_and_false_verbs_are_dropped() -> None:
    source = """\
schema: graph_cli.policy/1
id: installation
grants:
  - graphs: [example_graph]
    level: read
    writes:
      nas:Note:
        update: [kind, body, kind]
        link: [nas:cites, nas:about, nas:about]
        retract: false
        create: false
no_project: {"*": read}
"""
    rules = rules_of(compile_installation(source))
    assert list(rules)[:4] == ["grant.1", "grant.1.update", "grant.1.link", "grant.1.edge"]
    assert rules["grant.1.update"]["resource"] == at(
        EXAMPLE, f"/type:{NOTE}", f"/type:{NOTE}/attr:body", f"/type:{NOTE}/attr:kind"
    )
    assert rules["grant.1.edge"]["resource"] == at(
        EXAMPLE, f"/type:{NOTE}/rel:nas:about", f"/type:{NOTE}/rel:nas:cites"
    )
    assert rules["grant.1"]["principal"] == {"entrypoint": "app"}


def test_the_compiled_document_does_not_depend_on_the_model() -> None:
    """An ontology release never changes a policy's hash: only validation uses a model."""
    model = term_model(compiled_model("notes_and_sources", "1.1.0"))
    bare = compile_installation()
    checked = compile_installation(models=lambda graph: model)
    assert checked.hash == bare.hash
    assert checked.document == bare.document
    empty = compile_installation(models=lambda graph: TermModel({}))
    assert empty.hash == bare.hash
    assert [(w.graph, w.type) for w in empty.unknown_write_types] == [
        (EXAMPLE, NOTE),
        (EXAMPLE, TOPIC),
        (OTHER, NOTE),
    ]


# ---- problems (W2) -----------------------------------------------------------------------


def one_grant(writes: str, *, level: str = "read", graphs: str = "[example_graph]") -> str:
    return f"""\
schema: graph_cli.policy/1
id: installation
grants:
  - {{graphs: {graphs}, level: {level}}}
no_project: {{"*": read}}
""".replace("level: " + level + "}", "level: " + level + ", writes: " + writes + "}")


@pytest.mark.parametrize(
    ("writes", "options", "path", "fragment"),
    [
        ("{nas:Note: {create: true}}", {"level": "write"}, "grants[0].writes", "needs level: read"),
        (
            "{nas:Note: {create: true}}",
            {"graphs": '"*"'},
            "grants[0].graphs",
            "must name graphs",
        ),
        ("{}", {}, "grants[0].writes", "at least one type"),
        ("[nas:Note]", {}, "grants[0].writes", "must be a mapping"),
        ("{nas:*: {create: true}}", {}, "grants[0].writes.nas:*", "prefix"),
        ("{Note: {create: true}}", {}, "grants[0].writes.Note", "not a type CURIE"),
        ("{nas:Note: {}}", {}, "grants[0].writes.nas:Note", "grants no verb"),
        (
            "{nas:Note: {create: false, retract: false}}",
            {},
            "grants[0].writes.nas:Note",
            "grants no verb",
        ),
        ("{nas:Note: true}", {}, "grants[0].writes.nas:Note", "must be a mapping"),
        (
            "{nas:Note: {delete: true, create: true}}",
            {},
            "grants[0].writes.nas:Note.delete",
            "unknown key",
        ),
        ("{nas:Note: {create: []}}", {}, "grants[0].writes.nas:Note.create", "non-empty list"),
        ("{nas:Note: {create: yes_please}}", {}, "grants[0].writes.nas:Note.create", "list of"),
        (
            "{nas:Note: {create: [Bad-Name]}}",
            {},
            "grants[0].writes.nas:Note.create[0]",
            "attribute names",
        ),
        (
            "{nas:Note: {update: [title, nas:about]}}",
            {},
            "grants[0].writes.nas:Note.update[1]",
            "attribute names",
        ),
        (
            "{nas:Note: {link: [about]}}",
            {},
            "grants[0].writes.nas:Note.link[0]",
            "relation CURIEs",
        ),
        (
            "{nas:Note: {unlink: [nas:about, title]}}",
            {},
            "grants[0].writes.nas:Note.unlink[1]",
            "relation CURIEs",
        ),
        (
            "{nas:Note: {transition: [Status]}}",
            {},
            "grants[0].writes.nas:Note.transition[0]",
            "lifecycle attribute names",
        ),
        (
            "{nas:Note: {retract: [x]}}",
            {},
            "grants[0].writes.nas:Note.retract",
            "must be true or false",
        ),
        (
            "{nas:Note: {alias: maybe}}",
            {},
            "grants[0].writes.nas:Note.alias",
            "must be true or false",
        ),
    ],
)
def test_invalid_writes_list_their_problem(
    writes: str, options: dict[str, str], path: str, fragment: str
) -> None:
    found = problems_of(one_grant(writes, **options))
    assert any(p == path and fragment in m for p, m in found), found


def test_every_writes_problem_is_reported_at_once() -> None:
    source = one_grant(
        "{nas:*: {create: true}, nas:Note: {create: [], bogus: true, retract: 1}, nas:Topic: {}}",
        level="write",
        graphs='"*"',
    )
    assert [p for p, _ in problems_of(source)] == [
        "grants[0].writes.nas:*",
        "grants[0].writes.nas:Note.bogus",
        "grants[0].writes.nas:Note.create",
        "grants[0].writes.nas:Note.retract",
        "grants[0].writes.nas:Topic",
        "grants[0].writes",  # the level
        "grants[0].graphs",
    ]


# ---- graph deny rules on terms (W6) ------------------------------------------------------

GRAPH_POLICY = """\
schema: graph_cli.policy/1
id: graph
deny:
  - {id: no_body, role: [importer], action: [write.update, write.create], type: [nas:Note],
     attribute: [kind, body]}
  - {id: no_cites, action: [write.link], type: [nas:Topic, nas:Note], relation: [nas:cites]}
  - {id: no_unlink, action: [write.unlink], type: [nas:Note], relation: [nas:about]}
  - {id: no_any_write, action: ["write.*"], type: [nas:Note], relation: [nas:about]}
"""


def compile_graph(source: str = GRAPH_POLICY, **options: object) -> Compiled:
    return compile_policy(
        source.encode(),
        file="graph.yaml",
        graphs=GRAPHS,
        graph=EXAMPLE,
        roles=frozenset({"importer"}),
        **options,  # type: ignore[arg-type]
    )


def test_a_deny_rule_on_terms_denies_the_terms_instead_of_the_type() -> None:
    compiled = compile_graph()
    assert document_problems(compiled.document) == []
    rules = rules_of(compiled)
    assert rules["deny.no_body"] == {
        "id": "deny.no_body",
        "effect": "deny",
        "principal": {"role": ["importer"]},
        "action": ["write.create", "write.update"],
        "resource": at(EXAMPLE, f"/type:{NOTE}/attr:body", f"/type:{NOTE}/attr:kind"),
    }
    # a relation deny on write.link also denies write.create: it stops inline edges, too
    assert rules["deny.no_cites"]["action"] == ["write.create", "write.link"]
    assert rules["deny.no_cites"]["resource"] == at(
        EXAMPLE, f"/type:{NOTE}/rel:nas:cites", f"/type:{TOPIC}/rel:nas:cites"
    )
    assert "principal" not in rules["deny.no_cites"]
    assert rules["deny.no_unlink"]["action"] == ["write.unlink"]
    assert rules["deny.no_any_write"]["action"] == ["write.*"]


@pytest.mark.parametrize(
    ("rule", "path", "fragment"),
    [
        (
            "type: [nas:Note], attribute: [body], relation: [nas:about], action: [write.update]",
            "deny[0]",
            "not both",
        ),
        ("attribute: [body], action: [write.update]", "deny[0].attribute", "needs type"),
        ("relation: [nas:about], action: [write.link]", "deny[0].relation", "needs type"),
        (
            "type: [nas:*], attribute: [body], action: [write.update]",
            "deny[0].type",
            "name concrete types",
        ),
        (
            "type: [nas:Note], attribute: [Body], action: [write.update]",
            "deny[0].attribute",
            "not an attribute name",
        ),
        (
            "type: [nas:Note], relation: [about], action: [write.link]",
            "deny[0].relation",
            "not a relation CURIE",
        ),
        (
            "type: [nas:Note], attribute: [body], action: [write.link]",
            "deny[0].action",
            "never decides an attribute",
        ),
        (
            "type: [nas:Note], attribute: [body], action: [write.update, write.retract]",
            "deny[0].action",
            "never decides an attribute",
        ),
        (
            "type: [nas:Note], relation: [nas:about], action: [write.update]",
            "deny[0].action",
            "never decides a relation",
        ),
        (
            "type: [nas:Note], relation: [nas:about], action: [read.get]",
            "deny[0].action",
            "never decides a relation",
        ),
        ("type: [nas:Note], attribute: [], action: [write.update]", "deny[0].attribute", "list"),
    ],
)
def test_invalid_term_deny_rules_list_their_problem(rule: str, path: str, fragment: str) -> None:
    source = f"schema: graph_cli.policy/1\nid: graph\ndeny:\n  - {{id: bad, {rule}}}\n"
    found = problems_of(source, graph=EXAMPLE)
    assert any(p == path and fragment in m for p, m in found), found


def test_a_term_deny_rule_refuses_an_extended_type() -> None:
    source = (
        "schema: graph_cli.policy/1\nid: graph\ndeny:\n"
        "  - {id: bad, type: [nas:Source], attribute: [title], action: [write.update]}\n"
    )
    found = problems_of(source, graph=EXAMPLE, extended_types=frozenset({"nas:Source"}))
    assert found == [
        ("deny[0].type", "nas:Source is abstract or has subclasses; name the concrete types")
    ]


def test_the_installation_layer_has_no_term_deny_keys() -> None:
    source = (
        INSTALLATION
        + "deny:\n  - {id: bad, graph: [example_graph], action: [write.update], attribute: [x]}\n"
    )
    assert ("deny[0].attribute", "unknown key") in problems_of(source, roles=None)


# ---- checks against the model (W8) -------------------------------------------------------


@pytest.fixture(scope="module")
def model() -> TermModel:
    return term_model(compiled_model("notes_and_sources", "1.1.0"))


def writes_source(writes: str, graphs: str = "[example_graph]") -> str:
    return one_grant(writes, graphs=graphs)


def test_term_model_describes_what_the_compiler_needs(model: TermModel) -> None:
    note = model.types[NOTE]
    assert (note.extended, note.file) == (False, False)
    assert {n: (a.lifecycle, a.must_give) for n, a in note.attributes.items()} == {
        "title": (False, True),
        "body": (False, True),
        "kind": (False, True),
        "status": (True, False),  # a single initial state is filled in
    }
    assert {"nas:about", "nas:asserts", "nas:cites"} <= note.relations
    assert "nas:broader" not in note.relations  # the domain is another class
    assert "core:supersedes" in note.relations  # no domain: any class
    assert "nas:broader" in model.types[TOPIC].relations
    assert not {"nas:about", "nas:cites"} & model.types[TOPIC].relations
    assert model.types["nas:Source"].extended  # abstract, with subclasses
    assert model.types["nas:WebPage"].attributes.keys() >= {"title", "url"}
    scan = model.types["nas:Scan"]
    assert scan.file and not scan.extended
    # a stored file's create gives what ``graph file add`` sets, required or not
    assert {n for n, a in scan.attributes.items() if a.must_give} == {
        "title",
        "content",
        "media_type",
        "file_name",
    }
    assert not model.types["nas:Note"].file


def test_unknown_types_and_terms_are_collected_not_refused(model: TermModel) -> None:
    source = writes_source(
        "{nas:Gone: {create: true},"
        " nas:Note: {create: [title, body, kind, nope], update: [also_nope],"
        " link: [nas:broader], unlink: [nas:gone], transition: [title, missing]},"
        " nas:Topic: {link: [nas:broader]}}"
    )
    compiled = compile_policy(source.encode(), file="p.yaml", graphs=GRAPHS, models=lambda g: model)
    assert compiled.unknown_write_types == (UnknownWriteType(EXAMPLE, "nas:Gone"),)
    assert compiled.unknown_terms == (
        UnknownTerm(EXAMPLE, NOTE, "also_nope", "attribute"),
        UnknownTerm(EXAMPLE, NOTE, "missing", "lifecycle"),
        UnknownTerm(EXAMPLE, NOTE, "nas:broader", "relation"),
        UnknownTerm(EXAMPLE, NOTE, "nas:gone", "relation"),
        UnknownTerm(EXAMPLE, NOTE, "nope", "attribute"),
        UnknownTerm(EXAMPLE, NOTE, "title", "lifecycle"),  # no lifecycle attribute
    )
    assert compiled.unusable_writes == ()
    # the document is the same: a warning never changes what a policy means
    assert compiled.hash == compile_policy(source.encode(), file="p.yaml", graphs=GRAPHS).hash


def test_writes_on_an_abstract_or_extended_type_are_refused(model: TermModel) -> None:
    found = problems_of(writes_source("{nas:Source: {create: true}}"), models=lambda g: model)
    assert found == [
        (
            "grants[0].writes.nas:Source",
            "nas:Source is abstract or has subclasses in graph example_graph; name the"
            " concrete types",
        )
    ]


def test_unusable_writes_are_collected(model: TermModel) -> None:
    source = writes_source(
        "{nas:Note: {update: [body, status], create: [title]},"
        " nas:Topic: {create: [label, description], update: true},"
        " nas:Scan: {create: [scanned_on, title]}}"
    )
    compiled = compile_policy(source.encode(), file="p.yaml", graphs=GRAPHS, models=lambda g: model)
    assert compiled.unknown_terms == ()
    scan_missing = ("content", "file_name", "media_type")
    assert compiled.unusable_writes == (
        UnusableWrite(EXAMPLE, "nas:Note", "create", ("body", "kind"), False),
        UnusableWrite(EXAMPLE, "nas:Note", "update", ("status",), False),
        UnusableWrite(EXAMPLE, "nas:Scan", "create", scan_missing, True),
    )


def test_create_true_and_a_complete_list_are_never_unusable(model: TermModel) -> None:
    source = writes_source(
        "{nas:Note: {create: [title, body, kind, status], update: [body]},"
        " nas:Topic: {create: true}, nas:Scan: {create: true}}"
    )
    compiled = compile_policy(source.encode(), file="p.yaml", graphs=GRAPHS, models=lambda g: model)
    assert (compiled.unusable_writes, compiled.unknown_terms) == ((), ())


def test_a_lifecycle_with_several_initial_states_must_be_given() -> None:
    plain = term_model(compiled_model("notes_and_sources", "1.1.0"))
    note = plain.types[NOTE]
    attributes = {**note.attributes, "status": type(note.attributes["status"])(True, True)}
    several = TermModel({**plain.types, NOTE: type(note)(False, False, attributes, note.relations)})
    source = writes_source("{nas:Note: {create: [title, body, kind]}}")
    compiled = compile_policy(
        source.encode(), file="p.yaml", graphs=GRAPHS, models=lambda g: several
    )
    assert compiled.unusable_writes == (UnusableWrite(EXAMPLE, NOTE, "create", ("status",), False),)


def test_each_graph_is_checked_with_its_own_model_and_asked_once(model: TermModel) -> None:
    asked: list[GraphId] = []

    def models(graph: GraphId) -> TermModel | None:
        asked.append(graph)
        return model if graph == EXAMPLE else None  # no model for the other graph: not checked

    source = writes_source(
        "{nas:Note: {update: [nope]}, nas:Topic: {link: [nas:cites]}}",
        graphs="[example_graph, other_graph]",
    )
    compiled = compile_policy(source.encode(), file="p.yaml", graphs=GRAPHS, models=models)
    assert sorted(asked) == [EXAMPLE, OTHER]
    assert {t.graph for t in compiled.unknown_terms} == {EXAMPLE}
    assert [(t.type, t.term) for t in compiled.unknown_terms] == [
        (NOTE, "nope"),
        (TOPIC, "nas:cites"),
    ]


def test_no_models_means_no_checks_and_no_policy_without_writes_asks_for_one() -> None:
    def fail(graph: GraphId) -> TermModel | None:
        raise AssertionError("asked for a model")

    plain = INSTALLATION.split("grants:")[0] + 'grants:\n  - {graphs: "*", level: write}\n'
    plain += 'no_project: {"*": read}\n'
    compile_policy(plain.encode(), file="p.yaml", graphs=GRAPHS, models=fail)
    compiled = compile_installation(writes_source("{nas:Gone: {create: [x]}}"))
    assert (compiled.unknown_write_types, compiled.unknown_terms) == ((), ())


def test_term_deny_rules_check_their_terms_against_the_graph_model(model: TermModel) -> None:
    source = """\
schema: graph_cli.policy/1
id: graph
deny:
  - {id: a, type: [nas:Note, nas:Topic], attribute: [body], action: [write.update]}
  - {id: b, type: [nas:Topic, nas:Gone], relation: [nas:about], action: [write.link]}
"""
    compiled = compile_policy(
        source.encode(), file="g.yaml", graphs=GRAPHS, graph=EXAMPLE, models=lambda g: model
    )
    assert compiled.unknown_terms == (
        UnknownTerm(EXAMPLE, TOPIC, "body", "attribute"),
        UnknownTerm(EXAMPLE, TOPIC, "nas:about", "relation"),
    )
    # an unknown type is the business of ``known_types`` (the existing warning)
    assert compiled.unknown_write_types == ()
    bare = compile_policy(source.encode(), file="g.yaml", graphs=GRAPHS, graph=EXAMPLE)
    assert bare.hash == compiled.hash and bare.unknown_terms == ()
