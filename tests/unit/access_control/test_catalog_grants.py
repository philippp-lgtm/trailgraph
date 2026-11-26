"""Catalog grants: ``level: catalog`` and the ``catalog`` key, the entry resources and their
document constraints, ``catalog_grants`` (catalog callers plan, steps 1a: the access-control core).

The authored policy is compiled and then evaluated with ``authorize()``, so the cases show
what a catalog grant means: a caller whose grant names an entry may run it and may read the
types of its ``reads``, and nothing else of the graph.
"""

import dataclasses
import json
import re
from collections.abc import Mapping, Sequence
from uuid import UUID

import pytest
from harness import REPO_ROOT
from support.audit_events import maint_actor
from support.fixtures import GOLDENS, compiled_model
from support.txbuilder import actor

from graph_cli.access_control.authorize import authorize
from graph_cli.access_control.compiler import compile_policy
from graph_cli.access_control.documents import (
    POLICY_SCHEMA,
    RESOURCE_RE,
    document_problems,
    fine_types,
    is_resource_glob,
    read_limited,
)
from graph_cli.access_control.effective import (
    CatalogGrants,
    catalog_grants,
    effective_level,
    graph_level,
    withheld_actions,
    write_grant_types,
    write_obligations,
)
from graph_cli.access_control.policy import BUILTIN_POLICY_SET, policy_set
from graph_cli.kernel.actions import ENTRY_ACTIONS
from graph_cli.kernel.actor import ActorContext, Assurance, ClientInfo, Entrypoint, Facet
from graph_cli.kernel.authz import AuthzContext
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GraphId, parse_id
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.services.term_model import term_model

EXAMPLE = GraphId(parse_id("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a"))
OTHER = GraphId(parse_id("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8b"))
THIRD = GraphId(parse_id("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8c"))
GRAPHS = {"example_graph": EXAMPLE, "other_graph": OTHER, "third_graph": THIRD}
CTX = AuthzContext(op_count=1, has_reason=True, dry_run=False)
GRAPH = f"graph:{EXAMPLE}"
TICKET = f"{GRAPH}/type:ex:Ticket"

HEAD = """\
schema: graph_cli.policy/1
id: installation
roles:
  support: {description: "Support agents: the ticket entries of example_graph"}
  triage: {description: "Triage: open tickets of two graphs"}
  reader: {description: "Readers: every type of example_graph"}
  closer: {description: "Readers that close tickets through an action"}
"""
SUPPORT_CATALOG = """\
    catalog:
      functions: [open_tickets, customer_summary]
      actions: [close_ticket]
      views: [ticket_overview]
"""
SUPPORT = (
    """\
  - entrypoint: server
    principal: {role: support}
    graphs: [example_graph]
    level: catalog
"""
    + SUPPORT_CATALOG
    + """\
    reads: ["ex:Ticket", "ex:Customer"]
    markings: ["ex:personal"]
"""
)
WRITER = (
    "  - {entrypoint: server, principal: {role: reader}, graphs: [example_graph], level: write}\n"
)
TRIAGE = """\
  - entrypoint: server
    principal: {role: triage}
    graphs: [example_graph, other_graph]
    level: catalog
    catalog: {functions: [open_tickets, escalations]}
    reads: ["ex:Ticket"]
"""
READER = """\
  - {entrypoint: server, principal: {role: reader}, graphs: [example_graph], level: read}
"""
CLOSER = """\
  - entrypoint: server
    principal: {role: closer}
    graphs: [example_graph]
    level: read
    catalog: {actions: [close_ticket]}
"""
TAIL = 'no_project: {"*": write}\n'


def source(*grants: str, tail: str = TAIL) -> bytes:
    return (HEAD + "grants:\n" + "".join(grants) + tail).encode()


def compiled(*grants: str, tail: str = TAIL) -> dict[str, JsonValue]:
    return compile_policy(source(*grants, tail=tail), file="p.yaml", graphs=GRAPHS).document


def policy(*grants: str, tail: str = TAIL, graph_layer: bytes | None = None) -> PolicySet:
    installation = compiled(*grants, tail=tail)
    if graph_layer is None:
        return policy_set(installation)
    layer = compile_policy(
        graph_layer, file="g.yaml", graphs=GRAPHS, graph=EXAMPLE, roles=frozenset({"support"})
    ).document
    return policy_set(installation, layer)


def rule_map(document: Mapping[str, JsonValue]) -> dict[str, dict[str, JsonValue]]:
    found = document["rules"]
    assert isinstance(found, list)
    return {str(r["id"]): r for r in found if isinstance(r, dict)}  # type: ignore[index]


def token(*roles: str, project: str | None = None, agent_type: str | None = None) -> ActorContext:
    """A server request whose token holds ``roles``."""
    base = actor()
    bound = Facet(Assurance.VERIFIED, id=project, source="token") if project else base.project
    agent = (
        Facet(Assurance.ASSERTED, id="a1", type=agent_type, source="client")
        if agent_type
        else base.agent
    )
    return dataclasses.replace(
        base,
        principal=Facet(Assurance.VERIFIED, id="token:t", name="t", source="token"),
        project=bound,
        agent=agent,
        client=ClientInfo("graph-server", "0.1.0", Entrypoint.SERVER),
        roles=roles,
    )


