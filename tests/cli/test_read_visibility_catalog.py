"""S: functions, actions and the graph skill under a type read limit (type read limits plan V7).

The inventory graph holds items, locations and categories, a published function catalog and a
published action catalog. The app caller reads items only. Every surface that lists, describes,
suggests or runs a function or action answers for one that names a hidden term exactly as for
a name that was never published, and the skill installed for the caller names no hidden term.
"""

import json
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, data_root, init, write_documents
from support.app import app, create_graph
from support.fixtures import FIXTURES
from support.read_limits import install, shown, the_same_but_for, without_suggestions

from graph_cli.kernel.skill_marker import MARKER_FILE
from graph_cli.skills.tree import read_tree

ENV = {"GRAPH_CLI_GRAPH": "example_graph"}
POINTER = 'format_version = 1\ngraphs = ["example_graph"]\n'
READS_ITEMS = """\
schema: graph_cli.policy/1
id: installation
grants:
  - entrypoint: app
    graphs: [example_graph]
    level: read
    reads: [{types}]
{writes}no_project: {{"*": write}}
"""
WRITES_ITEMS = "    writes:\n      inv:Item: {transition: true, link: true, unlink: true}\n"
FUNCTIONS = FIXTURES / "functions" / "inventory" / "valid"
ACTIONS = FIXTURES / "actions" / "inventory" / "valid"
SKILL = FIXTURES / "skills" / "inventory_limited" / "graph-example-graph"
HIDDEN = ("Location", "located_in", "Category", "in_category", "items_in_location", "move_item")
COUNT_THINGS = """\
schema: graph_cli.function/1
name: count_things
summary: Number of entities of every kind.
use_when: How many things the graph holds, whatever their type.
params: {}
query: |
  MATCH (n) RETURN count(n) AS things
returns:
  - column: things
    summary: Number of entities.
examples:
  - question: How many things are there?
"""
GONE = "0192f3c0-11aa-7e01-b2c3-d4e5f6a7b8c9"


def ok(run_cli: CliRunner, *args: str, cwd: Path | None = None) -> Outcome:
    outcome = app(run_cli, *args, env=ENV, cwd=cwd)
    assert outcome.code == 0, (args, outcome.envelope)
    return outcome


def limit(run_cli: CliRunner, tmp_path: Path, *types: str, writes: str = "") -> None:
    install(
        run_cli, tmp_path / "policies", READS_ITEMS.format(types=", ".join(types), writes=writes)
    )


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


def names(outcome: Outcome, key: str) -> list[str]:
    return [i["name"] for i in outcome.data[key]]


@pytest.fixture
def world(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> dict[str, str]:
    """The graph with its catalogs; the ids of a location, a category and an item."""
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "inventory")
    functions = tmp_path / "functions"
    shutil.copytree(FUNCTIONS, functions)
    (functions / "count_things.yaml").write_text(COUNT_THINGS)
    published = admin(run_cli, "graph", "functions", "publish", "example_graph", str(functions))
    assert published.code == 0, published.envelope
    published = admin(run_cli, "graph", "actions", "publish", "example_graph", str(ACTIONS))
    assert published.code == 0, published.envelope
    document = {
        "ops": [
            {
                "op": "create",
                "type": "inv:Location",
                "local": "shelf",
                "attrs": {"name": "Shelf", "code": "S1"},
            },
            {"op": "create", "type": "inv:Category", "local": "books", "attrs": {"name": "Books"}},
            {
                "op": "create",
                "type": "inv:Item",
                "local": "chair",
                "attrs": {"name": "Chair", "condition": "damaged"},
                "edges": [
                    {"rel": "inv:located_in", "target": "@shelf"},
                    {"rel": "inv:in_category", "target": "@books"},
                ],
            },
        ]
    }
    write_documents(root, "example_graph", [document])
    ids = {}
    for kind, local in (("Location", "shelf"), ("Category", "books"), ("Item", "chair")):
        found = ok(run_cli, "find", "--type", f"inv:{kind}").data["items"]
        ids[local] = found[0]["id"]
    return ids


