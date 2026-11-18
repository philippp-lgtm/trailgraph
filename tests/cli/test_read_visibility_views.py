"""S: published views for a caller who sees less (views plan V10, B3; leak catalogue 1, 3, 6, 7,
8, 9).

The ``staff`` graph holds employees with marked attributes, contractors, crews and badges, two
functions and a catalog of views over them. A view is visible exactly when everything it names is:
the classes, relations, attributes and parameters it uses (a type limit or a marking that removes
one hides the view whole), the functions it runs (which also need ``read.function``) and, for a
set view, ``read.find``. A view that is not visible answers exactly as a name that was never
published; the default view of a class falls back to the next one up the class chain, or the
card; what ``fields: all`` and the listings show leaves out what the caller may not see.
"""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init, write_documents
from support.app import app
from support.markings import (
    FUNCTION_SOURCES,
    HIDDEN_NAMES,
    HOLDER,
    PERSONAL_ONLY,
    READER,
    STAFF,
    documents,
    leaks,
    without_twin,
)
from support.read_limits import install, shown, the_same_but_for, without_suggestions
from support.views import stale_on_disk

HOLDER, PERSONAL_ONLY, READER = (without_twin(p) for p in (HOLDER, PERSONAL_ONLY, READER))
ENV = {"GRAPH_CLI_GRAPH": "example_graph"}
EXPLORE = {**ENV, "GRAPH_CLI_AGENT_ID": "agent-1", "GRAPH_CLI_AGENT_TYPE": "explore"}
READS_PEOPLE = """\
schema: graph_cli.policy/1
id: installation
grants:
  - entrypoint: app
    graphs: [example_graph]
    level: read
    reads: [stf:Employee, stf:Contractor]
    markings: [stf:personal, stf:pay]
no_project: {"*": write}
"""
NO_EXPLORE = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {graphs: "*", level: write}
no_project: {"*": write}
deny:
  - {id: no_explore_%s, agent_type: [explore], action: ["%s"]}
"""
HEAD = "schema: graph_cli.view/1\nname: %s\nmodule: staff\nsummary: %s\nuse_when: %s\n"
VIEWS = {
    "crew_overview": HEAD
    % ("crew_overview", "A crew and who works in it.", "Who works in a crew; the default of crews.")
    + """\
takes: stf:Crew
default: true
sections:
  - kind: fields
    fields: [name]
  - kind: neighbors
    id: members
    relation: stf:in_crew
    direction: in
    fields: [name]
""",
    "employee_overview": HEAD
    % ("employee_overview", "An employee with the pay.", "An employee with salary; a default.")
    + """\
takes: stf:Employee
default: true
sections:
  - kind: fields
    fields: [name, phone, salary]
""",
    "colleague_overview": HEAD
    % ("colleague_overview", "A colleague.", "Who a colleague is; the default of colleagues.")
    + """\
takes: stf:Colleague
default: true
sections:
  - kind: fields
    fields: [name, phone]
""",
    "colleague_mail": HEAD
    % ("colleague_mail", "A colleague's e-mail.", "The e-mail of a colleague, where it is known.")
    + """\
takes: stf:Colleague
sections:
  - kind: fields
    fields: [name, email]
""",
    "colleague_crew": HEAD
    % ("colleague_crew", "The crew of a colleague.", "Which crew a colleague works in.")
    + """\
takes: stf:Colleague
sections:
  - kind: neighbors
    id: crew
    relation: stf:in_crew
    fields: [name]
""",
    "staff_catalog": HEAD
    % ("staff_catalog", "Every employee, all values.", "Every employee for a renderer.")
    + """\
sections:
  - kind: list
    id: employees
    type: stf:Employee
    fields: all
    relations: all
""",
    "pay_roll": HEAD % ("pay_roll", "Employees with their salary.", "The pay of every employee.")
    + """\
sections:
  - kind: list
    id: pay
    type: stf:Employee
    fields: [name, salary]