# ---- existing policies keep their documents and hashes --------------------------------


def _pins() -> dict[str, dict[str, JsonValue]]:
    data = json.loads(
        (GOLDENS / "access_control" / "policy_hashes_before_catalog.json").read_text()
    )
    return data["policies"]  # type: ignore[no-any-return]


def _names_entry_action(document: Mapping[str, JsonValue]) -> bool:
    found = document["rules"]
    assert isinstance(found, list)
    return any(
        a in ENTRY_ACTIONS
        for rule in found
        if isinstance(rule, dict)
        for a in rule["action"]  # type: ignore[union-attr]
    )


@pytest.mark.parametrize("name", sorted(_pins()))
def test_policies_without_catalog_keys_compile_to_the_same_hash(name: str) -> None:
    """The sources are the policy fixtures of the access-control tests and the examples of
    ``docs/access_control.md`` as they stood before ``read.view`` and ``write.action``, and
    their hashes were computed on the code before them: the new actions are in no level, so a
    policy that does not use ``catalog`` keeps its document and needs no ``policy apply``."""
    pin = _pins()[name]
    graphs = {k: GraphId(UUID(str(v))) for k, v in dict(pin["graphs"]).items()}  # type: ignore[call-overload]
    result = compile_policy(
        str(pin["source"]).encode(),
        file=f"{name}.yaml",
        graphs=graphs,
        graph=graphs["example_graph"],
    )
    assert result.hash == pin["hash"]
    assert not _names_entry_action(result.document)


def test_there_are_pins_for_every_kind_of_authored_policy() -> None:
    pins = _pins()
    assert len(pins) >= 25
    assert any(k.startswith("doc_") for k in pins)
    sources = "\n".join(str(p["source"]) for p in pins.values())
    for feature in (
        "writes:",
        "reads:",
        "markings:",
        "roles:",
        "scopes:",
        "maintain",
        "obligations:",
    ):
        assert feature in sources, feature


def test_the_examples_of_the_documentation_use_no_entry_action_unless_they_are_catalog_grants() -> (
    None
):
    """Every policy example of ``docs/access_control.md`` compiles; those that name no
    ``catalog`` compile to documents without ``read.view`` and ``write.action``."""
    text = (REPO_ROOT / "docs" / "access_control.md").read_text()
    blocks = re.findall(r"^( *)```yaml\n(.*?)^\1```", text, re.MULTILINE | re.DOTALL)
    graphs = {"example_graph": GraphId(UUID(int=1)), "other_graph": GraphId(UUID(int=2))}
    plain = 0
    for indent, body in blocks:
        if not body.lstrip().startswith("schema: graph_cli.policy/1"):
            continue
        dedented = "\n".join(line.removeprefix(indent) for line in body.splitlines()) + "\n"
        document = compile_policy(
            dedented.encode(), file="example.yaml", graphs=graphs, graph=graphs["example_graph"]
        ).document
        if "catalog" in dedented:
            assert _names_entry_action(document)
        else:
            plain += 1
            assert not _names_entry_action(document)
    assert plain >= 8


# ---- the compiler --------------------------------------------------------------------


def test_a_catalog_grant_compiles_to_its_rules() -> None:
    rules = rule_map(compiled(SUPPORT))
    own = [i for i in rules if i.startswith("grant.")]
    assert own == [
        "grant.1",
        "grant.1.reads",
        "grant.1.functions",
        "grant.1.views",
        "grant.1.actions",
        "grant.1.markings",
    ]
    principal = {"entrypoint": "server", "role": "support"}
    assert rules["grant.1"] == {
        "id": "grant.1",
        "effect": "allow",
        "principal": principal,
        # the graph and its model; the exact resource, never the glob that covers the entries
        "action": ["read.list", "read.describe"],
        "resource": [GRAPH],
    }
    assert rules["grant.1.reads"]["action"] == ["read.get"]
    assert rules["grant.1.reads"]["resource"] == [
        f"{GRAPH}/type:ex:Customer",
        f"{GRAPH}/type:ex:Ticket",
    ]
    assert rules["grant.1.functions"] == {
        "id": "grant.1.functions",
        "effect": "allow",
        "principal": principal,
        "action": ["read.function"],
        "resource": [f"{GRAPH}/function:customer_summary", f"{GRAPH}/function:open_tickets"],
    }
    assert rules["grant.1.views"]["action"] == ["read.view"]
    assert rules["grant.1.views"]["resource"] == [f"{GRAPH}/view:ticket_overview"]
    assert rules["grant.1.actions"]["action"] == ["write.action"]
    assert rules["grant.1.actions"]["resource"] == [f"{GRAPH}/action:close_ticket"]
    assert rules["grant.1.markings"]["action"] == ["read.get"]
    assert rules["grant.1.markings"]["resource"] == [f"marking:{EXAMPLE}/ex:personal"]
    assert document_problems(compiled(SUPPORT)) == []