# ---- describe ------------------------------------------------------------------------------


def test_describe_lists_only_the_functions_and_actions_that_fit(
    run_cli: CliRunner, world: dict[str, str], tmp_path: Path
) -> None:
    full = ok(run_cli, "describe", "functions")
    assert names(full, "functions") == ["count_things", "items_by_condition", "items_in_location"]
    assert names(ok(run_cli, "describe", "actions"), "actions") == ["move_item", "retire_item"]
    modules = ok(run_cli, "describe", "modules").data["modules"]
    assert [(m["functions"], m["actions"]) for m in modules if m["id"] == "inventory"] == [(3, 2)]
    limit(run_cli, tmp_path, "inv:Item", writes=WRITES_ITEMS)
    limited = ok(run_cli, "describe", "functions")
    # a function over unlabelled nodes names no hidden term and stays
    assert names(limited, "functions") == ["count_things", "items_by_condition"]
    assert names(ok(run_cli, "describe", "actions"), "actions") == ["retire_item"]
    modules = ok(run_cli, "describe", "modules").data["modules"]
    assert [(m["functions"], m["actions"]) for m in modules if m["id"] == "inventory"] == [(2, 1)]
    module = ok(run_cli, "describe", "module", "inventory").data["module"]
    assert [f["name"] for f in module["functions"]] == ["count_things", "items_by_condition"]
    assert [a["name"] for a in module["actions"]] == ["retire_item"]
    for command in (("describe", "functions"), ("describe", "actions"), ("describe", "modules")):
        text = json.dumps(ok(run_cli, *command).envelope)
        assert [name for name in HIDDEN if name in text] == [], command
    # the text forms too
    plain = run_cli("graph", "describe", "functions", env=ENV)
    assert plain.returncode == 0 and "items_in_location" not in plain.stdout
    assert "items_by_condition" in plain.stdout


def test_describing_a_hidden_function_or_action_is_describing_a_missing_one(
    run_cli: CliRunner, world: dict[str, str], tmp_path: Path
) -> None:
    unlimited = run_cli("graph", "describe", "function", "items_in_location", env=ENV)
    assert unlimited.returncode == 0 and "items_in_location" in unlimited.stdout
    limit(run_cli, tmp_path, "inv:Item", writes=WRITES_ITEMS)
    twin(
        run_cli,
        lambda n: ("describe", "function", n),
        "items_in_location",
        "items_in_nowhere",
        "function.not_found",
    )
    twin(
        run_cli,
        lambda n: ("describe", "action", n),
        "move_item",
        "move_nothing",
        "action.not_found",
    )
    detail = ok(run_cli, "describe", "function", "items_by_condition")  # a visible one is shown
    assert detail.data["function"]["touches"]["types"] == [
        {"curie": "inv:Item", "module": "inventory"}
    ]
    typo = app(run_cli, "describe", "function", "items_in_locatio", env=ENV)
    assert "items_in_location" not in typo.error["details"]["did_you_mean"]


def test_search_finds_no_hidden_function_or_action(
    run_cli: CliRunner, world: dict[str, str], tmp_path: Path
) -> None:
    control = ok(run_cli, "describe", "search", "location", "--kind", "function")
    assert "items_in_location" in {i["curie"] for i in control.data["items"]}
    moving = ok(run_cli, "describe", "search", "move", "--kind", "action")
    assert "move_item" in {i["curie"] for i in moving.data["items"]}
    limit(run_cli, tmp_path, "inv:Item", writes=WRITES_ITEMS)
    for text in ("location", "move", "stored", "put away", "category"):
        found = ok(run_cli, "describe", "search", text)
        for name in HIDDEN:
            assert name not in json.dumps(found.envelope), (text, name)
    page = ok(run_cli, "describe", "search", "item", "--limit", "100")
    assert page.page is not None and page.page["total"] == len(page.data["items"])


