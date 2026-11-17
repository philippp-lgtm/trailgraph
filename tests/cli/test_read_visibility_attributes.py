"""S: every read command answers about a hidden attribute exactly as about an attribute the type
does not declare (field visibility plan F5, F7, F9, section 6; leak catalogue items 2, 4-7, 10).

One data root holds two graphs written with the same seeded ids: ``example_graph`` (the ``staff``
module, which marks personal data) and ``twin_graph`` (its twin: the module without what a
caller holding no marking may see, and the same entities without the hidden values). Without an
installation policy the local app caller holds no marking, so it reads ``example_graph`` through
its read view, while ``twin_graph`` uses no marking at all. The oracle: every answer about
``example_graph`` equals the answer about ``twin_graph`` (same ids, ``rev``, ``tx_seq``, times;
content hashes of what is shown), but for the graph's name and id, and holds no hidden value or
term. A holder of the markings reads everything; a holder of some reads what they cover.

The migration surfaces (history across a release, ``names_mapped``, the ``--as-of`` warning,
rename hints) are in ``test_migration_visibility_attributes.py``.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init, write_documents
from support.app import app
from support.files import write_pdf
from support.markings import (
    HIDDEN_NAMES,
    HIDDEN_ONLY,
    HOLDER,
    PERSONAL_ONLY,
    READER,
    SECRETS,
    STAFF,
    STAFF_TWIN,
    documents,
    leaks,
    recomputed,
)
from support.read_limits import install

GRAPH = {"GRAPH_CLI_GRAPH": "example_graph"}
TWIN = {"GRAPH_CLI_GRAPH": "twin_graph"}
# Entities by alias (allocated in the order of the build document).
E1, E2, E3, E4 = "EMP-0001", "EMP-0002", "EMP-0003", "EMP-0004"
B1, B2, B3 = "BDG-0001", "BDG-0002", "BDG-0003"
C1, C2 = "CREW-0001", "CREW-0002"
K1, K2 = "CON-0001", "CON-0002"
EVERY = (E1, E2, E3, E4, B1, B2, B3, C1, C2, K1, K2)
# A function both graphs hold: an unlabelled node reads the e-mail of every colleague of a crew.
CREW_PEOPLE = """\
schema: graph_cli.function/1
name: crew_people
summary: Colleagues of a crew with their e-mail.
use_when: Who works in a crew and how to reach them.
params: {}
query: |
  MATCH (n)-[:in_crew]->(c:Crew)
  RETURN c.name AS crew, n.name AS name, n.email AS email, keys(n) AS fields
  ORDER BY crew, name
returns:
  - column: crew
    summary: Name of the crew.
  - column: name
    summary: Name of the colleague.
  - column: email
    summary: E-mail address.
  - column: fields
    summary: The fields the colleague has.
examples:
  - question: Who is in which crew?
"""
# A function only ``example_graph`` holds: it reads a hidden attribute on a typed node.
EMPLOYEE_MAIL = """\
schema: graph_cli.function/1
name: employee_mail
summary: Private e-mail of every employee.
use_when: How to reach an employee privately.
params: {}
query: |
  MATCH (e:Employee) RETURN e.name AS name, e.email AS email ORDER BY name
returns:
  - column: name
    summary: Name of the employee.
  - column: email
    summary: Private e-mail address.
examples:
  - question: What are the private e-mail addresses?