def test_a_catalog_names_every_graph_and_leaves_out_a_kind_without_names() -> None:
    rules = rule_map(compiled(TRIAGE))
    assert [i for i in rules if i.startswith("grant.")] == [
        "grant.1",
        "grant.1.reads",
        "grant.1.functions",
    ]
    assert rules["grant.1"]["resource"] == [GRAPH, f"graph:{OTHER}"]
    assert rules["grant.1.functions"]["resource"] == [
        f"{GRAPH}/function:escalations",
        f"{GRAPH}/function:open_tickets",
        f"graph:{OTHER}/function:escalations",
        f"graph:{OTHER}/function:open_tickets",
    ]
    assert rules["grant.1.reads"]["resource"] == [
        f"{GRAPH}/type:ex:Ticket",
        f"graph:{OTHER}/type:ex:Ticket",
    ]


def test_the_compiled_document_is_deterministic_and_sorted() -> None:
    """Names are sorted and deduplicated, so the order a maintainer writes them in never
    changes the hash."""
    shuffled = SUPPORT.replace(
        "functions: [open_tickets, customer_summary]",
        "functions: [open_tickets, customer_summary, open_tickets]",
    ).replace('reads: ["ex:Ticket", "ex:Customer"]', 'reads: ["ex:Customer", "ex:Ticket"]')
    first = compile_policy(source(SUPPORT), file="p.yaml", graphs=GRAPHS)
    second = compile_policy(source(shuffled), file="p.yaml", graphs=GRAPHS)
    assert first.hash == second.hash
    assert first.document == second.document
    # a single name may be written as a string
    scalar = TRIAGE.replace(
        "catalog: {functions: [open_tickets, escalations]}", "catalog: {functions: open_tickets}"
    )
    assert rule_map(compiled(scalar))["grant.1.functions"]["resource"] == [
        f"{GRAPH}/function:open_tickets",
        f"graph:{OTHER}/function:open_tickets",
    ]


def test_a_read_grant_may_list_actions_and_keeps_its_read_rule() -> None:
    rules = rule_map(compiled(CLOSER))
    assert [i for i in rules if i.startswith("grant.")] == ["grant.1", "grant.1.actions"]
    assert "read.get" in rules["grant.1"]["action"]  # type: ignore[operator]
    assert "write.action" not in rules["grant.1"]["action"]  # type: ignore[operator]
    assert rules["grant.1"]["resource"] == [f"graph:{EXAMPLE}*"]  # type: ignore[comparison-overlap]
    assert rules["grant.1.actions"]["resource"] == [f"{GRAPH}/action:close_ticket"]
    with_reads = CLOSER.replace("level: read", 'level: read\n    reads: ["ex:Ticket"]')
    again = rule_map(compiled(with_reads))
    assert [i for i in again if i.startswith("grant.")] == [
        "grant.1",
        "grant.1.reads",
        "grant.1.actions",
    ]
    assert again["grant.1"]["resource"] == [GRAPH]


def test_grant_numbers_follow_the_grants() -> None:
    rules = rule_map(compiled(READER, SUPPORT, TRIAGE))
    assert {"grant.2", "grant.2.functions", "grant.3", "grant.3.functions"} <= set(rules)
    assert "grant.1.functions" not in rules


# one authored grant that must be refused: (grant text, path of the problem, part of its message)
REFUSED: list[tuple[str, str, str]] = [
    # level: catalog needs its keys
    (
        SUPPORT.replace(
            SUPPORT_CATALOG,
            "",
        ),
        "grants[0].catalog",
        "is required for level: catalog",
    ),
    (
        SUPPORT.replace('    reads: ["ex:Ticket", "ex:Customer"]\n', ""),
        "grants[0].reads",
        "is required for level: catalog",
    ),
    (
        SUPPORT.replace("entrypoint: server", "entrypoint: app").replace(
            "principal: {role: support}\n", "principal: {name: support}\n"
        ),
        "grants[0].entrypoint",
        "needs entrypoint: server",
    ),
    (
        SUPPORT.replace("entrypoint: server", "entrypoint: [app, server]"),
        "grants[0].entrypoint",
        "needs entrypoint: server",
    ),
    (
        SUPPORT.replace("  - entrypoint: server\n    principal", "  - principal"),
        "grants[0].entrypoint",
        "needs entrypoint: server",
    ),
    (SUPPORT.replace("graphs: [example_graph]", 'graphs: "*"'), "grants[0].graphs", 'not "*"'),
    (
        SUPPORT + "    writes: {ex:Ticket: {update: true}}\n",
        "grants[0].writes",
        "level: catalog takes no writes",
    ),
    # the catalog key
    (
        SUPPORT.replace("functions: [open_tickets, customer_summary]", "functions: []"),
        "grants[0].catalog.functions",
        "non-empty list",
    ),
    (
        SUPPORT.replace("actions: [close_ticket]", "actions: [Close_Ticket]"),
        "grants[0].catalog.actions[0]",
        "not an entry name",
    ),
    (
        SUPPORT.replace("views: [ticket_overview]", "views: [x]"),
        "grants[0].catalog.views[0]",
        "not an entry name",
    ),
    (
        SUPPORT.replace("views: [ticket_overview]", "views: [ticket-overview]"),
        "grants[0].catalog.views[0]",
        "not an entry name",
    ),
    (
        SUPPORT.replace("views: [ticket_overview]", "modules: [tickets]"),
        "grants[0].catalog.modules",
        "unknown key",
    ),
    (
        SUPPORT.replace(
            SUPPORT_CATALOG,
            "    catalog: {}\n",
        ),
        "grants[0].catalog",
        "at least one function, action or view",
    ),
    (
        SUPPORT.replace(
            SUPPORT_CATALOG,
            "    catalog: [open_tickets]\n",
        ),
        "grants[0].catalog",
        "must be a mapping",
    ),
    # where the key may stand
    (
        READER.replace("level: read}", "level: write, catalog: {actions: [close_ticket]}}"),
        "grants[0].catalog",
        "needs level: catalog or read",
    ),
    (
        CLOSER.replace(
            "catalog: {actions: [close_ticket]}", "catalog: {functions: [open_tickets]}"
        ),
        "grants[0].catalog.functions",
        "redundant with level: read",
    ),
    (
        CLOSER.replace("catalog: {actions: [close_ticket]}", "catalog: {views: [ticket_overview]}"),
        "grants[0].catalog.views",
        "redundant with level: read",
    ),
    (
        CLOSER.replace("entrypoint: server", "entrypoint: app").replace(
            "principal: {role: closer}", "principal: {name: closer}"
        ),
        "grants[0].entrypoint",
        "needs entrypoint: server",
    ),
    (CLOSER.replace("graphs: [example_graph]", 'graphs: "*"'), "grants[0].graphs", 'not "*"'),
]