# ---- run -----------------------------------------------------------------------------------


def test_running_a_hidden_function_is_running_a_missing_one(
    run_cli: CliRunner, world: dict[str, str], tmp_path: Path
) -> None:
    ids = world
    asked = ok(run_cli, "run", "items_in_location", "--param", f"location={ids['shelf']}")
    assert [r["item"] for r in asked.data["items"]] == ["Chair"]
    limit(run_cli, tmp_path, "inv:Item")
    for params in ((), ("--param", f"location={ids['shelf']}"), ("--param", "other=1")):
        twin(
            run_cli,
            lambda n, params=params: ("run", n, *params),
            "items_in_location",
            "items_in_nowhere",
            "function.not_found",
        )
    typo = app(run_cli, "run", "items_in_locatio", env=ENV)
    assert "items_in_location" not in typo.error["details"]["did_you_mean"]
    # the visible functions run over the view
    damaged = ok(run_cli, "run", "items_by_condition", "--param", "condition=damaged")
    assert [r["item"] for r in damaged.data["items"]] == ["Chair"]
    # a function without a label reaches every type; what it counts is what the caller sees
    assert ok(run_cli, "run", "count_things").data["items"] == [{"things": 1}]


def test_the_cover_hints_name_no_hidden_function(
    run_cli: CliRunner, world: dict[str, str], tmp_path: Path
) -> None:
    ids = world
    covered = (
        f"MATCH (i:Item)-[:located_in]->(l:Location) WHERE l.id = '{ids['shelf']}' "
        "RETURN i.name AS item"
    )
    plain = ok(run_cli, "query", covered)
    assert any("items_in_location" in h["text"] for h in plain.envelope.get("hints", []))
    limit(run_cli, tmp_path, "inv:Item")
    # the query names a hidden type: unknown, with no hint to the function it would fit
    refused = app(run_cli, "query", covered, env=ENV)
    assert refused.error["code"] == "term.unknown"
    assert "items_in_location" not in json.dumps(refused.envelope)
    own = "MATCH (i:Item) WHERE i.condition = 'damaged' RETURN i.name AS item, i.condition AS c"
    answer = ok(run_cli, "query", own)
    for name in HIDDEN:
        assert name not in json.dumps(answer.envelope), name


def test_affordances_offer_no_hidden_function_or_action(
    run_cli: CliRunner, world: dict[str, str], tmp_path: Path
) -> None:
    ids = world
    full = ok(run_cli, "get", ids["chair"], "--affordances").data["affordances"]
    assert [a["action"] for a in full["actions"]] == ["move_item", "retire_item"]
    assert "functions" in full
    place = ok(run_cli, "get", ids["shelf"], "--affordances").data["affordances"]
    assert [f["function"] for f in place["functions"]] == ["items_in_location"]
    limit(run_cli, tmp_path, "inv:Item", writes=WRITES_ITEMS)
    limited = ok(run_cli, "get", ids["chair"], "--affordances")
    assert [a["action"] for a in limited.data["affordances"]["actions"]] == ["retire_item"]
    for name in HIDDEN:
        assert name not in json.dumps(limited.data["affordances"]), name
    # the card summarizes the same listing: names of what the reader may see, none hidden
    card = ok(run_cli, "view", ids["chair"])
    summary = next(s for s in card.data["sections"] if s["kind"] == "affordances")
    assert summary["actions"]["apply"] == ["retire_item"]
    for name in HIDDEN:
        assert name not in json.dumps(card.envelope), name