"""


@dataclass
class World:
    run_cli: CliRunner
    work: Path
    ids: dict[str, str]  # graph name -> graph id
    root: Path  # the data root

    def ask(self, graph: dict[str, str], *args: str) -> Outcome:
        return app(self.run_cli, *args, env=graph)

    def answer(self, outcome: Outcome, graph: str) -> Any:
        """What a caller gets, the graph's name and id written as those of ``example_graph``."""
        body = {
            "code": outcome.code,
            "data": outcome.data,
            "error": outcome.envelope.get("error"),
            "warnings": outcome.warnings,
        }
        text = json.dumps(body, sort_keys=True)
        text = text.replace(self.ids[graph], self.ids["example_graph"])
        return json.loads(text.replace(graph, "example_graph"))

    def same(self, *args: str) -> Outcome:
        """Ask both graphs: the answers are the same and name nothing hidden."""
        marked = self.ask(GRAPH, *args)
        twin = self.ask(TWIN, *args)
        got, want = self.answer(marked, "example_graph"), self.answer(twin, "twin_graph")
        assert got == want, json.dumps({"args": args, "marked": got, "twin": want}, indent=1)
        # a name or text the caller asked for comes back as asked (the twin echoes it too)
        found = [t for t in leaks(marked.envelope) if not any(t in arg for arg in args)]
        assert found == [], (args, found)
        return marked

    def ok(self, *args: str) -> Outcome:
        outcome = self.same(*args)
        assert outcome.code == 0, (args, outcome.envelope)
        return outcome

    def refused(self, code: str, *args: str) -> Outcome:
        outcome = self.same(*args)
        assert outcome.code != 0 and outcome.error["code"] == code, (args, outcome.envelope)
        return outcome


def publish(run_cli: CliRunner, graph: str, folder: Path, *texts: str) -> None:
    folder.mkdir(parents=True)
    for text in texts:
        name = text.split("\nname: ")[1].split("\n")[0]
        (folder / f"{name}.yaml").write_text(text)
    published = admin(run_cli, "graph", "functions", "publish", graph, str(folder))
    assert published.code == 0, published.envelope


