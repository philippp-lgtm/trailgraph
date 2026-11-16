"""Markings in the policy: the ``markings`` grant key, the resource ``marking:<graph>/<curie>``,
held markings and the decision on an unlisted attribute (field visibility plan F3, step 2).

A marking is granted on a resource outside ``graph:``, so no grant, scope, ``no_project`` or deny
rule that names a graph covers it: a marked attribute stays hidden until a grant names its
marking. The documents are compiled from their authored form and evaluated with ``authorize()``.
Markings and types are those of the ``personnel`` series (1.1.0).
"""

import dataclasses
from collections.abc import Callable

import pytest
from support.audit_events import maint_actor
from support.fixtures import compiled_model
from support.txbuilder import actor

from graph_cli.access_control.authorize import authorize, authorize_unlisted_attribute
from graph_cli.access_control.compiler import Compiled, compile_policy
from graph_cli.access_control.documents import document_problems, read_limited
from graph_cli.access_control.effective import held_markings, marking_allowance
from graph_cli.access_control.policy import BUILTIN_POLICY_SET, policy_set
from graph_cli.access_control.terms import (
    TermModel,
    UnknownMarking,
    UnusableMarkedWrite,
    marking_resource,
    named_markings,
)
from graph_cli.kernel.actor import ActorContext, Assurance, ClientInfo, Entrypoint, Facet
from graph_cli.kernel.authz import AuthzContext
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GraphId, parse_id
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.services.term_model import term_model

EXAMPLE = GraphId(parse_id("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a"))
OTHER = GraphId(parse_id("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8b"))
GRAPHS = {"example_graph": EXAMPLE, "other_graph": OTHER}
CTX = AuthzContext(op_count=1, has_reason=True, dry_run=False)
TERMS: TermModel = term_model(compiled_model("personnel", "1.1.0"))
PERSONAL = f"marking:{EXAMPLE}/ex:personal"
INTERNAL = f"marking:{EXAMPLE}/ex:team_internal"
PERSON = "ex:Person"
GRAPH = f"graph:{EXAMPLE}"

INSTALLATION = """\
schema: graph_cli.policy/1
id: installation
roles:
  hr: {description: "HR: personal data of people"}
  leads: {description: "Team leads: internal notes"}
  reporting: {description: "Reporting: people without personal data"}
  editor: {description: "Editors: update some people attributes"}
  wide: {description: "Wide editors: update every people attribute"}
grants:
  - {entrypoint: server, principal: {role: hr}, graphs: [example_graph], level: write,
     markings: [ex:personal]}
  - {entrypoint: server, principal: {role: leads}, graphs: [example_graph], level: read,
     markings: [ex:team_internal]}
  - {entrypoint: server, principal: {role: reporting}, graphs: "*", level: read}
  - entrypoint: server
    principal: {role: editor}
    graphs: [example_graph]
    level: read
    writes:
      ex:Person: {update: [name, team]}
  - entrypoint: server
    principal: {role: wide}
    graphs: [example_graph]
    level: read
    writes:
      ex:Person: {update: true}
scopes:
  frozen_project: {example_graph: none}
  ops: {example_graph: maintain}
no_project: {"*": write}
deny:
  - {id: no_explorers, agent_type: [explore], action: ["read.*", "write.*"], graph: "*"}
"""
GRAPH_LAYER = """\
schema: graph_cli.policy/1
id: graph
types:
  "ex:Team": none
deny:
  - {id: no_team_edit, role: [editor, wide], action: [write.update], type: [ex:Person],
     attribute: [team]}
  - {id: no_person_reads, role: [leads], action: [read.*], type: [ex:Person]}
"""
ROLES = frozenset({"hr", "leads", "reporting", "editor", "wide"})


def compile_installation(
    source: str = INSTALLATION,
    models: Callable[[GraphId], TermModel | None] | None = None,
) -> Compiled:
    return compile_policy(source.encode(), file="installation.yaml", graphs=GRAPHS, models=models)


