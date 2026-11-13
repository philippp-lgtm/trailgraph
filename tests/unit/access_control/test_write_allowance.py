"""``write_allowance`` and ``write_grant_types``: what a write allow list lets a caller write
(write allow lists plan W3, W10), over the toy ontology ``notes_and_sources`` 1.1.0."""

import dataclasses

from support.fixtures import compiled_model
from support.txbuilder import actor

from graph_cli.access_control.compiler import compile_policy
from graph_cli.access_control.effective import (
    write_allowance,
    write_grant_types,
    write_obligations,
)
from graph_cli.access_control.policy import policy_set
from graph_cli.access_control.terms import TermModel
from graph_cli.kernel.actor import ActorContext, Assurance, ClientInfo, Entrypoint, Facet
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.ids import GraphId, parse_id
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.services.term_model import term_model

EXAMPLE = GraphId(parse_id("0192f0a1-7c3e-7a10-8b2c-1f0e5d6c7b8a"))
GRAPHS = {"example_graph": EXAMPLE}
TERMS: TermModel = term_model(compiled_model("notes_and_sources", "1.1.0"))

INSTALLATION = """\
schema: graph_cli.policy/1
id: installation
roles:
  importer: {description: "Importers: notes and topics"}
  tagger: {description: "Taggers: update notes"}
  maintainer: {description: "Everything"}
grants:
  - entrypoint: server
    principal: {role: importer}
    graphs: [example_graph]
    level: read
    writes:
      nas:Note: {create: [title, body, kind], update: [body], link: [nas:about], retract: true}
      nas:Topic: {create: true, alias: true}
  - entrypoint: server
    principal: {role: tagger}
    graphs: [example_graph]
    level: read
    writes:
      nas:Note: {update: [kind], transition: [status]}
      nas:Missing: {create: true}
  - {entrypoint: server, principal: {role: maintainer}, graphs: [example_graph], level: write}
no_project: {"*": write}
"""
GRAPH_LAYER = """\
schema: graph_cli.policy/1
id: graph
types:
  "nas:Topic": read
obligations:
  - {action: [write.create], require_reason: true}
  - {action: ["write.*"], max_batch: 3}
"""


def server(*roles: str) -> ActorContext:
    return dataclasses.replace(
        actor(),
        principal=Facet(Assurance.VERIFIED, id="token:t", name="t", source="token"),
        client=ClientInfo("graph-server", "0.1.0", Entrypoint.SERVER),
        roles=roles,
    )


def policy(*layers: str) -> PolicySet:
    installation = compile_policy(INSTALLATION.encode(), file="i.yaml", graphs=GRAPHS).document
    graph_layers: list[dict[str, JsonValue]] = [
        compile_policy(
            text.encode(), file="g.yaml", graphs=GRAPHS, graph=EXAMPLE, roles=frozenset()
        ).document
        for text in layers
    ]
    return policy_set(installation, *graph_layers)


def allowance(
    who: ActorContext, *layers: str, terms: TermModel | None = TERMS
) -> dict[str, JsonValue] | None:
    return write_allowance(who, EXAMPLE, policy(*layers), terms)


def test_the_types_a_list_names_are_those_of_the_matching_rules() -> None:
    assert write_grant_types(server("importer"), EXAMPLE, policy()) == ("nas:Note", "nas:Topic")
    both = write_grant_types(server("importer", "tagger"), EXAMPLE, policy())
    assert both == ("nas:Missing", "nas:Note", "nas:Topic")  # a type the model lacks is named
    assert write_grant_types(server("maintainer"), EXAMPLE, policy()) == ()  # a level names none
    assert write_grant_types(server("other"), EXAMPLE, policy()) == ()
    assert write_grant_types(server(), EXAMPLE, policy()) == ()
    # only verified facts match: the same role asserted by an app caller names nothing
    assert (
        write_grant_types(dataclasses.replace(actor(), roles=("importer",)), EXAMPLE, policy())
        == ()
    )


def test_each_verb_is_true_or_the_allowed_terms() -> None:
    assert allowance(server("importer")) == {
        "nas:Note": {
            "create": ["body", "kind", "title"],
            "update": ["body"],
            "link": ["nas:about"],
            "retract": True,
        },
        "nas:Topic": {"create": True, "alias": True},
    }


def test_the_rights_of_several_roles_add_up() -> None:
    assert allowance(server("importer", "tagger")) == {
        "nas:Note": {
            "create": ["body", "kind", "title"],
            "update": ["body", "kind"],
            "link": ["nas:about"],
            "transition": True,
            "retract": True,
        },
        "nas:Topic": {"create": True, "alias": True},
    }
    # a type the model does not have is left out, never listed
    assert sorted(allowance(server("tagger")) or {}) == ["nas:Note"]


def test_a_caller_without_a_list_or_at_level_write_has_none() -> None:
    assert allowance(server("maintainer")) is None  # level write: everything, nothing to list
    assert allowance(server("other")) is None
    assert allowance(server()) is None


def test_a_type_limit_removes_the_verbs_of_the_type() -> None:
    found = allowance(server("importer"), GRAPH_LAYER)
    assert found is not None and sorted(found) == ["nas:Note"]  # Topic is read-only: no verb left
    # a limit on every named type leaves nothing to list
    assert (
        allowance(
            server("importer"),
            GRAPH_LAYER.replace('"nas:Topic": read', '"nas:Topic": read\n  "nas:Note": read'),
        )
        is None
    )


def test_without_a_model_the_types_are_probed_alone() -> None:
    """The model cannot be read: every term verb is true where the type allows the verb."""
    assert allowance(server("importer", "tagger"), terms=None) == {
        "nas:Missing": {"create": True},
        "nas:Note": {
            "create": True,
            "update": True,
            "link": True,
            "transition": True,
            "retract": True,
        },
        "nas:Topic": {"create": True, "alias": True},
    }


def test_the_obligations_of_a_limited_writer_come_from_its_allowed_types() -> None:
    layered = policy(GRAPH_LAYER)
    # the first allowed type decision gives the obligations; alias is granted on a read-only
    # type only, so it is no allowed write; retract carries the built-in reason obligation
    assert write_obligations(server("importer"), EXAMPLE, layered) == {
        "write.create": {"require_reason": True, "max_batch": 3},
        "write.link": {"max_batch": 3},
        "write.retract": {"require_reason": True, "max_batch": 3},
        "write.update": {"max_batch": 3},
    }
    assert write_obligations(server("importer"), EXAMPLE, policy()) == {
        "write.retract": {"require_reason": True}
    }
    # a caller at level write, and one with no list, get what they got before
    assert write_obligations(server("other"), EXAMPLE, layered) == {}
    assert set(write_obligations(server("maintainer"), EXAMPLE, layered)) == {
        "write.create",
        "write.update",
        "write.link",
        "write.unlink",
        "write.transition",
        "write.supersede",
        "write.retract",
        "write.alias",
    }