""",
    "mail_rows": HEAD
    % ("mail_rows", "The private mail rows.", "The rows of the function that reads e-mail.")
    + """\
sections:
  - kind: rows
    function: employee_mail
""",
    "names_rows": HEAD
    % ("names_rows", "The names of colleagues.", "The rows of the function that lists names.")
    + """\
sections:
  - kind: rows
    function: colleague_names
""",
    "old_staff": HEAD
    % ("old_staff", "The old employee list.", "Superseded by the pay list; do not use.")
    + """\
status: deprecated
replaced_by: pay_roll
sections:
  - kind: list
    id: employees
    type: stf:Employee
    fields: [name]
""",
}
HIDDEN_FROM_READER = ("employee_overview", "pay_roll", "mail_rows")


def ok(run_cli: CliRunner, *args: str, env: dict[str, str] | None = None) -> Outcome:
    outcome = app(run_cli, *args, env=env or ENV)
    assert outcome.code == 0, (args, outcome.envelope)
    return outcome


def publish(run_cli: CliRunner, tmp_path: Path, kind: str, sources: dict[str, str]) -> None:
    folder = tmp_path / kind
    folder.mkdir()
    for name, text in sources.items():
        (folder / f"{name}.yaml").write_text(text)
    published = admin(run_cli, "graph", kind, "publish", "example_graph", str(folder))
    assert published.code == 0, published.envelope


@pytest.fixture
def world(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> Path:
    """The staff graph with its data, two functions and the views; the folder for policies."""
    root = init(run_cli, isolated_env)
    created = admin(run_cli, "graph", "create", "example_graph", "--ontology", str(STAFF / "1.0.0"))
    assert created.code == 0, created.envelope
    publish(run_cli, tmp_path, "functions", {n[:-5]: t for n, t in FUNCTION_SOURCES.items()})
    publish(run_cli, tmp_path, "views", VIEWS)
    write_documents(root, "example_graph", documents())
    return tmp_path / "policies"


def twin(run_cli: CliRunner, hidden: str, missing: str, *extra: str) -> None:
    """Asking for a hidden view answers as asking for one that was never published."""
    asked = shown(app(run_cli, "view", *extra, "--name", hidden, env=ENV))
    absent = shown(app(run_cli, "view", *extra, "--name", missing, env=ENV))
    assert asked["error"]["code"] == "view.not_found", asked
    the_same_but_for(without_suggestions(asked), without_suggestions(absent), (hidden, missing))


def named(run_cli: CliRunner, *names: str, env: dict[str, str] | None = None) -> list[str]:
    """Which of ``names`` a caller finds (a set view is called without an entity)."""
    found = []
    for name in names:
        extra = ["EMP-0001"] if VIEWS[name].count("takes:") else []
        outcome = app(run_cli, "view", *extra, "--name", name, env=env or ENV)
        if outcome.code == 0:
            found.append(name)
        else:
            assert outcome.error["code"] in ("view.not_found", "view.class_mismatch"), name
            if outcome.error["code"] == "view.class_mismatch":
                found.append(name)  # found, only not for this entity
    return found


# ---- hidden by a marking ---------------------------------------------------------------------


def test_a_view_that_names_a_marked_attribute_is_visible_to_who_holds_the_marking(
    run_cli: CliRunner, world: Path
) -> None:
    everything = list(VIEWS)
    install(run_cli, world, HOLDER)
    assert named(run_cli, *everything) == everything
    install(run_cli, world, PERSONAL_ONLY)  # the salary needs stf:pay as well
    assert set(everything) - set(named(run_cli, *everything)) == {"employee_overview", "pay_roll"}
    install(run_cli, world, READER)
    found = named(run_cli, *everything)
    assert set(everything) - set(found) == set(HIDDEN_FROM_READER)


def test_a_hidden_view_is_a_missing_one(run_cli: CliRunner, world: Path) -> None:
    install(run_cli, world, HOLDER)
    assert ok(run_cli, "view", "EMP-0001", "--name", "employee_overview").data["sections"]
    install(run_cli, world, READER)
    twin(run_cli, "pay_roll", "pay_rolls")
    twin(run_cli, "mail_rows", "mail_rowz")
    twin(run_cli, "employee_overview", "employee_overviews", "EMP-0001")
    missing = app(run_cli, "view", "--name", "nothing_like_it", env=ENV)
    assert missing.error["details"]["did_you_mean"] == []
    close = app(run_cli, "view", "--name", "pay_rol", env=ENV)  # suggests visible names only
    assert "pay_roll" not in json.dumps(close.envelope)
    near = json.dumps(app(run_cli, "view", "--name", "staf_catalog", env=ENV).envelope)
    assert "staff_catalog" in near  # a visible name is suggested
    assert [name for name in HIDDEN_FROM_READER if name in near] == []


def test_the_default_falls_back_up_the_class_chain_and_to_the_card(
    run_cli: CliRunner, world: Path
) -> None:
    install(run_cli, world, HOLDER)
    held = ok(run_cli, "view", "EMP-0001").data
    assert held["view"]["name"] == "employee_overview"
    assert [i["name"] for i in held["sections"][0]["items"]] == ["name", "phone", "salary"]
    install(run_cli, world, READER)
    reader = ok(run_cli, "view", "EMP-0001").data
    assert reader["view"]["name"] == "colleague_overview"  # the next default up the chain
    assert [i["name"] for i in reader["sections"][0]["items"]] == ["name", "phone"]
    assert ok(run_cli, "view", "CON-0001").data["view"]["name"] == "colleague_overview"
    assert ok(run_cli, "view", "CREW-0001").data["view"]["name"] == "crew_overview"
    card = ok(run_cli, "view", "BDG-0001").data  # a class without any default view
    assert card["view"] == {"name": "card", "origin": "generated"}
    for text in (json.dumps(held), json.dumps(reader)):
        assert "ada@private.example" not in text
    assert "91234.50" in json.dumps(held) and "91234.50" not in json.dumps(reader)


def test_all_fields_of_a_listing_leave_out_what_the_caller_may_not_see(
    run_cli: CliRunner, world: Path
) -> None:
    install(run_cli, world, HOLDER)
    held = ok(run_cli, "view", "--name", "staff_catalog")
    held_text = json.dumps(held.data)
    assert "91234.50" in held_text and "ada@new.example" in held_text
    install(run_cli, world, READER)
    reader = ok(run_cli, "view", "--name", "staff_catalog")
    rows = reader.data["sections"][0]["items"]
    assert [r["ref"] for r in rows] == ["EMP-0001", "EMP-0002", "EMP-0003", "EMP-0004"]
    assert all(set(r["fields"]) <= {"name", "phone", "shift", "stage"} for r in rows)
    assert leaks(reader.envelope) == []
    text = json.dumps(reader.envelope)
    assert [name for name in HIDDEN_NAMES if f'"{name}"' in text] == []
    assert {r["ref"] for r in rows if "shift" in r["fields"]} == {"EMP-0001", "EMP-0002"}


def test_an_attribute_masked_on_one_subclass_only_is_absent_there_as_for_get(
    run_cli: CliRunner, world: Path
) -> None:
    """``colleague_mail`` takes the abstract ``stf:Colleague`` and names ``email``, which the
    contractors declare unmarked and the employees marked: the view is visible, the employee
    shows nothing for it, exactly as ``get``."""
    install(run_cli, world, READER)
    contractor = ok(run_cli, "view", "CON-0001", "--name", "colleague_mail").data
    assert {i["name"]: i["value"] for i in contractor["sections"][0]["items"]} == {
        "name": "Dee",
        "email": "dee@agency.example",
    }
    employee = ok(run_cli, "view", "EMP-0001", "--name", "colleague_mail").data
    assert [i["name"] for i in employee["sections"][0]["items"]] == ["name"]
    assert "email" not in ok(run_cli, "get", "EMP-0001").data["entity"]["attrs"]
    assert leaks(employee) == []
    install(run_cli, world, HOLDER)
    held = ok(run_cli, "view", "EMP-0001", "--name", "colleague_mail").data
    assert [i["name"] for i in held["sections"][0]["items"]] == ["name", "email"]


# ---- hidden by a type limit -------------------------------------------------------------------


def test_a_type_limit_hides_the_views_that_name_a_hidden_class_or_relation(
    run_cli: CliRunner, world: Path
) -> None:
    install(run_cli, world, READS_PEOPLE)
    hidden = ("crew_overview", "colleague_crew")  # the crews and in_crew are gone
    found = named(run_cli, *VIEWS)
    assert set(VIEWS) - set(found) == set(hidden)
    twin(run_cli, "crew_overview", "crew_overviews", "CREW-0001")
    assert ok(run_cli, "view", "EMP-0001").data["view"]["name"] == "employee_overview"
    assert app(run_cli, "view", "CREW-0001", env=ENV).error["code"] == "entity.not_found"
    listing = ok(run_cli, "view", "--name", "staff_catalog")
    text = json.dumps(listing.envelope)
    assert "CREW-" not in text and "in_crew" not in text and "Core crew" not in text


# ---- hidden by what the view runs or needs ----------------------------------------------------


def test_a_view_that_runs_a_hidden_function_is_hidden(run_cli: CliRunner, world: Path) -> None:
    install(run_cli, world, READER)  # employee_mail reads a marked attribute: hidden
    assert "mail_rows" not in named(run_cli, "mail_rows", "names_rows")
    assert ok(run_cli, "view", "--name", "names_rows").data["sections"][0]["count"] == 6
    twin(run_cli, "mail_rows", "mail_rowz")
    install(run_cli, world, HOLDER)
    assert ok(run_cli, "view", "--name", "mail_rows").data["sections"][0]["count"] == 4


def test_a_view_that_runs_functions_needs_read_function(run_cli: CliRunner, world: Path) -> None:
    install(run_cli, world, NO_EXPLORE % ("functions", "read.function"))
    assert ok(run_cli, "view", "--name", "names_rows").code == 0
    explore = {**EXPLORE}
    assert named(run_cli, "names_rows", "staff_catalog", env=explore) == ["staff_catalog"]
    asked = shown(app(run_cli, "view", "--name", "names_rows", env=explore))
    absent = shown(app(run_cli, "view", "--name", "names_rowz", env=explore))
    assert asked["error"]["code"] == "view.not_found"
    the_same_but_for(
        without_suggestions(asked), without_suggestions(absent), ("names_rows", "names_rowz")
    )
    denied = app(run_cli, "run", "colleague_names", env=explore)  # the control: run is denied
    assert denied.error["code"] == "access.denied"


def test_a_set_view_needs_read_find_and_an_entity_view_does_not(
    run_cli: CliRunner, world: Path
) -> None:
    install(run_cli, world, NO_EXPLORE % ("find", "read.find"))
    assert ok(run_cli, "view", "--name", "staff_catalog").code == 0
    explore = {**EXPLORE}
    assert (
        app(run_cli, "find", "--type", "stf:Employee", env=explore).error["code"] == "access.denied"
    )
    for name in ("staff_catalog", "pay_roll", "names_rows"):
        outcome = app(run_cli, "view", "--name", name, env=explore)
        assert (outcome.code, outcome.error["code"]) == (1, "view.not_found"), name
    entity = ok(run_cli, "view", "CON-0001", "--name", "colleague_mail", env=explore)
    assert entity.data["view"]["name"] == "colleague_mail"
    assert ok(run_cli, "view", "CON-0001", env=explore).data["view"]["name"] == "colleague_overview"


def test_a_deprecated_view_never_names_a_replacement_the_caller_cannot_see(
    run_cli: CliRunner, world: Path
) -> None:
    install(run_cli, world, HOLDER)
    held = ok(run_cli, "view", "--name", "old_staff")
    [note] = [w for w in held.envelope["warnings"] if w["code"] == "view.deprecated"]
    assert note["details"] == {"view": "old_staff", "replaced_by": "pay_roll"}
    assert "use pay_roll" in note["message"]
    install(run_cli, world, READER)  # pay_roll is hidden: the name must not appear
    reader = ok(run_cli, "view", "--name", "old_staff")
    [note] = [w for w in reader.envelope["warnings"] if w["code"] == "view.deprecated"]
    assert note["details"] == {"view": "old_staff", "replaced_by": None}
    assert "pay_roll" not in json.dumps(reader.envelope)


# ---- rows, counts and the cursor -------------------------------------------------------------


def test_the_listings_and_counts_hold_only_what_the_caller_sees(
    run_cli: CliRunner, world: Path
) -> None:
    install(run_cli, world, READS_PEOPLE)
    crewless = ok(run_cli, "view", "--name", "staff_catalog").data
    assert crewless["sections"][0]["count"] == 4
    install(run_cli, world, READER)
    members = ok(run_cli, "view", "CREW-0001").data
    assert members["sections"][1]["count"] == 3  # Ada, Bo and Dee: all visible to this caller
    install(run_cli, world, READS_PEOPLE)
    assert app(run_cli, "view", "CREW-0001", env=ENV).error["code"] == "entity.not_found"


def test_a_cursor_does_not_survive_a_change_of_what_the_caller_sees(
    run_cli: CliRunner, world: Path
) -> None:
    install(run_cli, world, HOLDER)
    base: Sequence[str] = ("--name", "staff_catalog", "--page-bytes", "500")
    first = ok(run_cli, "view", *base).data
    assert first["more"] is True
    again = ok(run_cli, "view", *base, "--cursor", first["next"])
    assert again.code == 0
    install(run_cli, world, PERSONAL_ONLY)  # the same state of the graph, other eyes
    changed = app(run_cli, "view", *base, "--cursor", first["next"], env=ENV)
    assert (changed.code, changed.error["code"]) == (2, "view.cursor_invalid"), changed.envelope


# ---- a view that no longer fits ---------------------------------------------------------------


def test_a_stale_view_says_only_that_it_is_and_never_what_it_names(
    run_cli: CliRunner, isolated_env: dict[str, str], world: Path
) -> None:
    """``colleague_mail`` is visible to a caller without markings (the contractors' e-mail is
    unmarked); its stored form is changed to name the salary, as an interrupted release could
    leave it. The re-check runs on the full model, so its problems may name terms the caller
    cannot see: the answer carries the code only."""
    graph_id = next(Path(isolated_env["XDG_DATA_HOME"], "graph_cli", "graphs").iterdir()).name
    root = Path(isolated_env["XDG_DATA_HOME"]) / "graph_cli"

    def edit(entry: dict[str, Any]) -> None:
        entry["sections"][0]["fields"] = ["name", "salary"]

    digest = stale_on_disk(root, graph_id, "colleague_mail", edit)
    install(run_cli, world, READER)
    stale = app(run_cli, "view", "CON-0001", "--name", "colleague_mail", env=ENV)
    assert (stale.code, stale.error["code"]) == (1, "view.stale"), stale.envelope
    details = stale.error["details"]
    assert set(details) == {"view", "hash", "cause"} and details["view"] == "colleague_mail"
    assert details["hash"] == digest and details["cause"].startswith("view.")
    assert "tell the maintainer" in stale.error["message"]
    text = json.dumps(stale.envelope)
    assert "salary" not in text and "pay" not in text.replace("payload", "")
    # the other views of the catalog are fine
    assert ok(run_cli, "view", "CON-0001", "--name", "colleague_overview").code == 0