def test_doing_a_hidden_action_is_doing_a_missing_one(
    run_cli: CliRunner, world: dict[str, str], tmp_path: Path
) -> None:
    ids = world
    limit(run_cli, tmp_path, "inv:Item", writes=WRITES_ITEMS)
    for params in ((), (f"item={ids['chair']}", f"location={ids['shelf']}")):
        twin(
            run_cli,
            lambda n, params=params: ("do", n, *(a for p in params for a in ("--param", p))),
            "move_item",
            "move_nothing",
            "action.not_found",
        )
    retired = ok(
        run_cli,
        "do",
        "retire_item",
        "--param",
        f"item={ids['chair']}",
        "--dry-run",
        "--reason",
        "t",
    )
    assert retired.data["action"]["name"] == "retire_item"


# ---- the skill ----------------------------------------------------------------------------


CURATED_HIDDEN = (
    "Items sit in an `inv:Location` and belong to an `inv:Category`.\n"
    "\n"
    "Where an item is: `graph run items_in_location --param location=<id>`; move it with\n"
    "`graph do move_item --param item=<id> --param location=<id>`;\n"
    "count: `graph run count_things`.\n"
)


def publish_skill(run_cli: CliRunner, tmp_path: Path, *lines: str) -> None:
    folder = tmp_path / "skill" / "graph-example-graph"
    shutil.rmtree(folder.parent, ignore_errors=True)
    shutil.copytree(SKILL, folder)
    text = (folder / "SKILL.md").read_text()
    text = text.replace("## Is it available here?", "".join(lines) + "\n## Is it available here?")
    (folder / "SKILL.md").write_text(text)
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


def installed_graph_skill(project: Path) -> dict[str, bytes]:
    files, problems = read_tree(project / ".claude" / "skills" / "graph-example-graph")
    assert problems == []
    return files


def marker_source(project: Path) -> dict[str, Any]:
    marker = project / ".claude" / "skills" / "graph-example-graph" / MARKER_FILE
    source: dict[str, Any] = json.loads(marker.read_text())["source"]
    return source


def test_the_skill_of_a_limited_caller_names_no_hidden_term(
    run_cli: CliRunner, world: dict[str, str], tmp_path: Path
) -> None:
    publish_skill(run_cli, tmp_path)
    project = project_with_pointer(tmp_path)
    assert install_skills(run_cli, project).code == 0
    everything = b"".join(installed_graph_skill(project).values()).decode()
    assert "items_in_location" in everything and "move_item" in everything  # no limit yet
    assert "visible" not in marker_source(project)
    limit(run_cli, tmp_path, "inv:Item", writes=WRITES_ITEMS)
    installed = install_skills(run_cli, project)
    assert installed.code == 0, installed.envelope
    files = installed_graph_skill(project)
    for path, data in files.items():
        if path == MARKER_FILE:
            continue
        for name in HIDDEN:
            assert name not in data.decode(), (path, name)
    everything = b"".join(files.values()).decode()
    # the blocks stay (the caller may use functions and actions); no entry that is not featured
    # is listed, and the featured ones name a hidden term
    assert "Named functions:" in everything and "Named actions:" in everything
    assert "items_by_condition" not in everything and "retire_item" not in everything
    assert marker_source(project)["visible"].startswith("sha256:")


def test_curated_text_naming_a_hidden_type_installs_without_a_problem_naming_it(
    run_cli: CliRunner, world: dict[str, str], tmp_path: Path
) -> None:
    publish_skill(run_cli, tmp_path, CURATED_HIDDEN)
    project = project_with_pointer(tmp_path)
    control = install_skills(run_cli, project)
    assert control.code == 0 and "skills.source_unknown_names" not in control.warning_codes
    limit(run_cli, tmp_path, "inv:Item", writes=WRITES_ITEMS)
    limited = install_skills(run_cli, project)
    assert limited.code == 0, limited.envelope
    assert "skills.source_unknown_names" not in limited.warning_codes
    assert "inv:Location" not in json.dumps(limited.envelope)
    # the curated text is the maintainer's: it stays as written
    skill = installed_graph_skill(project)["SKILL.md"]
    assert b"`inv:Location`" in skill and b"graph run items_in_location" in skill
    # the check still runs: a function the catalog lost is reported, and only that one
    functions = tmp_path / "fewer_functions"
    shutil.copytree(FUNCTIONS, functions)
    republished = admin(run_cli, "graph", "functions", "publish", "example_graph", str(functions))
    assert republished.code == 0, republished.envelope
    again = install_skills(run_cli, project)
    assert again.code == 0 and again.warning_codes == ["skills.source_unknown_names"]
    (problem,) = again.warnings[0]["details"]["problems"]
    assert problem["rule"] == "function" and "count_things" in problem["message"]
    for name in ("inv:Location", "inv:Category", "items_in_location", "move_item"):
        assert name not in json.dumps(again.envelope), name


