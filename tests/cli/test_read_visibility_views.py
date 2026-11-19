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
    FUNCTION,
    FUNCTION_SOURCES,
    HIDDEN_NAMES,
    HOLDER,
    NAME_ONLY,
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


def test_a_stale_view_is_stale_for_who_sees_everything_and_missing_for_a_limited_reader(
    run_cli: CliRunner, isolated_env: dict[str, str], world: Path
) -> None:
    """``colleague_mail`` is visible to a caller without markings (the contractors' e-mail is
    unmarked); its stored form is changed to name the salary, as an interrupted release could
    leave it. A caller who sees everything gets ``view.stale``, the code of the cause only; a
    limited caller gets what a name that was never published gets: the problems of the check may
    name terms it cannot see, so the view is hidden, and the default falls back to the card."""
    graph_id = next(Path(isolated_env["XDG_DATA_HOME"], "graph_cli", "graphs").iterdir()).name
    root = Path(isolated_env["XDG_DATA_HOME"]) / "graph_cli"

    def edit(entry: dict[str, Any]) -> None:
        entry["sections"][0]["fields"] = ["name", "salary"]

    digest = stale_on_disk(root, graph_id, "colleague_mail", edit)
    install(run_cli, world, HOLDER)
    stale = app(run_cli, "view", "CON-0001", "--name", "colleague_mail", env=ENV)
    assert (stale.code, stale.error["code"]) == (1, "view.stale"), stale.envelope
    details = stale.error["details"]
    assert set(details) == {"view", "hash", "cause"} and details["view"] == "colleague_mail"
    assert details["hash"] == digest and details["cause"].startswith("view.")
    assert "tell the maintainer" in stale.error["message"]
    install(run_cli, world, READER)
    twin(run_cli, "colleague_mail", "colleague_mailx", "CON-0001")
    described = app(run_cli, "describe", "view", "colleague_mail", env=ENV)
    assert (described.code, described.error["code"]) == (1, "view.not_found")
    assert "salary" not in json.dumps(described.envelope)
    assert "colleague_mail" not in listed(run_cli)
    # the other views of the catalog are fine
    assert ok(run_cli, "view", "CON-0001", "--name", "colleague_overview").code == 0


# ---- what a deprecation names ------------------------------------------------------------------


def test_a_deprecated_function_never_names_a_replacement_the_caller_cannot_see(
    run_cli: CliRunner, world: Path, tmp_path: Path
) -> None:
    """``old_mail`` (names only) is replaced by ``employee_mail``, which a reader without the
    markings does not see: neither ``graph run`` nor a view that runs it names the replacement."""
    functions = tmp_path / "functions_more"
    functions.mkdir()
    for name, text in FUNCTION_SOURCES.items():
        (functions / name).write_text(text)
    (functions / "old_mail.yaml").write_text(
        FUNCTION.format(
            name="old_mail",
            summary="The old list of names.",
            params="{}",
            query="MATCH (e:Employee) RETURN e.name AS name ORDER BY name",
            returns=NAME_ONLY,
        )
        + "status: deprecated\nreplaced_by: employee_mail\n"
    )
    views = tmp_path / "views_more"
    views.mkdir()
    for name, text in VIEWS.items():
        (views / f"{name}.yaml").write_text(text)
    (views / "old_rows.yaml").write_text(
        HEAD % ("old_rows", "The old names.", "The rows of the deprecated function.")
        + "sections:\n  - kind: rows\n    function: old_mail\n"
    )
    for kind, folder in (("functions", functions), ("views", views)):
        published = admin(run_cli, "graph", kind, "publish", "example_graph", str(folder))
        assert published.code == 0, published.envelope

    def warned(*args: str) -> dict[str, Any]:
        found = ok(run_cli, *args).envelope["warnings"]
        [note] = [
            w for w in found if w["code"] in ("function.deprecated", "view.function_deprecated")
        ]
        assert isinstance(note, dict)
        return note

    install(run_cli, world, HOLDER)
    assert warned("run", "old_mail")["details"] == {
        "name": "old_mail",
        "replaced_by": "employee_mail",
    }
    assert warned("view", "--name", "old_rows")["details"]["replaced_by"] == "employee_mail"
    install(run_cli, world, READER)
    run_note = warned("run", "old_mail")
    assert run_note["details"] == {"name": "old_mail", "replaced_by": None}
    assert "employee_mail" not in json.dumps(run_note) and "use " not in run_note["message"]
    view_note = warned("view", "--name", "old_rows")
    assert view_note["details"]["replaced_by"] is None
    assert "employee_mail" not in json.dumps(view_note)
    described = ok(run_cli, "describe", "function", "old_mail").data["function"]
    assert described["status"] == "deprecated" and described["replaced_by"] is None
    install(run_cli, world, HOLDER)
    assert ok(run_cli, "describe", "function", "old_mail").data["function"]["replaced_by"] == (
        "employee_mail"
    )


# ---- discovery: describe, search and the affordance --------------------------------------------


def listed(run_cli: CliRunner, env: dict[str, str] | None = None) -> list[str]:
    return [v["name"] for v in ok(run_cli, "describe", "views", env=env).data["views"]]