@pytest.mark.parametrize(("grant", "path", "message"), REFUSED)
def test_a_refused_catalog_grant_is_policy_invalid_with_its_path(
    grant: str, path: str, message: str
) -> None:
    with pytest.raises(GraphCliError) as info:
        compile_policy(source(grant), file="p.yaml", graphs=GRAPHS)
    assert info.value.code == "policy.invalid"
    problems = info.value.details["problems"]  # type: ignore[index]
    assert any(p["path"] == path and message in p["message"] for p in problems), problems  # type: ignore[index, union-attr]


def test_catalog_is_no_scope_level_and_no_graph_layer_key() -> None:
    with pytest.raises(GraphCliError) as info:
        compile_policy(
            source(
                SUPPORT, tail='scopes:\n  p: {example_graph: catalog}\nno_project: {"*": write}\n'
            ),
            file="p.yaml",
            graphs=GRAPHS,
        )
    assert "must be one of none, read, write, maintain" in str(info.value)
    with pytest.raises(GraphCliError):
        compile_policy(
            b"schema: graph_cli.policy/1\nid: graph\ntypes:\n  ex:Ticket: catalog\n",
            file="g.yaml",
            graphs=GRAPHS,
            graph=EXAMPLE,
        )


def test_reads_with_a_catalog_are_checked_against_the_model_like_any_reads() -> None:
    """An unknown type is collected (a later release may bring it), an extended one is a
    problem: the model checks are the ones of every ``reads``."""
    model = term_model(compiled_model("notes_and_sources", "1.1.0"))
    grant = SUPPORT.replace(
        'reads: ["ex:Ticket", "ex:Customer"]', 'reads: ["nas:Topic", "nas:Missing"]'
    )
    result = compile_policy(source(grant), file="p.yaml", graphs=GRAPHS, models=lambda graph: model)
    assert [(u.type) for u in result.unknown_read_types] == ["nas:Missing"]


# ---- resource grammar and document constraints -------------------------------------


@pytest.mark.parametrize(
    "resource",
    [
        f"graph:{EXAMPLE}/function:open_tickets",
        f"graph:{EXAMPLE}/action:close_ticket",
        f"graph:{EXAMPLE}/view:ticket_overview",
        f"graph:{EXAMPLE}/function:ab",
    ],
)
def test_entry_resources_are_resources(resource: str) -> None:
    assert RESOURCE_RE.match(resource)
    assert is_resource_glob(resource)


@pytest.mark.parametrize(
    "resource",
    [
        f"graph:{EXAMPLE}/function:Open",
        f"graph:{EXAMPLE}/function:x",
        f"graph:{EXAMPLE}/function:a-b",
        f"graph:{EXAMPLE}/function:",
        f"graph:{EXAMPLE}/macro:open_tickets",
        f"graph:{EXAMPLE}/function:open_tickets/attr:x",
        "graph:not-a-uuid/function:open_tickets",
        f"graph:{EXAMPLE}/function:a" + "b" * 63,
    ],
)
def test_malformed_entry_resources_are_refused(resource: str) -> None:
    assert not RESOURCE_RE.match(resource)
    assert not is_resource_glob(resource)


@pytest.mark.parametrize(
    "glob_",
    [f"graph:{EXAMPLE}/function:*", f"graph:{EXAMPLE}/action:*", f"graph:{EXAMPLE}/view:*"],
)
def test_the_glob_of_every_entry_of_a_kind(glob_: str) -> None:
    assert is_resource_glob(glob_)
    assert not RESOURCE_RE.match(glob_)  # a glob names no resource to decide on
    # the prefix glob of a name stays refused: names only (plan Q3)
    assert not is_resource_glob(f"graph:{EXAMPLE}/function:open_*")


def doc(
    *rules: dict[str, JsonValue], obligations: Sequence[JsonValue] = ()
) -> dict[str, JsonValue]:
    return {
        "schema": POLICY_SCHEMA,
        "id": "x",
        "layer": "installation",
        "rules": list(rules),
        "obligations": list(obligations),
    }


def allow(
    actions: list[str], resource: str | list[str], rule_id: str = "r"
) -> dict[str, JsonValue]:
    return {"id": rule_id, "effect": "allow", "action": actions, "resource": resource}


