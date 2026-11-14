"""S: one major release splits a module and keeps the data (migration plan step 9).

The ``workshop`` fixture series: 1.0.0 is one module, 2.0.0 an umbrella with two areas. Classes
and relations move by a CURIE ``renamed_from``, one class is merged into another by ``retype``,
a required attribute is filled, an enum value is mapped and a ``files:File`` subclass moves.
The graph holds data with aliases and history, a published function with bare names and a
policy whose rules name the old and the new terms. One ``graph push`` carries the ontology, the
migration and the functions; afterwards the ids, aliases, edges and history are the ones
before, read under the current names, the function works, the hidden type is still hidden and
``audit verify`` is clean.
"""

import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init, write_documents
from support.app import ENV, app
from support.files import synthetic_pdf, write_pdf
from support.fixtures import ONTOLOGIES
from support.policies import write_policy

from graph_cli.kernel.ids import GraphId, parse_id
from graph_cli.kernel.query_ast import Query
from graph_cli.kernel.records import Revision
from graph_cli.storage.files.store import FileStore

pytestmark = pytest.mark.slow

WORKSHOP = ONTOLOGIES / "workshop"

MIGRATION = """\
format: graph_cli.migration/1
from: 1.0.0
summary: Split the workshop into planning and equipment; the gadgets are tools now.
steps:
  - retype: {from: wk:Gadget, to: equip:Tool, attributes: {title: name}}
  - fill: {type: plan:Task, attribute: priority, value: normal}
  - map_values: {type: plan:Project, attribute: phase, values: {paused: running}}
"""

# a deny rule and a hidden type, each naming the old and the new term (plan M8)
POLICY = """\
schema: graph_cli.policy/1
id: graph
types:
  "wk:Budget": none
  "plan:Budget": none
deny:
  - {id: titles_stay, type: ["wk:Task", "plan:Task"], attribute: [title], action: ["write.update"]}
"""

FUNCTIONS = {
    "tasks_by_project": """\
schema: graph_cli.function/1
name: tasks_by_project
module: workshop
summary: The tasks of every project.
use_when: >-
  Which tasks a project has; the work of a project.
query: |
  MATCH (t:Task)-[:belongs_to]->(p:Project)
  RETURN p.title AS project, t.title AS task
  ORDER BY project, task
returns:
  - column: project
    summary: Name of the project.
  - column: task
    summary: What the task says.
examples:
  - question: Which tasks does each project have?
""",
    "tools_by_task": """\
schema: graph_cli.function/1
name: tools_by_task
module: workshop
summary: The tools that tasks need.
use_when: >-
  Which tool a task needs; what is needed for the work.
query: |
  MATCH (t:Task)-[:uses]->(x:Tool)
  RETURN t.title AS task, x.name AS tool
  ORDER BY task, tool
returns:
  - column: task
    summary: What the task says.
  - column: tool
    summary: Name of the tool.
examples:
  - question: Which tools do the tasks need?
""",
}

SEED = [
    {"op": "create", "type": "wk:Site", "local": "yard", "attrs": {"name": "Yard"}},
    {
        "op": "create",
        "type": "wk:Project",
        "local": "shed",
        "attrs": {"title": "Shed", "phase": "paused"},
    },
    {
        "op": "create",
        "type": "wk:Project",
        "local": "fence",
        "attrs": {"title": "Fence", "phase": "done"},
    },
    {"op": "create", "type": "wk:Budget", "local": "money", "attrs": {"amount": 500}},
    {"op": "create", "type": "wk:Tool", "local": "saw", "attrs": {"name": "Saw"}},
    {"op": "create", "type": "wk:Gadget", "local": "level", "attrs": {"title": "Level"}},
    {"op": "create", "type": "wk:Gadget", "local": "plumb", "attrs": {"title": "Plumb bob"}},
    {
        "op": "create",
        "type": "wk:Task",
        "local": "dig",
        "attrs": {"title": "Dig"},
        "edges": [
            {"rel": "wk:belongs_to", "target": "@shed"},
            {"rel": "wk:uses", "target": "@saw"},
        ],
    },
    {
        "op": "create",
        "type": "wk:Task",
        "local": "paint",
        "attrs": {"title": "Paint"},
        "edges": [{"rel": "wk:belongs_to", "target": "@fence"}],
    },
    {
        "op": "create",
        "type": "wk:Task",
        "local": "old",
        "attrs": {"title": "Obsolete"},
        "edges": [{"rel": "wk:belongs_to", "target": "@shed"}],
    },
]


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    return init(run_cli, isolated_env)


