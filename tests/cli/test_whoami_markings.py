"""S: ``graph whoami`` and ``graph-admin policy explain`` for a caller who lacks a marking (field
visibility plan F8), checked against real reads.

The ``staff`` graph marks personal data (``stf:personal``) and pay data (``stf:pay``). ``whoami``
lists the markings the caller holds, only when the graph's model uses markings and the caller
lacks one; a marking the caller lacks is never named. Every answer is compared with what
``get`` shows of a marked employee, so the list tells exactly which attributes the caller sees.
Callers that see every attribute, and graphs without markings, get the answers they got before.
"""

import json
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init, write_documents
from support.app import app, create_graph
from support.markings import (
    HIDDEN_TERMS,
    HOLDER,
    PERSONAL_ONLY,
    READER,
    SECRETS,
    STAFF,
    documents,
    leaks,
    without_twin,
)
from support.read_limits import install

GRAPH = "example_graph"
ENV = {"GRAPH_CLI_GRAPH": GRAPH}
E1 = "EMP-0001"
HOLDER, PERSONAL_ONLY, READER = (without_twin(p) for p in (HOLDER, PERSONAL_ONLY, READER))
# The marked attributes of ``stf:Employee`` with their markings (the fixture's declarations).
MARKED = {
    "birth_date": {"stf:personal"},
    "badge_code": {"stf:personal"},
    "email": {"stf:personal"},
    "pay_grade": {"stf:personal"},
    "salary": {"stf:personal", "stf:pay"},
    "weekly_hours": {"stf:personal"},
}
# A reader that holds personal data and may write some of the employee's attributes: the list
# names two marked ones, one of which (the salary) it holds no marking for.
WRITER = """\
schema: graph_cli.policy/1
id: installation
grants:
  - entrypoint: app
    graphs: [example_graph]
    level: read
    markings: [stf:personal]
    writes:
      stf:Employee: {create: [name, phone, email, salary], update: [phone, email, salary]}
no_project: {"*": write}
"""
# Roles: one holds personal data, one nothing, one everything.
ROLES = """\
schema: graph_cli.policy/1
id: installation
roles:
  hr: {description: "People team"}
  reporting: {description: "Reporting"}
  payroll: {description: "Payroll"}
grants:
  - entrypoint: server
    principal: {role: hr}
    graphs: [example_graph]
    level: read
    markings: [stf:personal]
  - {entrypoint: server, principal: {role: reporting}, graphs: [example_graph], level: read}
  - entrypoint: server
    principal: {role: payroll}
    graphs: [example_graph]
    level: read
    markings: [stf:personal, stf:pay]
  - {entrypoint: app, graphs: [example_graph], level: read}
no_project: {"*": write}
"""


class World(NamedTuple):
    work: Path  # the folder for policies
    root: Path  # the data root


