"""S: functions, actions, describe and the graph skill for a caller who lacks a marking (field
visibility plan F6, F7, leak catalogue items 6 and 7).

The ``staff`` graph holds a published function catalog and action catalog
(``support.markings``): ``set_phone`` and ``colleague_names`` touch nothing marked, the others
set or read a marked attribute, check one in a precondition or take an enum only a marked
attribute uses. Without an installation policy the app caller holds no marking. A function or
action it cannot see answers exactly as one that was never published; ``describe`` shows
markings only to a caller who sees the attribute; the skill installed for a caller names no
hidden attribute, and a grant of markings makes it stale.
"""

import json
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init, write_documents
from support.app import app
from support.fixtures import FIXTURES
from support.markings import (
    ACTION_SOURCES,
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

from graph_cli.kernel.read_view import ReadView
from graph_cli.kernel.skill_marker import MARKER_FILE
from graph_cli.skills.tree import read_tree

HOLDER, PERSONAL_ONLY, READER = (without_twin(p) for p in (HOLDER, PERSONAL_ONLY, READER))
ENV = {"GRAPH_CLI_GRAPH": "example_graph"}
POINTER = 'format_version = 1\ngraphs = ["example_graph"]\n'
SKILL = FIXTURES / "skills" / "blocks" / "graph-example-graph"
E1 = "EMP-0001"
HIDDEN = ("set_email", "set_salary", "set_grade", "set_phone_badged", "employee_mail", "by_grade")
READS_CREW = """\
schema: graph_cli.policy/1
id: installation
grants:
  - entrypoint: app
    graphs: [example_graph]
    level: read
    reads: [stf:Employee, stf:Crew]
    markings: [stf:personal, stf:pay]
no_project: {"*": write}
"""


def ok(run_cli: CliRunner, *args: str, cwd: Path | None = None) -> Outcome:
    outcome = app(run_cli, *args, env=ENV, cwd=cwd)
    assert outcome.code == 0, (args, outcome.envelope)
    return outcome


def names(outcome: Outcome, key: str) -> list[str]:
    return [i["name"] for i in outcome.data[key]]


def twin(
    run_cli: CliRunner,
    build: Callable[[str], Sequence[str]],
    hidden: str,
    missing: str,
    code: str,
) -> None:
    """The answer about a hidden name equals the answer about one that is missing, but for the
    name (and the suggestions, which follow the spelling)."""
    asked = shown(app(run_cli, *build(hidden), env=ENV))
    absent = shown(app(run_cli, *build(missing), env=ENV))
    assert asked["error"]["code"] == code, asked
    the_same_but_for(without_suggestions(asked), without_suggestions(absent), (hidden, missing))


@pytest.fixture
def world(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> Path:
    """The staff graph with its data and both catalogs; the folder for policies."""
    root = init(run_cli, isolated_env)
    created = admin(run_cli, "graph", "create", "example_graph", "--ontology", str(STAFF / "1.0.0"))
    assert created.code == 0, created.envelope
    for kind, sources in (("functions", FUNCTION_SOURCES), ("actions", ACTION_SOURCES)):
        folder = tmp_path / kind
        folder.mkdir()
        for file, text in sources.items():
            (folder / file).write_text(text)
        published = admin(run_cli, "graph", kind, "publish", "example_graph", str(folder))
        assert published.code == 0, published.envelope
    write_documents(root, "example_graph", documents())
    return tmp_path / "policies"


# ---- describe ------------------------------------------------------------------------------


def test_describe_lists_only_the_functions_and_actions_that_fit(
    run_cli: CliRunner, world: Path
) -> None:
    install(run_cli, world, READER)  # a caller that writes but holds no marking
    assert names(ok(run_cli, "describe", "actions"), "actions") == ["set_phone"]
    assert names(ok(run_cli, "describe", "functions"), "functions") == ["colleague_names"]
    modules = ok(run_cli, "describe", "modules").data["modules"]
    assert [(m["functions"], m["actions"]) for m in modules if m["id"] == "staff"] == [(1, 1)]
    module = ok(run_cli, "describe", "module", "staff").data["module"]
    assert [a["name"] for a in module["actions"]] == ["set_phone"]
    for command in (("describe", "functions"), ("describe", "actions"), ("describe", "modules")):
        text = json.dumps(ok(run_cli, *command).envelope)
        assert [name for name in HIDDEN if name in text] == [], command
        assert leaks(ok(run_cli, *command).envelope) == [], command
    install(run_cli, world, PERSONAL_ONLY)  # the salary also needs stf:pay
    assert names(ok(run_cli, "describe", "actions"), "actions") == [
        "set_email",
        "set_grade",
        "set_phone",
        "set_phone_badged",
    ]
    assert len(ok(run_cli, "describe", "functions").data["functions"]) == 3
    install(run_cli, world, HOLDER)
    assert len(ok(run_cli, "describe", "actions").data["actions"]) == 5
    plain = run_cli("graph", "describe", "actions", env=ENV)
    assert plain.returncode == 0 and "set_salary" in plain.stdout


def test_describing_a_hidden_function_or_action_is_describing_a_missing_one(
    run_cli: CliRunner, world: Path
) -> None:
    install(run_cli, world, HOLDER)
    assert "set_email" in run_cli("graph", "describe", "action", "set_email", env=ENV).stdout
    install(run_cli, world, READER)
    twin(
        run_cli, lambda n: ("describe", "action", n), "set_email", "set_nothing", "action.not_found"
    )
    twin(
        run_cli,
        lambda n: ("describe", "function", n),
        "employee_mail",
        "employee_nowhere",
        "function.not_found",
    )
    twin(
        run_cli,
        lambda n: ("describe", "function", n),
        "by_grade",
        "by_nothing",
        "function.not_found",
    )
    typo = app(run_cli, "describe", "action", "set_emai", env=ENV)
    assert "set_email" not in typo.error["details"]["did_you_mean"]
    detail = ok(run_cli, "describe", "action", "set_phone")  # a visible one is shown
    assert detail.data["action"]["name"] == "set_phone"


def test_describe_search_finds_no_hidden_function_or_action(
    run_cli: CliRunner, world: Path
) -> None:
    install(run_cli, world, HOLDER)
    control = ok(run_cli, "describe", "search", "e-mail")
    assert {"set_email", "employee_mail"} <= {i["curie"] for i in control.data["items"]}
    install(run_cli, world, READER)
    for text in ("e-mail", "email", "salary", "grade", "badge", "private"):
        found = ok(run_cli, "describe", "search", text)
        for name in (*HIDDEN, "stf:personal"):
            assert name not in json.dumps(found.envelope), (text, name)


def test_describe_shows_markings_to_a_caller_who_sees_the_attribute(
    run_cli: CliRunner, world: Path
) -> None:
    def fields(command: str) -> dict[str, dict[str, Any]]:
        data = ok(run_cli, "describe", command, "stf:Employee").data
        found = data["type"]["fields"] if command == "type" else data["fields"]
        return {f["name"]: f for f in found}

    install(run_cli, world, READER)
    for command in ("type", "fields"):
        assert set(fields(command)) == {"name", "phone", "shift", "stage"}
        assert all("markings" not in f for f in fields(command).values()), command
    install(run_cli, world, PERSONAL_ONLY)
    for command in ("type", "fields"):
        found = fields(command)
        assert found["email"]["markings"] == ["stf:personal"] and "salary" not in found
        assert "markings" not in found["phone"]
    install(run_cli, world, HOLDER)
    for command in ("type", "fields"):
        assert fields(command)["salary"]["markings"] == ["stf:pay", "stf:personal"]


# ---- run and do ----------------------------------------------------------------------------


def test_running_a_hidden_function_is_running_a_missing_one(
    run_cli: CliRunner, world: Path
) -> None:
    install(run_cli, world, HOLDER)
    assert len(ok(run_cli, "run", "employee_mail").data["items"]) == 4  # the employees
    install(run_cli, world, READER)
    for params, hidden, missing in (
        ((), "employee_mail", "employee_nowhere"),
        (("--param", "grade=senior"), "by_grade", "by_nothing"),
        (("--param", "other=1"), "employee_mail", "employee_nowhere"),
    ):
        twin(
            run_cli,
            lambda n, params=params: ("run", n, *params),
            hidden,
            missing,
            "function.not_found",
        )
    typo = app(run_cli, "run", "employee_mai", env=ENV)
    assert "employee_mail" not in typo.error["details"]["did_you_mean"]
    names_only = ok(run_cli, "run", "colleague_names")  # a visible one runs over the view
    assert len(names_only.data["items"]) == 6  # employees and contractors


def test_doing_a_hidden_action_is_doing_a_missing_one(run_cli: CliRunner, world: Path) -> None:
    install(run_cli, world, READER)
    for params in (
        (),
        (f"who={E1}", "value=a@b.example"),
        (f"who={E1}", "value=senior"),
    ):
        for hidden, missing in (
            ("set_email", "set_nothing"),
            ("set_salary", "set_nothing"),
            ("set_grade", "set_nothing"),
            ("set_phone_badged", "set_nothing"),
        ):
            for extra in ((), ("--dry-run",)):
                twin(
                    run_cli,
                    lambda n, params=params, extra=extra: (
                        "do",
                        n,
                        *(a for p in params for a in ("--param", p)),
                        *extra,
                    ),
                    hidden,
                    missing,
                    "action.not_found",
                )
    done = ok(
        run_cli, "do", "set_phone", "--param", f"who={E1}", "--param", "value=+1 555 0", "--dry-run"
    )
    assert done.data["action"]["name"] == "set_phone"
    # the visible action changes the phone; the hidden attributes stay as they were
    ok(run_cli, "do", "set_phone", "--param", f"who={E1}", "--param", "value=+1 555 0")
    install(run_cli, world, HOLDER)
    assert ok(run_cli, "get", E1).data["entity"]["attrs"]["email"] == "ada@new.example"
    assert ok(run_cli, "get", E1).data["entity"]["attrs"]["phone"] == "+1 555 0"


def test_a_partial_holder_runs_what_it_sees_and_nothing_else(
    run_cli: CliRunner, world: Path
) -> None:
    install(run_cli, world, PERSONAL_ONLY)
    done = ok(run_cli, "do", "set_email", "--param", f"who={E1}", "--param", "value=a@b.example")
    assert done.data["action"]["name"] == "set_email"
    twin(
        run_cli,
        lambda n: ("do", n, "--param", f"who={E1}", "--param", "value=1"),
        "set_salary",
        "set_nothing",
        "action.not_found",
    )


def test_affordances_offer_no_hidden_action(run_cli: CliRunner, world: Path) -> None:
    install(run_cli, world, HOLDER)
    full = ok(run_cli, "get", E1, "--affordances").data["affordances"]
    assert {a["action"] for a in full["actions"]} == {
        "set_email",
        "set_grade",
        "set_phone",
        "set_phone_badged",
        "set_salary",
    }
    install(run_cli, world, READER)
    limited = ok(run_cli, "get", E1, "--affordances").data["affordances"]
    assert [a["action"] for a in limited["actions"]] == ["set_phone"]
    for name in HIDDEN:
        assert name not in json.dumps(limited), name


# ---- the skill -----------------------------------------------------------------------------


def publish_skill(run_cli: CliRunner, tmp_path: Path) -> None:
    folder = tmp_path / "skill" / "graph-example-graph"
    shutil.rmtree(folder.parent, ignore_errors=True)
    shutil.copytree(SKILL, folder)
    published = admin(run_cli, "graph", "skill", "publish", "example_graph", str(folder))
    assert published.code == 0, published.envelope


def project_with_pointer(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".graph_cli.toml").write_text(POINTER)
    return project


def install_skills(run_cli: CliRunner, project: Path) -> Outcome:
    args = ("skills", "install", "--scope", "project", "--dir", str(project), "--graph")
    return app(run_cli, *args, "example_graph", cwd=project)


def installed_text(project: Path) -> str:
    files, problems = read_tree(project / ".claude" / "skills" / "graph-example-graph")
    assert problems == []
    return "".join(data.decode() for path, data in sorted(files.items()) if path != MARKER_FILE)


def seen(project: Path) -> str | None:
    marker = project / ".claude" / "skills" / "graph-example-graph" / MARKER_FILE
    source: dict[str, Any] = json.loads(marker.read_text())["source"]
    return source.get("visible")


def test_the_skill_of_a_caller_without_markings_names_no_hidden_attribute(
    run_cli: CliRunner, world: Path, tmp_path: Path
) -> None:
    publish_skill(run_cli, tmp_path)
    project = project_with_pointer(tmp_path)
    install(run_cli, world, READER)
    installed = install_skills(run_cli, project)
    assert installed.code == 0, installed.envelope
    text = installed_text(project)
    for name in (*HIDDEN, "marked", "stf:personal", "stf:pay", *HIDDEN_NAMES):
        if name != "email":  # a contractor's e-mail is visible
            assert name not in text, name
    assert "`phone`" in text and "set_phone" in text and "`email`" in text
    assert seen(project) is not None
    install(run_cli, world, HOLDER)
    refreshed = install_skills(run_cli, project)
    assert refreshed.code == 0, refreshed.envelope
    text = installed_text(project)
    assert "`salary` (decimal, marked stf:pay+stf:personal)" in text
    assert "`email` (marked stf:personal)" in text and "set_salary" in text
    assert seen(project) is None  # holds every marking: nothing limits it, as before


def test_a_marking_grant_makes_the_installed_skill_stale(
    run_cli: CliRunner, world: Path, tmp_path: Path
) -> None:
    publish_skill(run_cli, tmp_path)
    project = project_with_pointer(tmp_path)
    install(run_cli, world, READER)
    assert install_skills(run_cli, project).code == 0
    first = seen(project)
    fresh = ok(run_cli, "find", "--type", "stf:Crew", cwd=project)
    assert "skills.stale" not in fresh.warning_codes
    install(run_cli, world, PERSONAL_ONLY)  # the policy now grants stf:personal
    stale = ok(run_cli, "find", "--type", "stf:Crew", cwd=project)
    assert "skills.stale" in stale.warning_codes
    entry = next(w for w in stale.warnings if w["code"] == "skills.stale")
    (skill,) = entry["details"]["skills"]
    assert skill["installed"]["visible"] == first and skill["current"]["visible"] != first
    assert install_skills(run_cli, project).code == 0
    second = seen(project)
    assert second not in (None, first)
    assert (
        "skills.stale" not in ok(run_cli, "find", "--type", "stf:Crew", cwd=project).warning_codes
    )
    install(run_cli, world, HOLDER)  # every marking: the claim carries no digest any more
    holder = ok(run_cli, "find", "--type", "stf:Crew", cwd=project)
    assert "skills.stale" in holder.warning_codes
    entry = next(w for w in holder.warnings if w["code"] == "skills.stale")
    assert entry["details"]["skills"][0]["current"]["visible"] is None
    # the readers who gained nothing keep a fresh copy: a grant to another graph changes no claim
    install(run_cli, world, PERSONAL_ONLY)
    assert install_skills(run_cli, project).code == 0
    assert seen(project) == second


def test_the_claim_of_a_type_limited_reader_is_the_digest_of_its_types(
    run_cli: CliRunner, world: Path, tmp_path: Path
) -> None:
    """Stage 3 claims stay as they were: a reader limited by types alone, who holds every
    marking, carries the digest of the visible types, also for a graph that uses markings."""
    publish_skill(run_cli, tmp_path)
    project = project_with_pointer(tmp_path)
    install(run_cli, world, READS_CREW)
    installed = install_skills(run_cli, project)
    assert installed.code == 0, installed.envelope
    digest = ReadView.of(frozenset({"stf:Employee", "stf:Crew"})).digest
    assert digest is not None and seen(project) == digest
    assert (
        "skills.stale" not in ok(run_cli, "find", "--type", "stf:Crew", cwd=project).warning_codes
    )
    text = installed_text(project)
    assert "marked stf:personal" in text and "`stf:Badge`" not in text