@pytest.fixture
def world(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> World:
    root = init(run_cli, isolated_env)
    ids = {}
    for name, source in (("example_graph", STAFF / "1.0.0"), ("twin_graph", STAFF_TWIN)):
        created = admin(run_cli, "graph", "create", name, "--ontology", str(source))
        assert created.code == 0, created.envelope
        ids[name] = created.data["id"]
    publish(run_cli, "example_graph", tmp_path / "f1", CREW_PEOPLE, EMPLOYEE_MAIL)
    publish(run_cli, "twin_graph", tmp_path / "f2", CREW_PEOPLE)
    write_documents(root, "example_graph", documents())
    write_documents(root, "twin_graph", documents(twin=True))
    return World(run_cli, tmp_path / "policies", ids, root)


# ---- get ---------------------------------------------------------------------------------


def test_get_answers_as_over_the_twin(world: World) -> None:
    for ref in EVERY:
        world.ok("get", ref)
        full = world.ok("get", ref, "--full").data["entity"]
        assert full["content_hash"] == recomputed(full), ref
        first = world.ok("get", ref, "--rev", "1", "--full").data["entity"]
        assert first["content_hash"] == recomputed(first), ref
        world.ok("get", ref, "--fields", "phone,email,number")
        world.ok("get", ref, "--affordances")
    for rev in ("2", "3"):
        world.same("get", E1, "--rev", rev, "--full")
    # what is left of Ada: the visible fields, its edges, the stored rev and tx
    ada = world.ok("get", E1, "--full").data["entity"]
    assert sorted(ada["attrs"]) == ["name", "phone", "shift", "stage"]
    assert (ada["rev"], ada["tx_seq"], len(ada["edges"])) == (2, 2, 2)
    # a hidden field asked by name is unknown, with suggestions from the view
    for name in ("weekly_hours", "salary", "badge_code", "holder_name"):
        world.refused("term.unknown", "get", E1, "--fields", name)


def test_affordances_list_a_transition_whose_guard_reads_a_hidden_attribute(world: World) -> None:
    # Bo has no badge code: the guard refuses the transition on the whole graph, the view has
    # no guard (field visibility plan F5, accepted residual), the twin has none either
    for ref in (E1, E2, E3):
        transitions = world.ok("get", ref, "--affordances").data["affordances"]["transitions"]
        assert "equipped" in json.dumps(transitions), (ref, transitions)
    install(world.run_cli, world.work, HOLDER)
    bo = world.ask(GRAPH, "get", E2, "--affordances").data["affordances"]
    assert "badge_known" in json.dumps(bo["transitions"]), bo


# ---- find --------------------------------------------------------------------------------


def test_find_filters_sorts_and_shows_as_over_the_twin(world: World) -> None:
    for args in (
        ("find", "--type", "stf:Employee"),
        ("find", "--type", "stf:Employee", "--full"),
        ("find", "--include-inactive", "--limit", "100", "--full"),
        # no type: the contractors' e-mail is visible, the employees' is not (sibling classes)
        ("find", "--where", "email=ada@private.example"),
        ("find", "--where", "email=dee@agency.example"),
        ("find", "--where", "email:exists=true"),
        ("find", "--where", "email:exists=false", "--limit", "100"),
        ("find", "--where", "email:ne=dee@agency.example", "--limit", "100"),
        ("find", "--where", "email:contains=example", "--sort", "email"),
        ("find", "--sort", "email:desc", "--limit", "100"),
        ("find", "--type", "stf:Colleague", "--sort", "name", "--fields", "email,phone"),
        ("find", "--fields", "email", "--limit", "100"),
        ("find", "--type", "stf:Badge", "--sort", "number"),
        ("find", "--type", "stf:Contractor", "--where", "email:prefix=dee"),
    ):
        world.ok(*args)
    for args in (
        ("find", "--type", "stf:Employee", "--where", "email=ada@private.example"),
        ("find", "--type", "stf:Employee", "--sort", "email"),
        ("find", "--where", "weekly_hours:gt=1"),
        ("find", "--sort", "salary"),
        ("find", "--type", "stf:Badge", "--where", "holder_name:exists=true"),
        ("find", "--type", "stf:Employee", "--fields", "birth_date"),
    ):
        world.refused("term.unknown", *args)
    found = world.ok("find", "--where", "email:exists=true").data["items"]
    assert [i["alias"] for i in found] == [K1]


# ---- lookup and search -------------------------------------------------------------------


def test_lookup_does_not_match_a_hidden_label(world: World) -> None:
    for args in (
        ("lookup", "Ada"),
        ("lookup", "Ada", "--type", "stf:Employee"),
        ("lookup", "Ada Lovelace"),
        ("lookup", "Bo", "--match", "prefix"),
        ("lookup", "Core crew"),
        ("lookup", "Ada", "--include-inactive"),
    ):
        world.ok(*args)
    # a badge's only label is hidden: badges have no label
    world.refused("term.no_labels", "lookup", "Ada Lovelace", "--type", "stf:Badge")
    # the two Adas are told apart by their facts; a badge has no label to show there
    ada = world.ok("lookup", "Ada").data["items"]
    assert len(ada) == 2
    labels = [f["label"] for item in ada for f in item["facts"] if f["rel"] == "stf:holds_badge"]
    assert labels == [None, None]
    install(world.run_cli, world.work, HOLDER)
    holder = world.ask(GRAPH, "lookup", "Ada").data["items"]
    labels = [f["label"] for item in holder for f in item["facts"] if f["rel"] == "stf:holds_badge"]
    assert sorted(labels, key=str) == ["Ada Lovelace", None]
    badge = world.ask(GRAPH, "lookup", "Ada Lovelace", "--type", "stf:Badge").data["items"]
    assert [b["alias"] for b in badge] == [B1]


def test_search_does_not_search_a_hidden_field(world: World) -> None:
    for args in (
        ("search", "private"),
        ("search", "crew"),
        ("search", "Lovelace"),
        ("search", "Ada"),
        ("search", "private", "--field", "email"),
        ("search", "ada", "--type", "stf:Contractor", "--field", "email"),
    ):
        world.ok(*args)
    world.refused("term.unknown", "search", "ADA", "--field", "badge_code")
    world.refused("term.unknown", "search", "ada", "--type", "stf:Employee", "--field", "email")
    assert world.ok("search", "private").data["items"] == []


# ---- neighbors, query, run ---------------------------------------------------------------


def test_neighbors_show_stripped_neighbours(world: World) -> None:
    for args in (
        ("neighbors", C1),
        ("neighbors", C1, "--full"),
        ("neighbors", E1, "--depth", "2", "--full"),
        ("neighbors", B1, "--fields", "email,phone"),
    ):
        world.ok(*args)
    for item in world.ok("neighbors", C1, "--full").data["items"]:
        entity = item.get("entity", item)
        assert entity["content_hash"] == recomputed(entity), item


CYPHER = (
    "MATCH (n) RETURN n.name AS name, n.email AS email ORDER BY name",
    "MATCH (n) WHERE n.email IS NOT NULL RETURN n.name AS name",
    "MATCH (n) WHERE n.email CONTAINS 'private' RETURN n.name AS name",
    "MATCH (n) RETURN n.name AS name, properties(n) AS p, keys(n) AS k ORDER BY name",
    "MATCH (n) RETURN n ORDER BY n.email, n.name",
    "MATCH (n) RETURN count(n.email) AS mails, count(n) AS all",
    "MATCH (n) RETURN sum(n.weekly_hours) AS hours, collect(n.salary) AS pay",
    "MATCH (n) WHERE n.weekly_hours > 0 RETURN n.name AS name",
    "MATCH (n)-[:in_crew]->(c:Crew) RETURN c.name AS crew, collect(n.email) AS mails",
    "MATCH (n)-[:holds_badge]->(b) RETURN n.name AS name, b.holder_name AS holder",
    "MATCH (c:Contractor) RETURN c.email AS email ORDER BY email",
)


def test_query_reads_stripped_nodes(world: World) -> None:
    for text in CYPHER:
        world.same("query", text)
    for text in (
        "MATCH (e:Employee) RETURN e.email AS email",
        "MATCH (e:Employee) WHERE e.weekly_hours > 1 RETURN e.name",
        "MATCH (b:Badge) RETURN b.holder_name",
        "MATCH (e:Employee) RETURN e ORDER BY e.salary",
    ):
        world.refused("term.unknown", "query", text)
    rows = world.ok("query", CYPHER[0]).data["items"]
    assert {r["name"]: r["email"] for r in rows}["Dee"] == "dee@agency.example"
    assert {r["name"]: r["email"] for r in rows}["Bo"] is None


def test_run_reads_stripped_nodes_and_hides_a_function_that_reads_a_hidden_field(
    world: World,
) -> None:
    rows = world.ok("run", "crew_people").data["items"]
    assert {r["name"]: r["email"] for r in rows if r["crew"] == "Core crew"} == {
        "Ada": None,
        "Bo": None,
        "Dee": "dee@agency.example",
    }
    world.ok("describe", "functions")
    hidden = world.ask(GRAPH, "run", "employee_mail")
    missing = world.ask(GRAPH, "run", "no_such_function")
    assert hidden.code == missing.code and hidden.error["code"] == missing.error["code"]
    assert (
        hidden.error["message"].replace("employee_mail", "no_such_function")
        == (missing.error["message"])
    )


# ---- history and earlier states ----------------------------------------------------------


def test_history_and_as_of_answer_as_over_the_twin(world: World) -> None:
    for ref in EVERY:
        world.ok("history", ref)
    ada = world.ok("history", E1).data["items"]
    assert [i["changed"] and i["changed"]["attrs"] for i in ada] == [["phone"], None]
    for args in (
        ("get", E1, "--as-of", "tx:1", "--full"),
        ("get", E2, "--as-of", "tx:2"),
        ("find", "--as-of", "tx:1", "--where", "email:exists=true"),
        ("find", "--as-of", "tx:2", "--sort", "email", "--limit", "100"),
        ("neighbors", C1, "--as-of", "tx:1", "--full"),
        ("query", "MATCH (n) RETURN n.name AS name, keys(n) AS k ORDER BY name", "--as-of", "tx:1"),
        ("lookup", "Ada", "--as-of", "tx:1"),
        ("search", "private", "--as-of", "tx:1"),
    ):
        world.ok(*args)
    # a revision that changed a hidden attribute only: the twin has none, the view an empty
    # ``changed`` (a documented residual)
    write_documents(world.root, "example_graph", [HIDDEN_ONLY], seed=99)
    latest = world.ask(GRAPH, "history", E4).data["items"][0]
    assert latest["changed"]["attrs"] == [] and latest["reason"] == "more hours"
    assert leaks(world.ask(GRAPH, "history", E4).envelope) == []


def same_feed(world: World, *args: str) -> Outcome:
    """The feed over ``example_graph`` equals the feed over the twin but for ``view`` (the
    digest of the reader's read limits: null over the twin, which has none) and holds no
    hidden value or term."""
    marked, twin = world.ask(GRAPH, *args), world.ask(TWIN, *args)
    assert marked.code == 0 and twin.code == 0, (args, marked.envelope, twin.envelope)
    got, want = world.answer(marked, "example_graph"), world.answer(twin, "twin_graph")
    assert got["data"].pop("view") is not None and want["data"].pop("view") is None
    assert got == want, json.dumps({"args": args, "marked": got, "twin": want}, indent=1)
    found = [t for t in leaks(marked.envelope) if not any(t in arg for arg in args)]
    assert found == [], (args, found)
    return marked


def test_changes_answer_as_over_the_twin(world: World) -> None:
    every = same_feed(world, "changes", "--since", "tx:0", "--limit", "500")
    assert every.data["more"] is False
    # a hidden attribute is in no ``changed``; the twin has no such attribute at all
    names = {n for e in every.data["items"] for i in e["entities"] for n in _names(i["changed"])}
    assert "phone" in names and not names & set(HIDDEN_NAMES), names
    everything = {(i["id"], i["rev"]) for e in every.data["items"] for i in e["entities"]}
    # paged, resumed through every cursor: the same pages over both graphs, adding up to the
    # whole feed
    for limit in ("1", "2", "3"):
        seen, since = set(), "tx:0"
        while True:
            page = same_feed(world, "changes", "--since", since, "--limit", limit)
            seen |= {(i["id"], i["rev"]) for e in page.data["items"] for i in e["entities"]}
            since = page.data["next"]
            if not page.data["more"]:
                break
        assert seen == everything, limit
    same_feed(world, "changes", "--since", "tx:1")
    same_feed(world, "changes", "--since", every.data["items"][0]["recorded_at"])
    # a revision that changed a hidden attribute only: the feed lists it with an empty
    # ``changed``, as history does (a documented residual); the twin has no such revision
    write_documents(world.root, "example_graph", [HIDDEN_ONLY], seed=99)
    head = world.ask(GRAPH, "changes", "--since", "tx:0", "--limit", "500")
    last = head.data["items"][-1]
    assert (last["reason"], last["tx_seq"]) == ("more hours", head.data["head"])
    [item] = last["entities"]
    assert item["changed"]["attrs"] == [] and item["rev"] > 1
    assert leaks(head.envelope) == []
    # a holder of every marking sees the hidden names in the same feed, under another view
    install(world.run_cli, world.work, HOLDER)
    holder = world.ask(GRAPH, "changes", "--since", "tx:0", "--limit", "500")
    holder_names = {
        n for e in holder.data["items"] for i in e["entities"] for n in _names(i["changed"])
    }
    assert holder_names & set(HIDDEN_NAMES), holder_names
    assert holder.data["view"] != every.data["view"]  # the consumer must rebuild


def _names(changed: dict[str, Any] | None) -> set[str]:
    return set() if changed is None else set(changed["attrs"])


# ---- describe ------------------------------------------------------------------------------


def test_describe_shows_the_model_of_the_view(world: World) -> None:
    for args in (
        ("describe", "types"),
        ("describe", "type", "stf:Employee"),
        ("describe", "type", "stf:Colleague"),
        ("describe", "type", "stf:Badge"),
        ("describe", "type", "stf:Crew"),
        ("describe", "type", "stf:PersonnelFile"),
        ("describe", "fields", "stf:Employee"),
        ("describe", "fields", "stf:Contractor"),
        ("describe", "lifecycle", "stf:Employee"),
        ("describe", "module", "staff"),
        ("describe", "modules"),
        ("describe", "relations"),
        ("describe", "schema"),
        ("describe", "search", "email"),
        ("describe", "search", "salary"),
        ("describe", "search", "grade"),
        ("describe", "search", "badge"),
        ("describe", "functions"),
    ):
        world.ok(*args)


# ---- holders ---------------------------------------------------------------------------


def test_a_holder_reads_every_attribute_and_a_partial_holder_what_it_holds(
    world: World,
) -> None:
    install(world.run_cli, world.work, HOLDER)
    # positive control: the oracle tells a holder's answers from the twin's
    for args in (("get", E1), ("describe", "fields", "stf:Employee"), ("lookup", "Ada")):
        holder, twin = world.ask(GRAPH, *args), world.ask(TWIN, *args)
        assert world.answer(holder, "example_graph") != world.answer(twin, "twin_graph"), args
    ada = world.ask(GRAPH, "get", E1).data["entity"]["attrs"]
    assert ada["email"] == "ada@new.example" and ada["salary"] == "91234.50"
    found = world.ask(GRAPH, "find", "--type", "stf:Employee", "--where", "email:exists=true")
    assert sorted(i["alias"] for i in found.data["items"]) == [E1, E2]
    fields = world.ask(GRAPH, "describe", "fields", "stf:Employee").envelope
    assert "stf:personal" in json.dumps(fields) and "stf:pay" in json.dumps(fields)
    install(world.run_cli, world.work, PERSONAL_ONLY)
    ada = world.ask(GRAPH, "get", E1).data["entity"]["attrs"]
    assert "salary" not in ada and ada["weekly_hours"] == 35
    fields = json.dumps(world.ask(GRAPH, "describe", "fields", "stf:Employee").envelope)
    assert "salary" not in fields and "stf:pay" not in fields and "stf:personal" in fields
    refused = world.ask(GRAPH, "find", "--type", "stf:Employee", "--sort", "salary")
    assert refused.envelope.get("error", {}).get("code") == "term.unknown", refused.envelope
    summed = world.ask(GRAPH, "query", "MATCH (n) RETURN sum(n.salary) AS pay")
    assert summed.error["code"] == "term.unknown"  # no type of the view has it
    install(world.run_cli, world.work, READER)
    assert leaks(world.ask(GRAPH, "get", E1, "--full").envelope) == []


# ---- files ---------------------------------------------------------------------------------


def test_file_path_of_a_file_subclass_with_a_marked_attribute(world: World) -> None:
    install(world.run_cli, world.work, HOLDER)
    pdf = write_pdf(world.work.parent / "files" / "contract.pdf", "contract")
    added = world.ask(
        GRAPH,
        *("file", "add", str(pdf), "--type", "stf:PersonnelFile"),
        *("--set", "remark=Confidential remark", "--no-origin"),
    )
    assert added.code == 0, added.envelope
    entity = added.data["entity"]["id"]
    install(world.run_cli, world.work, READER)
    path = world.ask(GRAPH, "file", "path", entity)
    assert path.code == 0, path.envelope
    assert path.data["type"] == "stf:PersonnelFile" and path.data["hash"].startswith("sha256:")
    shown = world.ask(GRAPH, "get", entity, "--full")
    assert "remark" not in shown.data["entity"]["attrs"]
    assert shown.data["entity"]["content_hash"] == recomputed(shown.data["entity"])
    for outcome in (path, shown, world.ask(GRAPH, "file", "list")):
        assert "Confidential" not in json.dumps(outcome.envelope), outcome.envelope
    assert all(secret not in json.dumps(path.envelope) for secret in SECRETS)