@dataclass(frozen=True)
class Scene:
    run_cli: CliRunner
    root: Path
    home: Path
    graph_id: str
    ids: dict[str, str]  # alias -> id before the release

    def store(self) -> FileStore:
        return FileStore(self.root / "graphs" / self.graph_id, GraphId(parse_id(self.graph_id)))

    def state(self) -> dict[str, Revision]:
        """Every current revision of every status, by id (the raw store, no policy, no names)."""
        snapshot = self.store().snapshot()
        page = snapshot.query(
            Query(record_status=frozenset({"active", "superseded", "retracted"}), limit=10**6)
        )
        return {str(r.id): r for r in page.items}

    def app(self, *args: str) -> Outcome:
        return app(self.run_cli, *args, env=ENV)

    def ok(self, *args: str) -> Outcome:
        outcome = self.app(*args)
        assert outcome.code == 0, outcome.envelope
        return outcome

    def id_of(self, alias: str) -> str:
        return str(self.ok("get", alias).data["entity"]["id"])


def create_graph(run_cli: CliRunner) -> str:
    outcome = admin(
        run_cli,
        "graph",
        "create",
        "example_graph",
        "--ontology",
        str(WORKSHOP / "1.0.0"),
        "--module",
        "workshop",
    )
    assert outcome.code == 0, outcome.envelope
    graph_id: str = outcome.data["id"]
    return graph_id