FUNCTION = f"graph:{EXAMPLE}/function:open_tickets"
ACTION = f"graph:{EXAMPLE}/action:close_ticket"
VIEW = f"graph:{EXAMPLE}/view:ticket_overview"


@pytest.mark.parametrize(
    ("actions", "resource"),
    [
        (["read.function"], FUNCTION),
        (["read.view"], VIEW),
        (["write.action"], ACTION),
        (["read.function", "read.view", "write.action"], [FUNCTION, VIEW, ACTION]),
        (["read.*"], FUNCTION),  # an area glob covers the entries
        (["write.*"], ACTION),
        (["*"], VIEW),
        (["read.function"], f"graph:{EXAMPLE}/function:*"),
        # on a graph the three are decided too (today's read.function on the graph)
        (["read.function", "read.view", "write.action"], f"graph:{EXAMPLE}"),
        (["read.function", "read.view", "write.action"], f"graph:{EXAMPLE}*"),
        (["read.function", "read.view", "write.action"], "graph:*"),
        (["read.function", "read.get"], f"graph:{EXAMPLE}"),
    ],
)
def test_entry_resources_and_actions_that_belong_together_are_valid(
    actions: list[str], resource: str | list[str]
) -> None:
    assert document_problems(doc(allow(actions, resource))) == []
    deny = {"id": "d", "effect": "deny", "action": actions, "resource": resource}
    assert document_problems(doc(deny)) == []


@pytest.mark.parametrize(
    "actions",
    [
        ["read.get"],
        ["read.find"],
        ["write.create"],
        ["read.function", "write.update"],
        ["ext.notes.tag"],
        ["maint.audit_verify"],
    ],
)
def test_an_entry_resource_takes_only_the_entry_actions(actions: list[str]) -> None:
    for resource in (FUNCTION, ACTION, VIEW, [f"graph:{EXAMPLE}", FUNCTION]):
        problems = document_problems(doc(allow(actions, resource)))
        assert [p.path for p in problems] == ["rules[0].action"], (actions, resource)
        assert "no other action is decided on an entry" in problems[0].message


@pytest.mark.parametrize("action", ["read.function", "read.view", "write.action"])
@pytest.mark.parametrize(
    "resource",
    [
        "installation",
        "*",
        f"graph:{EXAMPLE}/type:ex:Ticket",
        f"graph:{EXAMPLE}/type:*",
        f"graph:{EXAMPLE}/type:ex:Ticket/attr:status",
        f"graph:{EXAMPLE}/type:ex:Ticket/rel:ex:about",
        f"marking:{EXAMPLE}/ex:personal",
    ],
)
def test_the_entry_actions_are_decided_on_graphs_and_entries_only(
    action: str, resource: str
) -> None:
    problems = document_problems(doc(allow([action], resource)))
    assert problems, (action, resource)
    assert all(p.path == "rules[0].action" for p in problems)
    assert any("is decided on a graph or on one of its entries" in p.message for p in problems)


def test_write_action_is_no_write_of_types_terms_or_obligations_by_accident() -> None:
    """A type or term rule never names ``write.action`` (the area ``write.*`` still covers
    it), so the type limits and ``writes`` never decide it; an obligation may name it."""
    typed = f"graph:{EXAMPLE}/type:ex:Ticket"
    assert document_problems(doc(allow(["write.*"], typed))) == []
    assert document_problems(doc(allow(["write.update"], typed))) == []
    assert document_problems(doc(allow(["write.action"], typed)))
    assert document_problems(doc(allow(["write.action"], f"{typed}/attr:status")))
    assert document_problems(doc(allow(["write.action"], f"{typed}/rel:ex:about")))
    assert document_problems(doc(allow(["write.update", "write.action"], typed)))
    assert document_problems(doc(allow(["read.view"], typed)))
    ok = doc(
        allow(["write.action"], ACTION),
        obligations=[{"action": ["write.action"], "require_reason": True}],
    )
    assert document_problems(ok) == []
    ok = doc(allow(["write.action"], ACTION), obligations=[{"action": ["write.*"], "max_batch": 5}])
    assert document_problems(ok) == []


def test_authored_rules_never_name_write_action_on_types_and_terms() -> None:
    graph_layer = b"schema: graph_cli.policy/1\nid: graph\ndeny:\n"
    for entry in (
        "  - {id: a, action: [write.action], type: [ex:Ticket]}\n",
        "  - {id: a, action: [write.action], type: [ex:Ticket], attribute: [status]}\n",
        "  - {id: a, action: [write.action], type: [ex:Ticket], relation: [ex:about]}\n",
        "  - {id: a, action: [read.view], type: [ex:Ticket]}\n",
    ):
        with pytest.raises(GraphCliError) as info:
            compile_policy(
                graph_layer + entry.encode(), file="g.yaml", graphs=GRAPHS, graph=EXAMPLE
            )
        assert info.value.code == "policy.invalid", entry
    # writes grants name the verbs of WRITE_VERBS: the entry action is none of them
    with pytest.raises(GraphCliError) as info:
        compile_policy(
            source(
                "  - {entrypoint: server, principal: {role: reader}, graphs: [example_graph],"
                " level: read, writes: {ex:Ticket: {action: true}}}\n"
            ),
            file="p.yaml",
            graphs=GRAPHS,
        )
    assert "unknown key" in str(info.value)