@pytest.fixture
def world(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> World:
    """The staff graph with its data, and a graph without markings next to it."""
    root = init(run_cli, isolated_env)
    created = admin(run_cli, "graph", "create", GRAPH, "--ontology", str(STAFF / "1.0.0"))
    assert created.code == 0, created.envelope
    create_graph(run_cli, "plain_graph", "notes_and_sources", version="1.1.0")
    write_documents(root, GRAPH, documents())
    return World(tmp_path / "policies", root)


def graph_entry(run_cli: CliRunner, name: str = GRAPH) -> dict[str, Any]:
    outcome = app(run_cli, "whoami", env={"GRAPH_CLI_GRAPH": name})
    assert outcome.code == 0, outcome.envelope
    (graph,) = [g for g in outcome.data["permissions"]["graphs"] if g["graph"] == name]
    assert isinstance(graph, dict)
    return graph


def explain(run_cli: CliRunner, *args: str) -> Outcome:
    return admin(run_cli, "policy", "explain", "--graph", GRAPH, *args)


def seen(run_cli: CliRunner) -> set[str]:
    """The marked attributes of ``EMP-0001`` a ``get`` shows the caller."""
    outcome = app(run_cli, "get", E1, env=ENV)
    assert outcome.code == 0, outcome.envelope
    return set(outcome.data["entity"]["attrs"]) & set(MARKED)


def unheld(held: list[str]) -> list[str]:
    """The terms and values a caller must never meet in an answer: all but the markings it holds
    (an answer lists those)."""
    return [t for t in (*SECRETS, *HIDDEN_TERMS) if t not in held]


def lacks(held: list[str]) -> set[str]:
    """The marked attributes a caller that holds ``held`` does not see."""
    return {name for name, markings in MARKED.items() if not markings <= set(held)}


# ---- whoami ---------------------------------------------------------------------------------


def test_whoami_lists_the_markings_the_caller_holds(run_cli: CliRunner, world: World) -> None:
    for policy, held in ((READER, []), (PERSONAL_ONLY, ["stf:personal"])):
        install(run_cli, world.work, policy)
        graph = graph_entry(run_cli)
        assert graph["markings"] == held
        # the list is what the reads show: an attribute is visible when its markings are held
        assert seen(run_cli) == set(MARKED) - lacks(held)
        assert leaks(graph, unheld(held)) == []
    # never a marking the caller lacks, nor an attribute that carries one
    assert "stf:pay" not in json.dumps(graph)
    assert not any(name in json.dumps(graph) for name in MARKED)


def test_a_caller_that_holds_every_marking_gets_the_answer_it_got_before(
    run_cli: CliRunner, world: World
) -> None:
    install(run_cli, world.work, HOLDER)
    graph = graph_entry(run_cli)
    assert sorted(graph) == ["graph", "level", "obligations", "types", "withheld"]
    assert seen(run_cli) == set(MARKED)


def test_a_graph_without_markings_shows_none(run_cli: CliRunner, world: World) -> None:
    # the graph without markings is granted as well, so whoami lists it
    install(
        run_cli,
        world.work,
        READER.replace(
            "level: write}",
            "level: write}\n  - {entrypoint: app, graphs: [plain_graph], level: write}",
            1,
        ),
    )
    assert graph_entry(run_cli)["markings"] == []
    plain = graph_entry(run_cli, "plain_graph")
    assert sorted(plain) == ["graph", "level", "obligations", "types", "withheld"]
    explained = admin(run_cli, "policy", "explain", "--graph", "plain_graph")
    assert explained.code == 0 and "markings" not in explained.data


def test_whoami_names_no_hidden_attribute_in_writes(run_cli: CliRunner, world: World) -> None:
    install(run_cli, world.work, WRITER)
    graph = graph_entry(run_cli)
    assert graph["markings"] == ["stf:personal"]
    # the salary is marked ``stf:pay`` as well: the grant names it, the caller holds no right to it
    assert graph["writes"] == {
        "stf:Employee": {"create": ["email", "name", "phone"], "update": ["email", "phone"]}
    }
    assert "salary" not in json.dumps(graph)
    # a caller holding pay data too gets the list as the grant says
    install(run_cli, world.work, WRITER.replace("[stf:personal]", "[stf:personal, stf:pay]"))
    graph = graph_entry(run_cli)
    assert "markings" not in graph
    assert graph["writes"]["stf:Employee"]["update"] == ["email", "phone", "salary"]
    # one that holds nothing sees neither marked attribute in what it may write
    install(run_cli, world.work, WRITER.replace("    markings: [stf:personal]\n", ""))
    graph = graph_entry(run_cli)
    assert graph["markings"] == []
    assert graph["writes"] == {"stf:Employee": {"create": ["name", "phone"], "update": ["phone"]}}


def test_whoami_fails_closed_when_the_model_cannot_be_read(
    run_cli: CliRunner, world: World
) -> None:
    install(run_cli, world.work, PERSONAL_ONLY)
    assert graph_entry(run_cli)["markings"] == ["stf:personal"]
    graph_id = admin(run_cli, "graph", "show", GRAPH).data["id"]
    compiled = world.root / "graphs" / graph_id / "ontology" / "1.0.0" / "compiled.json"
    original = compiled.read_bytes()
    # a release that cannot be read may use markings: the caller holds none that it can prove
    compiled.unlink()
    assert graph_entry(run_cli)["markings"] == []
    # unreadable bytes that show the markings schema: the same
    compiled.write_bytes(b'{"schema": "graph_cli.compiled_ontology/2", broken')
    assert graph_entry(run_cli)["markings"] == []
    # unreadable bytes without any trace of markings: the pin cannot use them, nothing to say
    compiled.write_bytes(b"broken")
    assert "markings" not in graph_entry(run_cli)
    compiled.write_bytes(original)
    assert graph_entry(run_cli)["markings"] == ["stf:personal"]


# ---- policy explain -------------------------------------------------------------------------


def test_explain_shows_the_same_markings_as_whoami(run_cli: CliRunner, world: World) -> None:
    for policy, held in ((READER, []), (PERSONAL_ONLY, ["stf:personal"])):
        install(run_cli, world.work, policy)
        explained = explain(run_cli)
        assert explained.code == 0, explained.envelope
        assert explained.data["markings"] == graph_entry(run_cli)["markings"] == held
        assert "type_decision" not in explained.data
        assert leaks(explained.data, unheld(held)) == []
    install(run_cli, world.work, HOLDER)
    assert "markings" not in explain(run_cli).data


def test_explain_shows_what_a_token_with_roles_holds(run_cli: CliRunner, world: World) -> None:
    install(run_cli, world.work, ROLES)
    held = {"hr": ["stf:personal"], "reporting": [], "payroll": None}
    for role, markings in held.items():
        explained = explain(run_cli, "--role", role)
        assert explained.code == 0, explained.envelope
        assert explained.data.get("markings") == markings, role
    both = explain(run_cli, "--role", "hr", "--role", "reporting")
    assert both.data["markings"] == ["stf:personal"]


def test_explain_decides_one_marking(run_cli: CliRunner, world: World) -> None:
    install(run_cli, world.work, ROLES)

    def decide(marking: str, *who: str) -> dict[str, Any]:
        out = explain(run_cli, "--action", "read.get", "--marking", marking, *who)
        assert out.code == 0, out.envelope
        assert "markings" not in out.data and "type_decision" not in out.data
        assert out.data["resource"].endswith(f"/{marking}")
        assert out.data["resource"].startswith("marking:")
        (decision,) = out.data["decisions"]
        assert isinstance(decision, dict)
        return decision

    held = decide("stf:personal", "--role", "hr")
    assert held["effect"] == "allow" and held["rule"].endswith(".markings")
    lacking = decide("stf:pay", "--role", "hr")
    assert lacking["effect"] == "deny" and lacking["code"] == "access.denied"
    assert decide("stf:pay", "--role", "payroll")["effect"] == "allow"
    assert decide("stf:personal", "--role", "reporting")["effect"] == "deny"
    # the app caller of the installation holds none; a marking nobody declared is denied too
    assert decide("stf:personal")["effect"] == "deny"
    assert decide("ex:nothing", "--role", "hr")["effect"] == "deny"
    # the decision agrees with what the caller reads: held markings are the ones that allow
    install(run_cli, world.work, PERSONAL_ONLY)
    for marking in ("stf:personal", "stf:pay"):
        allowed = decide(marking)["effect"] == "allow"
        assert allowed == (marking in graph_entry(run_cli)["markings"])


@pytest.mark.parametrize(
    ("args", "code", "argument"),
    [
        (("--marking", "stf:personal"), "usage.missing_argument", "action"),
        (
            ("--marking", "stf:personal", "--action", "write.update"),
            "usage.invalid_argument",
            "action",
        ),
        (
            ("--marking", "stf:personal", "--action", "read.find"),
            "usage.invalid_argument",
            "action",
        ),
        (
            ("--marking", "stf:personal", "--action", "read.get", "--type", "stf:Employee"),
            "usage.invalid_argument",
            "marking",
        ),
        (
            (
                "--marking",
                "stf:personal",
                "--action",
                "read.get",
                "--type",
                "stf:Employee",
                "--attribute",
                "email",
            ),
            "usage.invalid_argument",
            "marking",
        ),
        (("--marking", "personal", "--action", "read.get"), "usage.invalid_argument", "marking"),
        (("--marking", "stf:*", "--action", "read.get"), "usage.invalid_argument", "marking"),
    ],
)
def test_explain_refuses_a_marking_question_that_is_not_one(
    run_cli: CliRunner, world: World, args: tuple[str, ...], code: str, argument: str
) -> None:
    out = explain(run_cli, *args)
    assert out.code == 2, out.envelope
    assert out.error["code"] == code
    assert out.error["details"]["argument"] == argument


def test_a_project_admin_holds_no_marking(run_cli: CliRunner, world: World) -> None:
    install(run_cli, world.work, ROLES)
    created = admin(
        run_cli, "token", "create", "ops", "--scope", "admin", "--project", "example_project"
    )
    assert created.code == 0, created.envelope
    out = explain(run_cli, "--token", "ops", "--action", "read.get", "--marking", "stf:personal")
    assert out.code == 0, out.envelope
    (decision,) = out.data["decisions"]
    # the ceiling: a project admin reads no data, so it holds no marking, whatever a rule says
    assert decision["effect"] == "deny" and decision["code"] == "access.ceiling"
    assert out.data["resource"].startswith("marking:")
    # without an action it is listed as holding none
    assert explain(run_cli, "--token", "ops").data["markings"] == []


def test_an_installation_admin_holds_every_marking(run_cli: CliRunner, world: World) -> None:
    install(run_cli, world.work, ROLES)
    created = admin(run_cli, "token", "create", "root", "--scope", "admin")
    assert created.code == 0, created.envelope
    out = explain(run_cli, "--token", "root", "--action", "read.get", "--marking", "stf:pay")
    assert out.code == 0, out.envelope
    (decision,) = out.data["decisions"]
    assert decision["effect"] == "allow" and decision["rule"] == "maint.any"
    assert "markings" not in explain(run_cli, "--token", "root").data