def test_a_policy_change_makes_the_installed_skill_stale(
    run_cli: CliRunner, world: dict[str, str], tmp_path: Path
) -> None:
    publish_skill(run_cli, tmp_path)
    project = project_with_pointer(tmp_path)
    limit(run_cli, tmp_path, "inv:Item", writes=WRITES_ITEMS)
    assert install_skills(run_cli, project).code == 0
    seen = marker_source(project)["visible"]
    fresh = ok(run_cli, "find", "--type", "inv:Item", cwd=project)
    assert "skills.stale" not in fresh.warning_codes
    limit(run_cli, tmp_path, "inv:Item", "inv:Category", writes=WRITES_ITEMS)  # the policy moved
    stale = ok(run_cli, "find", "--type", "inv:Item", cwd=project)
    assert "skills.stale" in stale.warning_codes
    entry = next(w for w in stale.warnings if w["code"] == "skills.stale")
    (skill,) = entry["details"]["skills"]
    assert skill["installed"]["visible"] == seen and skill["current"]["visible"] != seen
    assert install_skills(run_cli, project).code == 0  # refreshing makes it fresh again
    assert marker_source(project)["visible"] != seen
    assert (
        "skills.stale" not in ok(run_cli, "find", "--type", "inv:Item", cwd=project).warning_codes
    )


# ---- what the render shows of the catalogs, per reader (graph start plan S6) -----------------


def set_featured(tmp_path: Path, name: str, **featured: bool) -> Path:
    """The function sources with ``featured`` set per function name."""
    folder = tmp_path / f"functions_{name}"
    shutil.copytree(tmp_path / "functions", folder)
    for function, value in featured.items():
        path = folder / f"{function}.yaml"
        text = path.read_text().replace("featured: true\n", "").replace("featured: false\n", "")
        path.write_text(text.replace("\nparams:", f"\nfeatured: {str(value).lower()}\nparams:", 1))
    return folder


def check_states(
    run_cli: CliRunner, project: Path, env: dict[str, str] | None = None
) -> dict[str, str]:
    done = app(
        run_cli,
        "skills",
        "check",
        "--scope",
        "project",
        "--dir",
        str(project),
        cwd=project,
        env=env,
    )
    return {s["skill"]: s["status"] for s in done.data["skills"]}