def test_installation_deny_rules_may_name_the_entry_actions() -> None:
    """A deny rule of the installation names a graph (``graph:<id>*``), which covers the
    entries below it."""
    tail = (
        'no_project: {"*": write}\ndeny:\n'
        "  - {id: no_views, role: [support], action: [read.view]}\n"
        "  - {id: no_actions, role: [support], action: [write.action], graph: [example_graph]}\n"
    )
    document = compiled(SUPPORT, tail=tail)
    assert document_problems(document) == []
    rules = rule_map(document)
    assert rules["deny.no_views"]["resource"] == ["graph:*"]
    assert rules["deny.no_actions"]["resource"] == [f"graph:{EXAMPLE}*"]
    who = token("support")
    installed = policy_set(document)
    assert authorize(who, "read.view", VIEW, CTX, installed).rule_id == "deny.no_views"
    assert authorize(who, "write.action", ACTION, CTX, installed).rule_id == "deny.no_actions"
    # the function is not named by a deny rule: its grant allows it
    assert authorize(who, "read.function", FUNCTION, CTX, installed).rule_id == "grant.1.functions"


def test_read_limits_see_a_catalog_grants_types_but_not_its_entries() -> None:
    """A catalog grant makes its graph read-limited (its ``reads`` decide the type
    visibility), and no entry resource looks like a type to ``read_limited`` or
    ``fine_types``."""
    documents = policy(SUPPORT).documents
    assert read_limited(documents, EXAMPLE)
    assert not read_limited(documents, OTHER)
    assert fine_types(documents, EXAMPLE) == frozenset()
    assert write_grant_types(token("support"), EXAMPLE, policy(SUPPORT)) == ()
    only_actions = policy(CLOSER).documents
    assert not read_limited(only_actions, EXAMPLE)


# ---- decisions under a catalog grant ---------------------------------------------------


def decide(who: ActorContext, action: str, resource: str, installed: PolicySet) -> str:
    decision = authorize(who, action, resource, CTX, installed)
    return f"{decision.effect}:{decision.rule_id}"


def test_a_catalog_grant_allows_its_entries_and_the_types_of_its_reads_only() -> None:
    installed = policy(SUPPORT)
    who = token("support")
    table = [
        # the graph and its model
        ("read.list", GRAPH, "allow:grant.1"),
        ("read.describe", GRAPH, "allow:grant.1"),
        # the named entries
        ("read.function", f"{GRAPH}/function:open_tickets", "allow:grant.1.functions"),
        ("read.function", f"{GRAPH}/function:customer_summary", "allow:grant.1.functions"),
        ("read.view", f"{GRAPH}/view:ticket_overview", "allow:grant.1.views"),
        ("write.action", f"{GRAPH}/action:close_ticket", "allow:grant.1.actions"),
        # the types it reads (visibility), a marking it holds
        ("read.get", TICKET, "allow:grant.1.reads"),
        ("read.get", f"{GRAPH}/type:ex:Customer", "allow:grant.1.reads"),
        ("read.get", f"marking:{EXAMPLE}/ex:personal", "allow:grant.1.markings"),
        # everything else is refused by the default
        ("read.function", f"{GRAPH}/function:escalations", "deny:default.deny"),
        ("read.view", f"{GRAPH}/view:other_view", "deny:default.deny"),
        ("write.action", f"{GRAPH}/action:open_ticket", "deny:default.deny"),
        # a kind's action on another kind's entry
        ("write.action", f"{GRAPH}/function:open_tickets", "deny:default.deny"),
        ("read.function", f"{GRAPH}/view:ticket_overview", "deny:default.deny"),
        ("read.function", GRAPH, "deny:default.deny"),
        ("read.view", GRAPH, "deny:default.deny"),
        ("write.action", GRAPH, "deny:default.deny"),
        ("read.get", GRAPH, "deny:default.deny"),
        ("read.find", GRAPH, "deny:default.deny"),
        ("read.traverse", GRAPH, "deny:default.deny"),
        ("read.query", GRAPH, "deny:default.deny"),
        ("read.history", GRAPH, "deny:default.deny"),
        ("write.create", GRAPH, "deny:default.deny"),
        ("write.create", TICKET, "deny:default.deny"),
        ("read.get", f"{GRAPH}/type:ex:Other", "deny:default.deny"),
        ("read.get", f"marking:{EXAMPLE}/ex:secret", "deny:default.deny"),
        # another graph
        ("read.list", f"graph:{OTHER}", "deny:default.deny"),
        ("read.function", f"graph:{OTHER}/function:open_tickets", "deny:default.deny"),
    ]
    for action, resource, expected in table:
        assert decide(who, action, resource, installed) == expected, (action, resource)