def compile_graph(source: str = GRAPH_LAYER) -> Compiled:
    return compile_policy(
        source.encode(), file="graph.yaml", graphs=GRAPHS, graph=EXAMPLE, roles=ROLES
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


def token(*roles: str, project: str | None = None) -> ActorContext:
    asserted = (
        Facet(Assurance.ASSERTED, id=project, source="pointer") if project else actor().project
    )
    return dataclasses.replace(
        actor(),
        principal=Facet(Assurance.VERIFIED, id="token:t", name="t", source="token"),
        project=asserted,
        client=ClientInfo("graph-server", "0.1.0", Entrypoint.SERVER),
        roles=roles,
    )


HR = token("hr")
LEADS = token("leads")
REPORTING = token("reporting")
BOTH = token("hr", "leads")
NOBODY = token()


def installed(*layers: str) -> PolicySet:
    return policy_set(compile_installation().document, *(compile_graph(s).document for s in layers))


def decide(who: ActorContext, action: str, resource: str, *layers: str) -> str:
    decision = authorize(who, action, resource, CTX, installed(*layers))
    return f"{decision.effect}:{decision.rule_id}"


# ---- the compiled form ---------------------------------------------------------------------


def test_a_markings_grant_compiles_to_one_more_allow_rule() -> None:
    compiled = compile_installation()
    assert document_problems(compiled.document) == []
    rules = rules_of(compiled)
    assert rules["grant.1.markings"] == {
        "id": "grant.1.markings",
        "effect": "allow",
        "principal": {"entrypoint": "server", "role": "hr"},
        "action": ["read.get"],
        "resource": [PERSONAL],
    }
    assert rules["grant.2.markings"]["resource"] == [INTERNAL]
    # the grant keeps its read rule on the graph glob, which no marking resource matches
    assert rules["grant.1"]["resource"] == [f"graph:{EXAMPLE}*"]
    assert "grant.3.markings" not in rules


def test_markings_compile_after_the_read_rules_and_before_the_write_rules() -> None:
    source = """\
schema: graph_cli.policy/1
id: installation
roles:
  curator: {description: "Curators"}
grants:
  - entrypoint: server
    principal: {role: curator}
    graphs: [other_graph, example_graph]
    level: read
    reads: ["ex:*"]
    writes:
      ex:Person: {update: [name]}
    markings: [ex:team_internal, ex:personal, ex:personal]
no_project: {"*": read}
"""
    rules = rules_of(compile_installation(source))
    assert [r for r in rules if r.startswith("grant.")] == [
        "grant.1",
        "grant.1.reads",
        "grant.1.markings",
        "grant.1.update",
    ]
    # every named graph and marking, sorted, duplicates dropped
    assert rules["grant.1.markings"]["resource"] == sorted(
        f"marking:{g}/{m}" for g in (EXAMPLE, OTHER) for m in ("ex:personal", "ex:team_internal")
    )
    assert rules["grant.1.markings"]["action"] == ["read.get"]


def test_a_marking_resource_makes_no_graph_read_limited() -> None:
    """``read_limited`` and the fine types look at ``graph:<id>/type:`` resources only: a policy
    that grants markings changes nothing for the type machinery."""
    assert not read_limited([compile_installation().document], EXAMPLE)


def test_a_grant_may_name_markings_at_any_level() -> None:
    source = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {graphs: [example_graph], level: read, markings: [ex:personal]}
  - {graphs: [example_graph], level: write, markings: [ex:personal]}
no_project: {"*": read}
"""
    rules = rules_of(compile_installation(source))
    assert rules["grant.1.markings"]["resource"] == rules["grant.2.markings"]["resource"]


@pytest.mark.parametrize(
    ("fragment", "path"),
    [
        ('graphs: "*", level: read, markings: [ex:personal]', "grants[0].graphs"),
        ("graphs: [example_graph], level: read, markings: []", "grants[0].markings"),
        ("graphs: [example_graph], level: read, markings: ex:Personal", "grants[0].markings[0]"),
        ("graphs: [example_graph], level: read, markings: [personal]", "grants[0].markings[0]"),
        ("graphs: [example_graph], level: read, markings: [ex:*]", "grants[0].markings[0]"),
        ("graphs: [example_graph], level: read, markings: [ex:a, '']", "grants[0].markings[1]"),
        ("graphs: [example_graph], level: read, markings: {ex: personal}", "grants[0].markings"),
    ],
)
def test_invalid_markings_list_their_problem(fragment: str, path: str) -> None:
    source = (
        "schema: graph_cli.policy/1\nid: installation\n"
        f"grants:\n  - {{{fragment}}}\nno_project: {{'*': read}}\n"
    )
    assert path in [p for p, _ in problems_of(source)]


def test_a_policy_without_markings_compiles_to_no_marking_rule() -> None:
    source = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {graphs: [example_graph], level: read}
no_project: {"*": read}
"""
    compiled = compile_installation(source)
    assert all("marking" not in str(rule) for rule in rules_of(compiled).values())


# ---- the document grammar ------------------------------------------------------------------


def document(
    resource: str | list[str], actions: list[str], effect: str = "allow"
) -> dict[str, JsonValue]:
    return {
        "schema": "graph_cli.policy/1",
        "id": "installation",
        "layer": "installation",
        "projects": [],
        "rules": [
            {
                "id": "r",
                "effect": effect,
                "principal": {"entrypoint": "server"},
                "action": actions,
                "resource": resource,
            }
        ],
        "obligations": [],
    }


def paths(doc: dict[str, JsonValue]) -> list[str]:
    return [p.path for p in document_problems(doc)]


def test_a_rule_on_a_marking_names_read_get_only() -> None:
    assert paths(document(PERSONAL, ["read.get"])) == []
    assert paths(document([PERSONAL, GRAPH], ["read.get"])) == []
    for actions in (["read.*"], ["*"], ["read.find"], ["write.create"], ["read.get", "read.list"]):
        assert paths(document(PERSONAL, actions)) == ["rules[0].action"], actions
    # the same restriction for a deny rule that names a marking
    assert paths(document(PERSONAL, ["read.*"], "deny")) == ["rules[0].action"]


@pytest.mark.parametrize(
    "resource",
    [
        f"marking:{EXAMPLE}/ex:*",
        f"marking:{EXAMPLE}*",
        "marking:*",
        f"marking:{EXAMPLE}",
        f"marking:{EXAMPLE}/personal",
        f"marking:{EXAMPLE}/ex:Personal",
        "marking:not-a-uuid/ex:personal",
        f"marking:{EXAMPLE}/ex:personal/attr:name",
    ],
)
def test_marking_resources_are_exact(resource: str) -> None:
    """No marking globs: a grant lists its markings, so a new marking stays hidden until named."""
    assert "rules[0].resource" in paths(document(resource, ["read.get"]))


def test_authorize_refuses_a_malformed_marking_resource() -> None:
    with pytest.raises(GraphCliError) as info:
        authorize(HR, "read.get", f"marking:{EXAMPLE}/ex:*", CTX, installed())
    assert info.value.code == "internal.error"


def test_marking_resource_builds_the_exact_resource() -> None:
    assert marking_resource(EXAMPLE, "ex:personal") == PERSONAL
    assert marking_resource(EXAMPLE, "personal") is None
    assert marking_resource(EXAMPLE, "ex:*") is None
    assert marking_resource(EXAMPLE, "Ex:personal") is None


# ---- authorize on marking resources --------------------------------------------------------


@pytest.mark.parametrize(
    ("who", "resource", "expected"),
    [
        # a grant names its markings and nothing else
        (HR, PERSONAL, "allow:grant.1.markings"),
        (HR, INTERNAL, "deny:default.deny"),
        (LEADS, INTERNAL, "allow:grant.2.markings"),
        (LEADS, PERSONAL, "deny:default.deny"),
        # rights of two roles add up
        (BOTH, PERSONAL, "allow:grant.1.markings"),
        (BOTH, INTERNAL, "allow:grant.2.markings"),
        # a grant of every graph, an unrelated role and no role hold no marking
        (REPORTING, PERSONAL, "deny:default.deny"),
        (REPORTING, INTERNAL, "deny:default.deny"),
        (NOBODY, PERSONAL, "deny:default.deny"),
        # the marking of another graph is another resource
        (HR, f"marking:{OTHER}/ex:personal", "deny:default.deny"),
    ],
)
def test_markings_are_held_by_the_grants_that_name_them(
    who: ActorContext, resource: str, expected: str
) -> None:
    assert decide(who, "read.get", resource) == expected


def test_a_marking_grants_no_other_action() -> None:
    for action in ("read.find", "read.list", "write.create", "write.update", "read.describe"):
        assert decide(HR, action, PERSONAL) == "deny:default.deny"


def test_no_graph_grant_covers_a_marking() -> None:
    """The levels glob ``graph:<id>*`` and ``graph:*`` of every other grant stops at ``graph:``:
    a caller that reads and writes the whole graph holds no marking."""
    full = token("hr")
    assert decide(full, "read.get", GRAPH) == "allow:grant.1"
    assert decide(full, "read.get", INTERNAL) == "deny:default.deny"
    assert decide(REPORTING, "read.get", GRAPH) == "allow:grant.3"
    assert decide(REPORTING, "read.get", PERSONAL) == "deny:default.deny"


def test_the_built_in_policy_grants_no_marking_to_the_app() -> None:
    app = dataclasses.replace(actor(), client=ClientInfo("graph", "0.1.0", Entrypoint.APP))
    decision = authorize(app, "read.get", PERSONAL, CTX, BUILTIN_POLICY_SET)
    assert (decision.effect, decision.rule_id) == ("deny", "default.deny")
    assert authorize(app, "read.get", f"graph:{EXAMPLE}", CTX, BUILTIN_POLICY_SET).effect == "allow"
    maintainer = authorize(maint_actor(), "read.get", PERSONAL, CTX, BUILTIN_POLICY_SET)
    assert (maintainer.effect, maintainer.rule_id) == ("allow", "builtin.allow_maint")


def test_scopes_no_project_and_deny_rules_do_not_reach_a_marking() -> None:
    """They all name ``graph:`` resources: the frozen project keeps the graph from its callers
    (so a marking is moot there) but never withholds the marking, and a deny rule of every
    graph does not either. What a marking does not get from them it does not lose to them."""
    frozen = token("hr", project="frozen_project")
    assert decide(frozen, "read.get", GRAPH) == "deny:scope.frozen_project.example_graph"
    assert decide(frozen, "read.get", PERSONAL) == "allow:grant.1.markings"
    explorer = dataclasses.replace(
        HR, agent=Facet(Assurance.ASSERTED, id="a", type="explore", source="pointer")
    )
    assert decide(explorer, "read.get", GRAPH) == "deny:deny.no_explorers"
    assert decide(explorer, "read.get", PERSONAL) == "allow:grant.1.markings"


def test_a_graph_layer_does_not_hide_a_marking() -> None:
    assert decide(LEADS, "read.get", f"{GRAPH}/type:{PERSON}", GRAPH_LAYER) == (
        "deny:deny.no_person_reads"
    )
    assert decide(LEADS, "read.get", INTERNAL, GRAPH_LAYER) == "allow:grant.2.markings"
    assert decide(HR, "read.get", PERSONAL, GRAPH_LAYER) == "allow:grant.1.markings"


def test_the_maintainer_rules_cover_markings() -> None:
    for resource in (PERSONAL, INTERNAL, f"marking:{OTHER}/ex:personal"):
        assert decide(maint_actor(), "read.get", resource) == "allow:maint.any"


def test_a_project_admin_never_holds_a_marking() -> None:
    """The ceiling refuses every resource outside ``graph:`` before any rule is looked at."""
    admin = dataclasses.replace(
        maint_actor(), project=Facet(Assurance.VERIFIED, id="ops", source="token")
    )
    for resource in (PERSONAL, INTERNAL):
        decision = authorize(admin, "read.get", resource, CTX, installed())
        assert (decision.effect, decision.rule_id, decision.code) == (
            "deny",
            "ceiling.project_maint",
            "access.ceiling",
        )
    # while the graph itself is read through the maintain scope, as before
    assert decide(admin, "read.get", GRAPH) == "allow:scope.ops:maintain"


# ---- held markings and the allowance --------------------------------------------------------

USED = ("ex:personal", "ex:team_internal")


def test_held_markings_are_the_granted_ones() -> None:
    policy = installed()
    assert held_markings(HR, EXAMPLE, policy, USED) == {"ex:personal"}
    assert held_markings(LEADS, EXAMPLE, policy, USED) == {"ex:team_internal"}
    assert held_markings(BOTH, EXAMPLE, policy, USED) == set(USED)
    assert held_markings(REPORTING, EXAMPLE, policy, USED) == frozenset()
    assert held_markings(maint_actor(), EXAMPLE, policy, USED) == set(USED)
    assert held_markings(HR, OTHER, policy, USED) == frozenset()
    assert held_markings(HR, EXAMPLE, policy, ()) == frozenset()


def test_a_name_outside_the_marking_grammar_is_never_held() -> None:
    assert held_markings(maint_actor(), EXAMPLE, installed(), ["personal", "ex:*"]) == frozenset()


def test_the_built_in_policy_holds_nothing_for_the_app() -> None:
    app = dataclasses.replace(actor(), client=ClientInfo("graph", "0.1.0", Entrypoint.APP))
    assert held_markings(app, EXAMPLE, BUILTIN_POLICY_SET, USED) == frozenset()


def test_the_allowance_lists_held_markings_only_when_something_is_hidden() -> None:
    policy = installed()
    assert marking_allowance(HR, EXAMPLE, policy, USED) == ["ex:personal"]
    assert marking_allowance(REPORTING, EXAMPLE, policy, USED) == []
    # a caller that holds every used marking, and a graph that uses none: nothing to say
    assert marking_allowance(BOTH, EXAMPLE, policy, USED) is None
    assert marking_allowance(maint_actor(), EXAMPLE, policy, USED) is None
    assert marking_allowance(REPORTING, EXAMPLE, policy, ()) is None
    assert marking_allowance(HR, EXAMPLE, policy, ["ex:personal"]) is None


def test_held_markings_follow_the_policy_that_is_asked() -> None:
    """Pure: a marking is decided like any resource, and nothing is recorded or cached here."""
    empty = policy_set(
        compile_installation(
            INSTALLATION.replace("markings: [ex:personal]", "markings: [ex:team_internal]")
        ).document
    )
    assert held_markings(HR, EXAMPLE, empty, USED) == {"ex:team_internal"}


# ---- named markings ------------------------------------------------------------------------


def test_named_markings_are_the_markings_the_grants_name() -> None:
    documents = installed(GRAPH_LAYER).documents
    assert named_markings(documents, EXAMPLE) == ("ex:personal", "ex:team_internal")
    assert named_markings(documents, OTHER) == ()
    assert named_markings(BUILTIN_POLICY_SET.documents, EXAMPLE) == ()


def test_only_marking_grants_of_the_installation_layer_count() -> None:
    forged: dict[str, JsonValue] = {
        "schema": "graph_cli.policy/1",
        "id": "graph",
        "layer": "graph",
        "graph": str(EXAMPLE),
        "types": {},
        "rules": [
            {
                "id": "grant.1.markings",
                "effect": "deny",
                "action": ["read.get"],
                "resource": [PERSONAL],
            }
        ],
        "obligations": [],
    }
    assert named_markings([forged], EXAMPLE) == ()
    other_rule = document(PERSONAL, ["read.get"])
    assert named_markings([other_rule], EXAMPLE) == ()  # the rule id is not grant.<n>.markings


# ---- the model checks ----------------------------------------------------------------------


def models_of(model: TermModel) -> Callable[[GraphId], TermModel | None]:
    return lambda graph: model if graph == EXAMPLE else None


def test_the_term_model_carries_the_markings() -> None:
    assert TERMS.markings == {"ex:personal", "ex:team_internal"}
    person = TERMS.types[PERSON].attributes
    assert person["email"].markings == ("ex:personal",)
    assert person["remark"].markings == ("ex:team_internal",)
    assert person["name"].markings == ()


def test_an_undeclared_marking_is_collected_not_refused() -> None:
    source = INSTALLATION.replace("markings: [ex:personal]", "markings: [ex:personal, ex:ghost]")
    compiled = compile_installation(source, models_of(TERMS))
    assert compiled.unknown_markings == (UnknownMarking(EXAMPLE, "ex:ghost"),)
    # the document does not depend on the model
    assert compiled.hash == compile_installation(source).hash
    assert compile_installation(source).unknown_markings == ()
    assert compile_installation(INSTALLATION, models_of(TERMS)).unknown_markings == ()


WRITES = """\
schema: graph_cli.policy/1
id: installation
roles:
  importer: {description: "Importers"}
grants:
  - entrypoint: server
    principal: {role: importer}
    graphs: [example_graph]
    level: read
    markings: [MARKINGS]
    writes:
      ex:Person:
        create: CREATE
        update: UPDATE
no_project: {"*": read}
"""


def writes_policy(held: str, create: str, update: str) -> str:
    return WRITES.replace("MARKINGS", held).replace("CREATE", create).replace("UPDATE", update)


def marked_writes(held: str, create: str, update: str, model: TermModel = TERMS) -> list[object]:
    compiled = compile_installation(writes_policy(held, create, update), models_of(model))
    return list(compiled.unusable_marked_writes)


def test_a_write_list_naming_a_marked_attribute_the_grant_does_not_hold_is_dead() -> None:
    found = marked_writes("ex:personal", "[name, email, remark]", "[email, remark, team]")
    assert found == [
        UnusableMarkedWrite(EXAMPLE, PERSON, "create", ("remark",), ("ex:team_internal",), False),
        UnusableMarkedWrite(EXAMPLE, PERSON, "update", ("remark",), ("ex:team_internal",), False),
    ]


def test_true_covers_every_marked_attribute_of_the_type() -> None:
    found = marked_writes("ex:team_internal", "true", "true")
    assert found == [
        UnusableMarkedWrite(EXAMPLE, PERSON, "create", ("email",), ("ex:personal",), False),
        UnusableMarkedWrite(EXAMPLE, PERSON, "update", ("email",), ("ex:personal",), False),
    ]


def test_a_grant_that_holds_the_markings_has_no_dead_write() -> None:
    assert marked_writes("ex:personal, ex:team_internal", "true", "true") == []
    assert marked_writes("ex:personal", "[name, email]", "[email, team]") == []
    # a grant without markings: every marked attribute it names is dead
    unmarked = compile_installation(
        writes_policy("ex:personal", "[name, email]", "[email]").replace(
            "    markings: [ex:personal]\n", ""
        ),
        models_of(TERMS),
    )
    assert [w.attributes for w in unmarked.unusable_marked_writes] == [("email",), ("email",)]


def test_a_create_of_a_required_marked_attribute_without_default_is_blocked() -> None:
    person = TERMS.types[PERSON]
    required = dataclasses.replace(person.attributes["email"], must_give=True)
    model = TermModel(
        {
            **TERMS.types,
            PERSON: dataclasses.replace(
                person, attributes={**person.attributes, "email": required}
            ),
        },
        TERMS.markings,
    )
    assert marked_writes("ex:team_internal", "true", "[remark]", model) == [
        UnusableMarkedWrite(EXAMPLE, PERSON, "create", ("email",), ("ex:personal",), True),
    ]
    # an update never creates: the same attribute is only a dead right there
    assert marked_writes("ex:team_internal", "[name, remark]", "[email]", model) == [
        UnusableMarkedWrite(EXAMPLE, PERSON, "update", ("email",), ("ex:personal",), False),
        # the create that omits the required attribute is the existing "missing" problem
    ]


def test_the_compiled_document_does_not_depend_on_markings_of_the_model() -> None:
    source = writes_policy("ex:personal", "[name, email]", "[email]")
    assert compile_installation(source).hash == compile_installation(source, models_of(TERMS)).hash


# ---- authorize_unlisted_attribute ------------------------------------------------------------

EDITOR = token("editor")
WIDE = token("wide")


def attribute(name: str) -> str:
    return f"{GRAPH}/type:{PERSON}/attr:{name}"


def unlisted(who: ActorContext, name: str, *layers: str, action: str = "write.update") -> str:
    decision = authorize_unlisted_attribute(
        who, action, EXAMPLE, PERSON, name, CTX, installed(*layers)
    )
    assert decision.resource == attribute(name)  # the decision names the resource as asked
    return f"{decision.effect}:{decision.rule_id}"


def test_an_exact_name_in_a_write_list_does_not_match_an_unlisted_attribute() -> None:
    """The decision for an attribute no rule lists is the one a name the type lacks gets: the
    exact entry ``name`` of an ``update`` list does not match, ``attr:*`` and graph globs do."""
    assert decide(EDITOR, "write.update", attribute("team")) == "allow:grant.4.update"
    assert unlisted(EDITOR, "team") == "deny:default.deny"
    assert unlisted(EDITOR, "nope") == "deny:default.deny"
    assert decide(WIDE, "write.update", attribute("team")) == "allow:grant.5.update"
    assert unlisted(WIDE, "team") == "allow:grant.5.update"
    assert unlisted(WIDE, "nope") == "allow:grant.5.update"
    # a caller with the level write holds the graph glob
    assert unlisted(HR, "email") == "allow:grant.1"


def test_an_exact_attribute_deny_does_not_match_an_unlisted_attribute() -> None:
    exact = decide(WIDE, "write.update", attribute("team"), GRAPH_LAYER)
    assert exact == "deny:deny.no_team_edit"
    # as for a name the type does not have: the exact deny names nothing it matches
    assert unlisted(WIDE, "team", GRAPH_LAYER) == "allow:grant.5.update"
    assert unlisted(WIDE, "nope", GRAPH_LAYER) == "allow:grant.5.update"


def test_the_unlisted_attribute_is_decided_for_the_action_asked() -> None:
    assert unlisted(WIDE, "team", action="write.create") == "deny:default.deny"
    assert unlisted(HR, "team", action="write.create") == "allow:grant.1"
    assert unlisted(NOBODY, "team") == "deny:default.deny"
    assert unlisted(maint_actor(), "team") == "allow:maint.any"


def test_a_scope_and_deny_rules_still_decide_an_unlisted_attribute() -> None:
    frozen = token("hr", project="frozen_project")
    assert unlisted(frozen, "email") == "deny:scope.frozen_project.example_graph"


def test_an_unlisted_attribute_refuses_what_the_grammar_refuses() -> None:
    with pytest.raises(GraphCliError) as bad_action:
        authorize_unlisted_attribute(HR, "write.nothing", EXAMPLE, PERSON, "team", CTX, installed())
    assert bad_action.value.code == "internal.error"
    with pytest.raises(GraphCliError) as bad_name:
        authorize_unlisted_attribute(HR, "write.update", EXAMPLE, PERSON, "Team", CTX, installed())
    assert bad_name.value.code == "internal.error"