def test_describe_views_lists_what_the_caller_can_call_and_nothing_else(
    run_cli: CliRunner, world: Path
) -> None:
    install(run_cli, world, HOLDER)
    everything = sorted(set(VIEWS) - {"old_staff"})
    assert listed(run_cli) == everything
    install(run_cli, world, READER)
    assert listed(run_cli) == [n for n in everything if n not in HIDDEN_FROM_READER]
    assert listed(run_cli) == sorted(  # the same set `graph view --name` finds
        n for n in named(run_cli, *VIEWS) if n != "old_staff"
    )
    text = run_cli("graph", "describe", "views", env=ENV).stdout
    assert [n for n in HIDDEN_FROM_READER if n in text] == []
    install(run_cli, world, READS_PEOPLE)  # no crews: the views that name them go
    assert not {"crew_overview", "colleague_crew"} & set(listed(run_cli))


def test_a_hidden_view_cannot_be_described_and_is_not_suggested(
    run_cli: CliRunner, world: Path
) -> None:
    install(run_cli, world, READER)
    asked = shown(app(run_cli, "describe", "view", "pay_roll", env=ENV))
    absent = shown(app(run_cli, "describe", "view", "pay_rolls", env=ENV))
    assert asked["error"]["code"] == "view.not_found"
    the_same_but_for(
        without_suggestions(asked), without_suggestions(absent), ("pay_roll", "pay_rolls")
    )
    assert "employee_overview" not in json.dumps(asked["error"])
    visible = ok(run_cli, "describe", "view", "staff_catalog").data["view"]
    assert visible["takes"] is None and visible["touches"]["functions"] == []
    text = run_cli("graph", "describe", "view", "staff_catalog", env=ENV).stdout
    assert "  employees  list  stf:Employee\n    fields: all\n    relations: all\n" in text
    install(run_cli, world, HOLDER)
    assert ok(run_cli, "describe", "view", "pay_roll").data["view"]["name"] == "pay_roll"


def test_search_never_finds_a_hidden_view_by_any_of_its_words(
    run_cli: CliRunner, world: Path
) -> None:
    install(run_cli, world, READER)

    def found(text: str) -> list[str]:
        items = ok(run_cli, "describe", "search", text, "--kind", "view").data["items"]
        return [i["curie"] for i in items]

    for text in (
        "pay roll",
        "pay_roll",
        "salary of every employee",
        "pay of every employee",
        "private mail rows",
        "employee mail",
        "employee overview",
        "e-mail of a colleague",
    ):
        assert not set(found(text)) & set(HIDDEN_FROM_READER), text
    assert "colleague_mail" in found("e-mail of a colleague")
    everything = json.dumps(ok(run_cli, "describe", "search", "employee").data)
    assert [n for n in HIDDEN_FROM_READER if f'"{n}"' in everything] == []
    install(run_cli, world, HOLDER)
    assert "pay_roll" in found("pay of every employee")
    assert "mail_rows" in found("private mail rows")


def test_the_affordance_names_the_other_views_the_caller_sees(
    run_cli: CliRunner, world: Path
) -> None:
    def affordance(ref: str) -> list[str]:
        found = ok(run_cli, "get", ref, "--affordances").data["affordances"]
        return [v["view"] for v in found.get("views", [])]

    install(run_cli, world, HOLDER)
    # an employee: the default of its own class is what `view` shows; the default of the
    # superclass is another view of it and is named
    assert affordance("EMP-0001") == ["colleague_crew", "colleague_mail", "colleague_overview"]
    install(run_cli, world, READER)
    assert affordance("EMP-0001") == ["colleague_crew", "colleague_mail"]
    assert affordance("CON-0001") == ["colleague_crew", "colleague_mail"]
    assert affordance("BDG-0001") == []
    card = ok(run_cli, "view", "EMP-0001", "--name", "card").data
    section = next(s for s in card["sections"] if s["kind"] == "affordances")
    assert section["views"] == ["colleague_crew", "colleague_mail"]
    assert "employee_overview" not in json.dumps(card)
    install(run_cli, world, READS_PEOPLE)  # the crews are gone, and the view that reads them
    assert "colleague_crew" not in affordance("EMP-0001")


def test_a_caller_without_read_function_or_read_find_is_not_told_about_those_views(
    run_cli: CliRunner, world: Path
) -> None:
    install(run_cli, world, NO_EXPLORE % ("functions", "read.function"))
    explore = {**EXPLORE}
    assert "names_rows" in listed(run_cli) and "names_rows" not in listed(run_cli, explore)
    assert "staff_catalog" in listed(run_cli, explore)
    asked = app(run_cli, "describe", "view", "names_rows", env=explore)
    assert (asked.code, asked.error["code"]) == (1, "view.not_found")
    install(run_cli, world, NO_EXPLORE % ("find", "read.find"))
    without_find = listed(run_cli, explore)
    assert "staff_catalog" not in without_find and "pay_roll" not in without_find
    assert "colleague_mail" in without_find  # entity views need read.get only
    hits = ok(run_cli, "describe", "search", "staff", "--kind", "view", env=explore).data["items"]
    assert [i["curie"] for i in hits] == []