def test_a_limited_copy_is_fresh_after_install_and_only_what_it_sees_makes_it_stale(
    run_cli: CliRunner, world: dict[str, str], tmp_path: Path, isolated_env: dict[str, str]
) -> None:
    """The digest of a limited reader covers the featured entries it sees: unfeaturing the
    function that names a hidden location changes nothing for it (copy fresh for check and for a
    read), featuring one it sees makes the copy stale. No digest of the whole catalog reaches
    it."""
    limit(run_cli, tmp_path, "inv:Item", writes=WRITES_ITEMS)
    publish_skill(run_cli, tmp_path)
    project = project_with_pointer(tmp_path)
    assert install_skills(run_cli, project).code == 0
    mine = marker_source(project)["featured"]
    assert check_states(run_cli, project) == {"graph-cli": "fresh", "graph-example-graph": "fresh"}
    find = ("find", "--type", "inv:Item", "--graph", "example_graph")
    assert app(run_cli, *find, cwd=project).warning_codes == []
    hidden = set_featured(tmp_path, "hidden", items_in_location=False)
    published = admin(run_cli, "graph", "functions", "publish", "example_graph", str(hidden))
    assert published.code == 0, published.envelope
    assert app(run_cli, *find, cwd=project).warning_codes == []
    assert check_states(run_cli, project)["graph-example-graph"] == "fresh"
    seen = set_featured(tmp_path, "seen", items_in_location=False, items_by_condition=True)
    published = admin(run_cli, "graph", "functions", "publish", "example_graph", str(seen))
    assert published.code == 0, published.envelope
    stale = app(run_cli, *find, cwd=project)
    assert stale.warning_codes == ["skills.stale"], stale.envelope
    (entry,) = stale.envelope["warnings"][0]["details"]["skills"]
    assert entry["installed"]["featured"] == mine
    assert check_states(run_cli, project)["graph-example-graph"] == "stale"
    # no digest of the whole catalog reaches the limited reader
    from graph_cli.catalog.file_catalog import FileCatalog
    from graph_cli.kernel.ids import GraphId, parse_id

    root = data_root(isolated_env)
    graph = next((root / "graphs").iterdir())
    whole = FileCatalog(root).skill_featured(GraphId(parse_id(graph.name)))
    assert whole not in {entry["installed"]["featured"], entry["current"]["featured"]}
    assert install_skills(run_cli, project).code == 0
    assert app(run_cli, *find, cwd=project).warning_codes == []


NO_FUNCTIONS = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {{graphs: "*", level: write}}
no_project: {{"*": write}}
{deny}"""
DENY_FUNCTIONS = """\
deny:
  - {id: no_functions, agent_type: [explore], action: ["read.function"]}
"""
EXPLORE = {**ENV, "GRAPH_CLI_AGENT_ID": "agent-1", "GRAPH_CLI_AGENT_TYPE": "explore"}


def test_a_caller_without_read_function_gets_no_function_lines_and_a_stale_copy_when_that_changes(
    run_cli: CliRunner, world: dict[str, str], tmp_path: Path
) -> None:
    """The graph skill lists featured functions only to a caller who may ``read.function``;
    the block says in one line that functions are not for this token. The copy records a
    digest for that caller, so granting the right makes it stale."""
    work = tmp_path / "policies"
    install(run_cli, work, NO_FUNCTIONS.format(deny=DENY_FUNCTIONS))
    publish_skill(run_cli, tmp_path, "")
    project = project_with_pointer(tmp_path)
    args = ("skills", "install", "--scope", "project", "--dir", str(project), "--graph")
    assert app(run_cli, *args, "example_graph", cwd=project, env=EXPLORE).code == 0
    text = b"".join(installed_graph_skill(project).values()).decode()
    assert "This token may not run named functions in this graph" in text
    assert "items_in_location" not in text and "Named actions:" in text
    states = check_states(run_cli, project, EXPLORE)
    assert states == {"graph-cli": "fresh", "graph-example-graph": "fresh"}
    find = ("find", "--type", "inv:Item", "--graph", "example_graph")
    assert app(run_cli, *find, cwd=project, env=EXPLORE).warning_codes == []
    # without the denial the same caller reads functions: its copy is stale, a refresh lists them
    install(run_cli, work, NO_FUNCTIONS.format(deny=""))
    stale = app(run_cli, *find, cwd=project, env=EXPLORE)
    assert stale.warning_codes == ["skills.stale"], stale.envelope
    assert check_states(run_cli, project, EXPLORE)["graph-example-graph"] == "stale"
    assert app(run_cli, *args, "example_graph", cwd=project, env=EXPLORE).code == 0
    refreshed = b"".join(installed_graph_skill(project).values()).decode()
    assert "may not run named functions" not in refreshed and "items_in_location" in refreshed
    assert app(run_cli, *find, cwd=project, env=EXPLORE).warning_codes == []