def configure_roots(env: dict[str, str]) -> None:
    config = Path(env["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.write_text(config.read_text().rstrip("\n") + '\n[roots]\nmanuals = "~/manuals"\n')


def load_before(run_cli: CliRunner, root: Path, env: dict[str, str], tmp_path: Path) -> Scene:
    """The 1.0.0 graph with data, history, a stored file, a policy and two functions."""
    graph_id = create_graph(run_cli)
    home = Path(env["HOME"])
    (home / "manuals").mkdir()
    configure_roots(env)
    write_documents(root, "example_graph", [{"ops": SEED}])
    scene = Scene(run_cli, root, home, graph_id, {})
    found = scene.ok("find", "--type", "wk:Task", "--sort", "alias").data["items"]
    assert [i["alias"] for i in found] == ["TASK-0001", "TASK-0002", "TASK-0003"]
    dig, old = found[0]["id"], found[2]["id"]
    # history: a second revision of a task and of a gadget, one retracted task
    scene.ok("update", dig, "--set", "title=Dig the pit")
    gadget = scene.id_of("GADG-0001")
    scene.ok("update", gadget, "--set", "title=Spirit level")
    scene.ok("retract", old, "--reason", "not needed")
    # a stored file of the moved subclass, cited by a task with a page
    write_pdf(home / "manuals" / "saw.pdf", marker="saw")
    added = scene.ok(
        "file",
        "add",
        str(home / "manuals" / "saw.pdf"),
        "--type",
        "wk:Manual",
        "--title",
        "Saw manual",
        "--set",
        "edition=2nd",
    )
    assert added.data["entity"]["alias"] == "MAN-0001"
    scene.ok("link", dig, "files:from_file", "MAN-0001", "--qualifier-json", "page=3")
    # the policy and the functions come last: data written through the services before a policy
    # is applied keeps the audit trail of the policy hashes in order
    policy = write_policy(tmp_path / "policies", "graph.yaml", POLICY)
    applied = admin(
        run_cli, "policy", "apply", str(policy), "--graph", "example_graph", "--reason", "r"
    )
    assert applied.code == 0, applied.envelope
    folder = tmp_path / "functions"
    folder.mkdir()
    for name, text in FUNCTIONS.items():
        (folder / f"{name}.yaml").write_text(text)
    published = admin(run_cli, "graph", "functions", "publish", "example_graph", str(folder))
    assert published.code == 0, published.envelope
    aliases = {r.alias: str(r.id) for r in scene.state().values() if r.alias}
    assert len(aliases) == 11  # ten seeded entities and the manual
    return Scene(run_cli, root, home, graph_id, aliases)


def test_one_release_splits_the_module_and_keeps_the_data(
    run_cli: CliRunner, root: Path, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    scene = load_before(run_cli, root, isolated_env, tmp_path)
    before = scene.state()
    rows_before = {
        name: scene.ok("run", name).data["items"] for name in ("tasks_by_project", "tools_by_task")
    }
    assert rows_before["tasks_by_project"] == [
        {"project": "Fence", "task": "Paint"},
        {"project": "Shed", "task": "Dig the pit"},
    ]
    seq_before = scene.store().head().seq
    released = push(scene, tmp_path)
    after = scene.state()

    # the answer: one migration with the derived renames and the three steps
    block = released.data["ontology"]["migration"]
    assert released.data["ontology"]["pinned"] is True
    assert block["unused_steps"] == [] and block["findings"]["count"] == 0
    assert (block["entities"], block["unchanged"], block["edges"]) == (11, 0, 4)
    assert block["migrated"]["entities"] == 11 and block["migrated"]["edges"] == 4
    by_step = block["migrated"]["by_step"]
    assert by_step["steps[0].retype"] == 2  # the gadgets
    assert by_step["steps[1].fill"] == 3  # every task, the retracted one included
    assert by_step["steps[2].map_values"] == 1  # the paused project
    assert {r["kind"] for r in block["renames"]} == {"rename_class", "rename_relation"}
    assert [r["to"] for r in block["renames"] if r["kind"] == "rename_relation"] == [
        "plan:belongs_to",
        "plan:uses",
    ]
    republished = released.data["ontology"]["republished"]["functions"]
    assert republished["names"] == ["tasks_by_project", "tools_by_task"]

    check_store(before, after, seq_before)
    check_reads(scene, before)
    check_history(scene, seq_before)
    check_catalog(scene, rows_before)  # before the next check creates a task
    check_aliases_continue(scene)
    check_policy(scene)

    # nothing is left to do: the workspace describes the graph, the checks are clean
    work = tmp_path / "work"
    assert admin(run_cli, "graph", "push", "example_graph", str(work)).data["unchanged"] is True
    checked = admin(run_cli, "graph", "check", "example_graph")
    assert checked.code == 0, checked.envelope
    assert checked.data["counts"]["error"] == 0
    assert admin(run_cli, "files", "verify", "--graph", "example_graph").code == 0
    verified = admin(run_cli, "audit", "verify")
    assert verified.code == 0, verified.envelope
    assert verified.data["counts"]["error"] == 0


TYPES = {
    "wk:Project": "plan:Project",
    "wk:Task": "plan:Task",
    "wk:Budget": "plan:Budget",
    "wk:Tool": "equip:Tool",
    "wk:Gadget": "equip:Tool",  # merged by the retype step
    "wk:Manual": "equip:Manual",
    "wk:Site": "equip:Site",
}
RELATIONS = {"wk:belongs_to": "plan:belongs_to", "wk:uses": "plan:uses"}


def check_store(before: dict[str, Revision], after: dict[str, Revision], seq_before: int) -> None:
    """What the migration tx did to every revision, read from the raw store."""
    assert after.keys() == before.keys()  # same ids, none added or removed
    assert {r.tx_seq for r in after.values()} == {seq_before + 1}  # one tx
    for entity, old in before.items():
        new = after[entity]
        # kept
        assert (new.alias, new.created_at, new.record_status) == (
            old.alias,
            old.created_at,
            old.record_status,
        )
        assert (new.valid_from, new.valid_to) == (old.valid_from, old.valid_to)
        assert (new.rev, new.prev_rev) == (old.rev + 1, old.rev)
        assert new.ontology_version == "2.0.0"
        # changed
        assert new.type == TYPES[old.type]
        assert [(e.edge_id, e.target, dict(e.qualifiers), e.target_rev) for e in new.edges] == [
            (e.edge_id, e.target, dict(e.qualifiers), e.target_rev) for e in old.edges
        ]
        assert [e.rel for e in new.edges] == [RELATIONS.get(e.rel, e.rel) for e in old.edges]
    by_alias = {r.alias: r for r in after.values()}
    assert by_alias["GADG-0001"].attrs == {"name": "Spirit level"}  # title -> name
    assert by_alias["GADG-0002"].attrs == {"name": "Plumb bob"}
    assert [by_alias[f"TASK-000{n}"].attrs["priority"] for n in (1, 2, 3)] == ["normal"] * 3
    assert by_alias["TASK-0003"].record_status == "retracted"  # migrated too, status kept
    assert {r.attrs["phase"] for r in after.values() if r.type == "plan:Project"} == {
        "running",
        "done",
    }
    assert by_alias["BUD-0001"].attrs == {"amount": 500}
    dig = by_alias["TASK-0001"]
    page = next(e for e in dig.edges if e.rel == "files:from_file")
    assert dict(page.qualifiers) == {"page": 3}  # the standard relation and its qualifier stay


def check_reads(scene: Scene, before: dict[str, Revision]) -> None:
    """Aliases and ids resolve to the same entities, now under the new names."""
    for alias, entity in scene.ids.items():
        if alias.startswith("BUD"):
            continue  # hidden by the policy
        shown = scene.ok("get", alias).data["entity"]
        assert shown["id"] == entity
        assert shown["type"] == TYPES[before[entity].type]
        assert scene.ok("get", entity).data["entity"]["alias"] == alias
    tasks = scene.ok("find", "--type", "plan:Task", "--sort", "alias").data["items"]
    assert [i["alias"] for i in tasks] == ["TASK-0001", "TASK-0002"]  # the third is retracted
    tools = scene.ok("find", "--type", "equip:Tool", "--sort", "alias").data["items"]
    assert [i["alias"] for i in tools] == ["GADG-0001", "GADG-0002", "TOOL-0001"]
    # old names on input fail and say what they are called now
    old = scene.app("find", "--type", "wk:Task")
    assert (old.code, old.error["code"]) == (1, "term.unknown")
    assert "plan:Task" in old.error["message"] and "2.0.0" in old.error["message"]
    # the stored file is where it was, with its content
    shown = scene.ok("file", "path", "MAN-0001").data
    stored = Path(shown["path"])
    assert stored.read_bytes() == synthetic_pdf("saw")
    assert stat.S_IMODE(stored.stat().st_mode) == 0o444
    assert scene.ok("file", "check").data["counts"]["same"] == 1


def check_history(scene: Scene, seq_before: int) -> None:
    """History and ``--as-of`` show old revisions under the current names; the migration
    revision is marked."""
    dig = scene.ids["TASK-0001"]
    history = {i["rev"]: i for i in scene.ok("history", dig).data["items"]}
    assert sorted(history) == [1, 2, 3, 4]  # created, retitled, linked, migrated
    assert history[2]["changed"]["attrs"] == ["title"]
    marked = history[4]
    assert marked["reason"] == "split the workshop"
    assert (marked["migration"]["from"], marked["migration"]["to"]) == ("1.0.0", "2.0.0")
    assert marked["migration"]["summary"].startswith("Split the workshop")
    assert {"rename_class", "rename_relation"} <= {
        r["kind"] for r in marked["migration"]["renames"]
    }
    assert [s["kind"] for s in marked["migration"]["steps"]] == ["fill"]  # those that touched it
    assert all("migration" not in history[r] for r in (1, 2, 3))
    # the first revision of the gadget: type, attribute and enum value all under current names
    gadget = scene.ok("get", "GADG-0001", "--rev", "1", "--full").data["entity"]
    assert (gadget["type"], gadget["attrs"]) == ("equip:Tool", {"name": "Level"})
    assert gadget["names_mapped"] is True and gadget["ontology_version"] == "1.0.0"
    first = scene.ok("get", dig, "--rev", "1", "--full").data["entity"]
    assert first["type"] == "plan:Task"
    assert [e["rel"] for e in first["edges"]] == ["plan:belongs_to", "plan:uses"]
    current = scene.ok("get", dig, "--full").data["entity"]
    assert "names_mapped" not in current  # current revisions are never mapped
    shed = scene.ids["PROJ-0001"]
    assert scene.ok("get", shed, "--rev", "1").data["entity"]["attrs"]["phase"] == "running"
    # --as-of before the release answers with the current names and the mapped value
    as_of = f"tx:{seq_before}"
    found = scene.ok("find", "--type", "plan:Project", "--where", "phase=running", "--as-of", as_of)
    assert [i["alias"] for i in found.data["items"]] == ["PROJ-0001"]
    [warning] = [w for w in found.warnings if w["code"] == "temporal.as_of_read"]
    assert warning["details"]["names_mapped"] is True
    tasks = scene.ok("find", "--type", "plan:Task", "--sort", "alias", "--as-of", as_of)
    assert [i["attrs"]["title"] for i in tasks.data["items"]] == ["Dig the pit", "Paint"]
    queried = scene.ok(
        "query",
        "MATCH (t:Task)-[:belongs_to]->(p:Project) RETURN p.title AS project, t.title AS task "
        "ORDER BY task",
        "--as-of",
        as_of,
    )
    assert queried.data["items"] == [
        {"project": "Shed", "task": "Dig the pit"},
        {"project": "Fence", "task": "Paint"},
    ]


def check_aliases_continue(scene: Scene) -> None:
    """The prefix moved with its class: the numbering goes on where it stopped."""
    project = scene.ids["PROJ-0001"]
    task = scene.ok(
        "create",
        "plan:Task",
        "--set",
        "title=Roof",
        "--set",
        "priority=high",
        "--edge",
        f"plan:belongs_to={project}",
    )
    assert task.data["entities"][0]["alias"] == "TASK-0004"
    tool = scene.ok("create", "equip:Tool", "--set", "name=Drill")
    assert tool.data["entities"][0]["alias"] == "TOOL-0002"
    assert (
        scene.ok("create", "plan:Project", "--set", "title=Gate").data["entities"][0]["alias"]
        == "PROJ-0003"
    )
    # a required attribute of the new version applies to new entities
    refused = scene.app(
        "create", "plan:Task", "--set", "title=Door", "--edge", f"plan:belongs_to={project}"
    )
    assert (refused.code, refused.error["code"]) == (1, "write.invalid")


def check_catalog(scene: Scene, rows_before: dict[str, Any]) -> None:
    """The functions were published again with the release and answer as before. The reader
    has read limits (the policy hides a type) and their module, the main module, holds no term
    of its own: they stay visible to it."""
    listed = scene.ok("describe", "functions").data["functions"]
    assert {f["name"] for f in listed} == set(rows_before)
    for name, rows in rows_before.items():
        assert scene.ok("run", name).data["items"] == rows
    slot = admin(scene.run_cli, "graph", "functions", "show", "example_graph").data["slot"]
    assert slot["published_against"]["version"] == "2.0.0"


def check_policy(scene: Scene) -> None:
    """The hidden type is still hidden under its new name; the deny rule still holds."""
    hidden = scene.app("find", "--type", "plan:Budget")
    nothing = scene.app("find", "--type", "plan:Nothing")
    assert hidden.code == 1 and hidden.error["code"] == nothing.error["code"]
    assert scene.app("get", "BUD-0001").code == 1
    task = scene.ids["TASK-0001"]
    denied = scene.app("update", task, "--set", "title=Dig again")
    assert (denied.code, denied.error["code"]) == (3, "access.denied")
    assert denied.error["details"]["rule"] == "deny.titles_stay"
    assert scene.ok("update", task, "--set", "priority=high").code == 0
    # the rule did not swallow the rest: a tool can be renamed
    assert scene.ok("update", scene.ids["TOOL-0001"], "--set", "name=Handsaw").code == 0


def push(scene: Scene, tmp_path: Path) -> Outcome:
    work = tmp_path / "work"
    pulled = admin(scene.run_cli, "graph", "pull", "example_graph", str(work))
    assert pulled.code == 0, pulled.envelope
    shutil.copytree(WORKSHOP / "2.0.0", work / "ontology" / "2.0.0")
    (work / "ontology" / "2.0.0" / "migration.yaml").write_text(MIGRATION)
    dry = admin(
        scene.run_cli, "graph", "push", "example_graph", str(work), "--dry-run", "--reason", "split"
    )
    assert dry.code == 0, dry.envelope
    pushed = admin(
        scene.run_cli, "graph", "push", "example_graph", str(work), "--reason", "split the workshop"
    )
    assert pushed.code == 0, pushed.envelope
    return pushed


_ = (stat, synthetic_pdf, Any)