def test_the_entry_grant_is_the_one_of_the_caller() -> None:
    """The same names granted to another role, or to no one, allow nothing."""
    installed = policy(SUPPORT, TRIAGE)
    entry = f"{GRAPH}/function:open_tickets"
    assert decide(token("support"), "read.function", entry, installed) == "allow:grant.1.functions"
    assert decide(token("triage"), "read.function", entry, installed) == "allow:grant.2.functions"
    assert decide(
        token("triage"), "read.function", f"graph:{OTHER}/function:escalations", installed
    ) == ("allow:grant.2.functions")
    assert (
        decide(token("triage"), "read.view", f"{GRAPH}/view:ticket_overview", installed)
        == "deny:default.deny"
    )
    assert decide(token(), "read.function", entry, installed) == "deny:default.deny"
    assert decide(token("reader"), "read.function", entry, installed) == "deny:default.deny"
    assert decide(actor(), "read.function", entry, installed) == "deny:default.deny"  # a local app
    both = token("support", "triage")
    assert (
        decide(both, "read.function", f"{GRAPH}/function:escalations", installed)
        == "allow:grant.2.functions"
    )


def test_a_scope_takes_the_entry_actions_away_with_the_rest_of_its_class() -> None:
    """Scopes deny ``write.*`` (scope ``read``) and ``read.*`` (scope ``none``) on
    ``graph:<id>*``, which covers every entry resource."""
    tail = (
        "scopes:\n  frozen: {example_graph: none}\n  readonly: {example_graph: read}\n"
        'no_project: {"*": write}\n'
    )
    installed = policy(SUPPORT, tail=tail)
    function, action, view = FUNCTION, ACTION, VIEW
    frozen = token("support", project="frozen")
    assert decide(frozen, "read.function", function, installed) == "deny:scope.frozen.example_graph"
    assert decide(frozen, "read.view", view, installed) == "deny:scope.frozen.example_graph"
    assert decide(frozen, "write.action", action, installed) == "deny:scope.frozen.example_graph"
    assert decide(frozen, "read.list", GRAPH, installed) == "deny:scope.frozen.example_graph"
    readonly = token("support", project="readonly")
    assert decide(readonly, "read.function", function, installed) == "allow:grant.1.functions"
    assert decide(readonly, "read.view", view, installed) == "allow:grant.1.views"
    assert (
        decide(readonly, "write.action", action, installed) == "deny:scope.readonly.example_graph"
    )


def test_a_type_limit_of_the_graph_layer_hides_a_type_from_a_catalog_grant() -> None:
    layer = b"schema: graph_cli.policy/1\nid: graph\ntypes:\n  ex:Customer: none\n"
    installed = policy(SUPPORT, graph_layer=layer)
    who = token("support")
    assert decide(who, "read.get", TICKET, installed) == "allow:grant.1.reads"
    assert (
        decide(who, "read.get", f"{GRAPH}/type:ex:Customer", installed) == "deny:type.ex:Customer"
    )
    # a limit of "read" hides no type: it takes writes away
    layer = b"schema: graph_cli.policy/1\nid: graph\ntypes:\n  ex:Ticket: read\n"
    installed = policy(SUPPORT, graph_layer=layer)
    assert decide(who, "read.get", TICKET, installed) == "allow:grant.1.reads"
    assert decide(who, "write.update", TICKET, installed) == "deny:type.ex:Ticket"


# ---- catalog_grants ---------------------------------------------------------------------


def test_a_catalog_caller_is_catalog_only_and_gets_its_names() -> None:
    installed = policy(SUPPORT, TRIAGE, READER, CLOSER)
    found = catalog_grants(token("support"), EXAMPLE, installed)
    assert found == CatalogGrants(
        functions=("customer_summary", "open_tickets"),
        actions=("close_ticket",),
        views=("ticket_overview",),
        catalog_only=True,
    )
    assert found.names("function") == ("customer_summary", "open_tickets")
    assert found.names("action") == ("close_ticket",)
    assert found.names("view") == ("ticket_overview",)
    assert found
    with pytest.raises(ValueError):
        found.names("module")


def test_the_names_and_the_state_belong_to_one_graph() -> None:
    installed = policy(SUPPORT, TRIAGE)
    assert catalog_grants(token("support"), OTHER, installed) == CatalogGrants()
    assert not catalog_grants(token("support"), OTHER, installed)
    triage_other = catalog_grants(token("triage"), OTHER, installed)
    assert triage_other == CatalogGrants(
        functions=("escalations", "open_tickets"), catalog_only=True
    )
    assert catalog_grants(token("triage"), THIRD, installed) == CatalogGrants()


def test_two_catalog_grants_add_up_and_stay_catalog_only() -> None:
    installed = policy(SUPPORT, TRIAGE)
    found = catalog_grants(token("support", "triage"), EXAMPLE, installed)
    assert found.functions == ("customer_summary", "escalations", "open_tickets")
    assert found.actions == ("close_ticket",)
    assert found.catalog_only


def test_a_grant_of_the_graph_itself_makes_the_caller_no_catalog_caller() -> None:
    """Rights add up: a reader that also holds a catalog grant sees the graph as a reader
    does; the names are still reported."""
    installed = policy(SUPPORT, READER)
    both = catalog_grants(token("support", "reader"), EXAMPLE, installed)
    assert both.functions == ("customer_summary", "open_tickets")
    assert not both.catalog_only
    only_reader = catalog_grants(token("reader"), EXAMPLE, installed)
    assert only_reader == CatalogGrants()


def test_a_read_grant_with_catalog_actions_is_no_catalog_caller() -> None:
    installed = policy(CLOSER)
    found = catalog_grants(token("closer"), EXAMPLE, installed)
    assert found == CatalogGrants(actions=("close_ticket",), catalog_only=False)
    assert found  # it names an action


def test_a_deny_rule_does_not_make_a_reader_a_catalog_caller() -> None:
    """The state is decided from the grants, never from a probe: a deny of ``read.get`` (or
    of every read) leaves a reader what it was."""
    tail = (
        'no_project: {"*": write}\ndeny:\n  - {id: no_get, role: [reader], action: [read.get]}\n'
        "  - {id: no_reads, role: [closer], action: [read.*]}\n"
    )
    installed = policy(READER, CLOSER, SUPPORT, tail=tail)
    assert catalog_grants(token("reader"), EXAMPLE, installed) == CatalogGrants()
    assert authorize(token("reader"), "read.get", GRAPH, CTX, installed).code == "access.denied"
    closer = catalog_grants(token("closer"), EXAMPLE, installed)
    assert not closer.catalog_only
    # and a deny on a catalog caller's entry does not take its name away: the grant names it
    deny_entry = (
        'no_project: {"*": write}\ndeny:\n'
        "  - {id: no_functions, role: [support], action: [read.function]}\n"
    )
    support = catalog_grants(token("support"), EXAMPLE, policy(SUPPORT, tail=deny_entry))
    assert support.functions == ("customer_summary", "open_tickets")
    assert support.catalog_only


def test_no_catalog_in_the_builtin_policy_or_for_maintainers_and_apps() -> None:
    assert catalog_grants(actor(), EXAMPLE, BUILTIN_POLICY_SET) == CatalogGrants()
    assert catalog_grants(maint_actor(), EXAMPLE, BUILTIN_POLICY_SET) == CatalogGrants()
    installed = policy(SUPPORT)
    assert catalog_grants(actor(), EXAMPLE, installed) == CatalogGrants()
    # the maintainers' rule is a grant of the graph itself, whatever else names entries
    assert catalog_grants(maint_actor(), EXAMPLE, installed) == CatalogGrants()


def test_a_server_token_that_is_also_a_maintainer_is_no_catalog_caller() -> None:
    tail = 'maintainers:\n  - {id: "token:t"}\nno_project: {"*": write}\n'
    # a maintainer rule is for the maintenance entrypoint; a server token does not match it
    installed = policy(SUPPORT, tail=tail)
    assert catalog_grants(token("support"), EXAMPLE, installed).catalog_only
    assert catalog_grants(maint_actor(), EXAMPLE, installed) == CatalogGrants()


def test_a_project_scoped_catalog_grant_needs_the_verified_project() -> None:
    grant = SUPPORT.replace(
        "principal: {role: support}", "principal: {role: support, project: example_project}"
    )
    installed = policy(
        grant, tail='scopes:\n  example_project: {example_graph: write}\nno_project: {"*": write}\n'
    )
    assert catalog_grants(
        token("support", project="example_project"), EXAMPLE, installed
    ).catalog_only
    assert catalog_grants(token("support"), EXAMPLE, installed) == CatalogGrants()
    asserted = dataclasses.replace(
        token("support"), project=Facet(Assurance.ASSERTED, id="example_project", source="pointer")
    )
    assert catalog_grants(asserted, EXAMPLE, installed) == CatalogGrants()


def test_catalog_grants_is_independent_of_the_model() -> None:
    """Names are read from the compiled rules: a name no catalog publishes is still named."""
    found = catalog_grants(token("support"), EXAMPLE, policy(SUPPORT))
    assert "customer_summary" in found.functions


# ---- the ladders skip the entry actions -------------------------------------------------


def test_levels_obligations_and_withheld_skip_the_entry_actions() -> None:
    """``read.view`` and ``write.action`` are no rung of the ladder: a level does not need
    them, a deny of them is no withheld action of a level and no obligation lists them."""
    tail = (
        'no_project: {"*": write}\ndeny:\n'
        "  - {id: no_entries, role: [reader], action: [read.view, write.action]}\n"
        'obligations:\n  - {action: ["write.*"], require_reason: true}\n'
    )
    installed = policy(WRITER, tail=tail)
    who = token("reader")
    assert authorize(who, "write.action", ACTION, CTX, installed).code == "access.denied"
    assert graph_level(who, EXAMPLE, installed) == "write"
    assert effective_level(who, EXAMPLE, installed) == "write"
    assert withheld_actions(who, EXAMPLE, installed) == []
    obligations = write_obligations(who, EXAMPLE, installed)
    assert "write.action" not in obligations
    assert obligations["write.create"] == {"require_reason": True}
    # a reader that writes through an action only is still a reader
    closer = policy(CLOSER)
    assert graph_level(token("closer"), EXAMPLE, closer) == "read"
    assert effective_level(token("closer"), EXAMPLE, closer) == "read"
    assert withheld_actions(token("closer"), EXAMPLE, closer) == []


def test_a_write_level_grant_allows_write_action_only_through_the_glob() -> None:
    """``level: write`` expands to ``write.*`` (it covers ``write.action``), ``level: read``
    expands to named reads: ``read.view`` is not among them."""
    installed = policy(WRITER)
    who = token("reader")
    assert decide(who, "write.action", ACTION, installed) == "allow:grant.1"
    assert decide(who, "read.view", VIEW, installed) == "deny:default.deny"
    assert decide(who, "read.function", FUNCTION, installed) == "allow:grant.1"
