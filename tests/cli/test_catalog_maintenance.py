"""C: what maintaining a graph does to the entries a catalog grant names (catalog callers plan,
step 4): ``graph functions|actions|views publish`` and ``graph push`` warn
``catalog.granted_entry_removed`` and ``catalog.granted_entry_changed`` (also on a dry run), a
publish that touches no granted entry warns about none, and ``policy explain`` shows for each
granted name whether the graph publishes it and its current hash.

The role ``catalog_agent`` is granted the function ``items_by_condition``, the action
``move_item`` and the view ``location_overview`` of ``example_graph`` (inventory); ``second_agent``
is granted the same function through a grant of its own. The graph publishes both functions, both
actions and the view, and ``items_elsewhere``, a function no granted entry names and no granted view
runs (the granted view ``location_overview`` runs ``items_in_location``; no view runs
``items_by_condition``, so it can be removed).
"""

import shutil
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init
from support.app import create_graph
from support.fixtures import FIXTURES
from support.policies import write_policy

pytestmark = pytest.mark.slow

POLICY = """\
schema: graph_cli.policy/1
id: installation
roles:
  catalog_agent: {description: "Runs three entries of example_graph"}
  second_agent: {description: "Runs one function of example_graph"}
grants:
  - entrypoint: server
    principal: {role: catalog_agent}
    graphs: [example_graph]
    level: catalog
    catalog:
      functions: [items_by_condition]
      actions: [move_item]
      views: [location_overview]
    reads: [inv:Item, inv:Location, inv:Category]
  - entrypoint: server
    principal: {role: second_agent, project: example_project}
    graphs: [example_graph]
    level: catalog
    catalog: {functions: [items_by_condition]}
    reads: [inv:Item]
scopes:
  example_project: {example_graph: write}
no_project: {"*": none}
"""
FUNCTIONS = FIXTURES / "functions" / "inventory" / "valid"
ACTIONS = FIXTURES / "actions" / "inventory" / "valid"
VIEWS = FIXTURES / "views" / "inventory" / "valid"
KINDS = ("functions", "actions", "views")
ELSEWHERE = """\
schema: graph_cli.function/1
name: items_elsewhere
summary: Items stored somewhere.
use_when: >-
  Which items are stored anywhere; a list of all items with their quantity.
query: |
  MATCH (i:Item)
  RETURN i.name AS item, i.quantity AS quantity
  ORDER BY item
returns:
  - column: item
    summary: Name of the item.
  - column: quantity
    summary: Number of units.
examples:
  - question: Which items are stored at all?
"""
REMOVED, CHANGED = "catalog.granted_entry_removed", "catalog.granted_entry_changed"


def copy_folder(source: Path, target: Path, *names: str) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    for name in names or sorted(p.name for p in source.glob("*.yaml")):
        shutil.copy(source / name, target / name)
    return target


@pytest.fixture
def work(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> dict[str, Path]:
    """The graph with its catalogs published, the policy installed and a folder per kind to
    change and publish again."""
    init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "inventory")
    folders = {
        "functions": copy_folder(FUNCTIONS, tmp_path / "functions"),
        "actions": copy_folder(ACTIONS, tmp_path / "actions"),
        "views": copy_folder(VIEWS, tmp_path / "views", "location_overview.yaml"),
    }
    (folders["functions"] / "items_elsewhere.yaml").write_text(ELSEWHERE)
    for kind, folder in folders.items():
        published = admin(run_cli, "graph", kind, "publish", "example_graph", str(folder))
        assert published.code == 0 and catalog_warnings(published) == [], published.envelope
    path = write_policy(tmp_path / "policies", "installation.yaml", POLICY)
    applied = admin(run_cli, "policy", "apply", str(path), "--reason", "catalog caller")
    assert applied.code == 0, applied.envelope
    return folders


def publish(run_cli: CliRunner, kind: str, folder: Path, *extra: str) -> Outcome:
    outcome = admin(run_cli, "graph", kind, "publish", "example_graph", str(folder), *extra)
    assert outcome.code == 0, outcome.envelope
    return outcome


def catalog_warnings(outcome: Outcome) -> list[dict[str, Any]]:
    return [w for w in outcome.warnings if w["code"].startswith("catalog.")]


def rows(run_cli: CliRunner, kind: str) -> dict[str, dict[str, Any]]:
    shown = admin(run_cli, "graph", kind, "show", "example_graph")
    assert shown.code == 0, shown.envelope
    return {r["name"]: r for r in shown.data[kind]}


def edit(folder: Path, name: str, old: str, new: str) -> None:
    path = folder / name
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new, 1))


# ---- publish: removed ------------------------------------------------------------------------


def test_removing_a_granted_function_warns_and_names_the_grants(
    run_cli: CliRunner, work: dict[str, Path]
) -> None:
    (work["functions"] / "items_by_condition.yaml").unlink()
    done = publish(run_cli, "functions", work["functions"])
    assert done.warning_codes == [REMOVED]
    (warning,) = done.warnings
    assert warning["details"] == {
        "graph": "example_graph",
        "kind": "function",
        "name": "items_by_condition",
        "grants": [
            {"grant": "grant.1", "principal": {"role": ["catalog_agent"]}},
            {
                "grant": "grant.2",
                "principal": {"project": ["example_project"], "role": ["second_agent"]},
            },
        ],
        "replaced_by": None,
    }
    for text in ("items_by_condition", "grant.1 (role catalog_agent)", "grant.2 (project"):
        assert text in warning["message"], warning["message"]
    assert "items_by_condition" not in rows(run_cli, "functions")


def test_a_dry_run_shows_the_warning_and_publishes_nothing(
    run_cli: CliRunner, work: dict[str, Path]
) -> None:
    before = rows(run_cli, "actions")
    (work["actions"] / "move_item.yaml").unlink()
    dry = publish(run_cli, "actions", work["actions"], "--dry-run")
    assert dry.warning_codes == [REMOVED] and dry.data["published"] is False
    assert dry.warnings[0]["details"]["kind"] == "action"
    assert rows(run_cli, "actions") == before
    done = publish(run_cli, "actions", work["actions"])
    assert done.warning_codes == [REMOVED] and done.data["published"] is True


def test_removing_a_granted_view_warns(run_cli: CliRunner, work: dict[str, Path]) -> None:
    (work["views"] / "location_overview.yaml").unlink()
    done = publish(run_cli, "views", work["views"])
    assert done.warning_codes == [REMOVED]
    assert done.warnings[0]["details"]["kind"] == "view"
    assert done.warnings[0]["details"]["grants"][0]["grant"] == "grant.1"


def test_a_rename_warns_for_the_old_name_only(run_cli: CliRunner, work: dict[str, Path]) -> None:
    """A grant names entries: the entry under its new name is a new entry, which no grant names."""
    old = work["actions"] / "move_item.yaml"
    new = work["actions"] / "relocate_item.yaml"
    new.write_text(old.read_text().replace("name: move_item", "name: relocate_item"))
    old.unlink()
    done = publish(run_cli, "actions", work["actions"])
    assert [(w["code"], w["details"]["name"]) for w in done.warnings] == [(REMOVED, "move_item")]


def test_a_removed_entry_names_its_replacement_but_the_grant_does_not_follow(
    run_cli: CliRunner, work: dict[str, Path]
) -> None:
    folder = work["functions"]
    edit(
        folder,
        "items_by_condition.yaml",
        "use_when:",
        "status: deprecated\nreplaced_by: items_in_location\nuse_when:",
    )
    deprecated = publish(run_cli, "functions", folder)
    # the deprecation is a change of the entry: its callers run the new definition
    assert CHANGED in deprecated.warning_codes
    (folder / "items_by_condition.yaml").unlink()
    gone = publish(run_cli, "functions", folder)
    (removed,) = [w for w in gone.warnings if w["code"] == REMOVED]
    assert removed["details"]["replaced_by"] == "items_in_location"
    assert "items_in_location" in removed["message"] and "does not cover" in removed["message"]
    # the policy still names the old function only: the replacement is granted to nobody
    shown = admin(
        run_cli, "policy", "explain", "--graph", "example_graph", "--project", "example_project",
        "--role", "catalog_agent",
    )  # fmt: skip
    names = {e["name"]: e for e in shown.data["catalog"]["functions"]}
    assert names == {
        "items_by_condition": {"name": "items_by_condition", "published": False, "hash": None}
    }


# ---- publish: changed --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "name", "old"),
    [
        ("functions", "items_by_condition", "summary: Items in a given"),
        ("actions", "move_item", "summary: Store an item"),
        ("views", "location_overview", "summary: "),
    ],
)
def test_changing_a_granted_entry_warns_with_both_hashes(
    run_cli: CliRunner, work: dict[str, Path], kind: str, name: str, old: str
) -> None:
    before = rows(run_cli, kind)[name]["hash"]
    edit(work[kind], f"{name}.yaml", old, old + " (reworded)")
    dry = publish(run_cli, kind, work[kind], "--dry-run")
    assert dry.warning_codes == [CHANGED]
    done = publish(run_cli, kind, work[kind])
    (warning,) = done.warnings
    after = rows(run_cli, kind)[name]["hash"]
    assert warning["details"]["hash_before"] == before != after == warning["details"]["hash_after"]
    assert warning["details"]["kind"] == kind.removesuffix("s")
    assert warning["details"]["grants"][0]["grant"] == "grant.1"
    # the same files again change nothing and warn about nothing
    again = publish(run_cli, kind, work[kind])
    assert catalog_warnings(again) == [] and again.data["published"] is False


# ---- publish: unaffected -----------------------------------------------------------------------


def test_a_publish_that_touches_no_granted_entry_has_no_catalog_warning(
    run_cli: CliRunner, work: dict[str, Path]
) -> None:
    same = publish(run_cli, "functions", work["functions"])
    assert same.warning_codes == [] and same.data["published"] is False
    # an entry no grant names: changed, then removed
    edit(
        work["functions"],
        "items_elsewhere.yaml",
        "summary: Items stored somewhere",
        "summary: Things stored somewhere",
    )
    changed = publish(run_cli, "functions", work["functions"])
    assert changed.data["published"] is True and catalog_warnings(changed) == []
    (work["actions"] / "retire_item.yaml").unlink()
    removed = publish(run_cli, "actions", work["actions"])
    assert removed.data["published"] is True and catalog_warnings(removed) == []
    # an entry added
    shutil.copy(VIEWS / "item_catalog.yaml", work["views"])
    added = publish(run_cli, "views", work["views"])
    assert added.data["published"] is True and catalog_warnings(added) == []


def test_a_name_the_graph_had_not_published_warns_about_nothing_when_it_appears(
    run_cli: CliRunner, work: dict[str, Path], tmp_path: Path
) -> None:
    """A grant for a name nothing publishes gave nothing (``policy.entry_unknown`` said so); the
    publish that adds the entry is no removal and no change."""
    (work["actions"] / "move_item.yaml").unlink()
    publish(run_cli, "actions", work["actions"])
    shutil.copy(ACTIONS / "move_item.yaml", work["actions"])
    back = publish(run_cli, "actions", work["actions"])
    assert catalog_warnings(back) == [] and back.data["published"] is True


def test_without_a_catalog_grant_no_publish_warns(
    run_cli: CliRunner, work: dict[str, Path], tmp_path: Path
) -> None:
    plain = write_policy(
        tmp_path / "plain",
        "installation.yaml",
        "schema: graph_cli.policy/1\nid: installation\ngrants:\n"
        "  - {entrypoint: server, graphs: '*', level: read}\nno_project: {'*': read}\n",
    )
    assert admin(run_cli, "policy", "apply", str(plain), "--reason", "plain").code == 0
    (work["functions"] / "items_by_condition.yaml").unlink()
    assert publish(run_cli, "functions", work["functions"]).warning_codes == []


# ---- graph push ---------------------------------------------------------------------------------


def push(run_cli: CliRunner, source: Path, *args: str) -> Outcome:
    outcome = admin(run_cli, "graph", "push", "example_graph", str(source), *args)
    assert outcome.code == 0, outcome.envelope
    return outcome


def test_a_push_warns_like_the_publish_commands(
    run_cli: CliRunner, work: dict[str, Path], tmp_path: Path
) -> None:
    source = tmp_path / "workspace"
    assert admin(run_cli, "graph", "pull", "example_graph", str(source)).code == 0
    assert push(run_cli, source, "--dry-run").warning_codes == []  # nothing differs
    (source / "actions" / "move_item.yaml").unlink()
    edit(source / "functions", "items_by_condition.yaml", "summary: Items in a given",
         "summary: Items in a given (reworded)")  # fmt: skip
    edit(
        source / "functions",
        "items_elsewhere.yaml",
        "summary: Items stored somewhere",
        "summary: Things stored somewhere",
    )
    dry = push(run_cli, source, "--dry-run")
    found = [(w["code"], w["details"]["kind"], w["details"]["name"]) for w in dry.warnings]
    assert found == [
        (CHANGED, "function", "items_by_condition"),
        (REMOVED, "action", "move_item"),
    ]
    assert "move_item" in rows(run_cli, "actions")  # a dry run publishes nothing
    done = push(run_cli, source)
    assert [(w["code"], w["details"]["name"]) for w in done.warnings] == [
        (CHANGED, "items_by_condition"),
        (REMOVED, "move_item"),
    ]
    assert "move_item" not in rows(run_cli, "actions")
    assert catalog_warnings(push(run_cli, source)) == []  # the same push again


# ---- publish: a function a granted view runs --------------------------------------------------


def test_a_function_a_granted_view_runs_warns_for_the_view(
    run_cli: CliRunner, work: dict[str, Path]
) -> None:
    """The view names its row function, never its hash, and runs what the graph publishes under
    the name: changing ``items_in_location`` changes what a token of the view grant reads."""
    edit(
        work["functions"],
        "items_in_location.yaml",
        "summary: Items stored",
        "summary: Things stored",
    )
    before = rows(run_cli, "functions")["items_in_location"]["hash"]
    dry = publish(run_cli, "functions", work["functions"], "--dry-run")
    (warning,) = catalog_warnings(dry)
    assert warning["code"] == CHANGED
    assert (warning["details"]["kind"], warning["details"]["name"]) == ("view", "location_overview")
    (function,) = warning["details"]["functions"]
    assert function["name"] == "items_in_location" and function["hash_before"] == before
    assert function["hash_after"] not in (None, before)
    assert [g["grant"] for g in warning["details"]["grants"]] == ["grant.1"]
    assert "items_in_location" in warning["message"] and "grant.1" in warning["message"]
    assert rows(run_cli, "functions")["items_in_location"]["hash"] == before  # a dry run
    done = publish(run_cli, "functions", work["functions"])
    assert [w["details"]["name"] for w in catalog_warnings(done)] == ["location_overview"]
    assert rows(run_cli, "functions")["items_in_location"]["hash"] == function["hash_after"]
    # the same publish again changes nothing and warns about nothing
    assert catalog_warnings(publish(run_cli, "functions", work["functions"])) == []


def test_a_push_warns_for_the_granted_view_of_a_function_it_changes(
    run_cli: CliRunner, work: dict[str, Path], tmp_path: Path
) -> None:
    source = tmp_path / "workspace"
    assert admin(run_cli, "graph", "pull", "example_graph", str(source)).code == 0
    edit(
        source / "functions",
        "items_in_location.yaml",
        "summary: Items stored",
        "summary: Things stored",
    )
    dry = push(run_cli, source, "--dry-run")
    assert [(w["code"], w["details"]["kind"], w["details"]["name"]) for w in dry.warnings] == [
        (CHANGED, "view", "location_overview")
    ]
    # the push that replaces the view too names it once, as a changed view
    edit(source / "views", "location_overview.yaml", "summary: A location", "summary: One location")
    both = push(run_cli, source, "--dry-run")
    assert [(w["details"]["kind"], w["details"]["name"]) for w in both.warnings] == [
        ("view", "location_overview")
    ]


# ---- policy explain ----------------------------------------------------------------------------


def test_policy_explain_says_whether_a_granted_name_is_published_and_its_hash(
    run_cli: CliRunner, work: dict[str, Path]
) -> None:
    def explain(role: str) -> dict[str, Any]:
        found = admin(
            run_cli, "policy", "explain", "--graph", "example_graph",
            "--project", "example_project", "--role", role,
        )  # fmt: skip
        assert found.code == 0, found.envelope
        catalog: dict[str, Any] = found.data["catalog"]
        return catalog

    shown = explain("catalog_agent")
    functions, actions, views = (rows(run_cli, k) for k in KINDS)
    assert shown["functions"] == [
        {
            "name": "items_by_condition",
            "published": True,
            "hash": functions["items_by_condition"]["hash"],
        }
    ]
    assert shown["actions"] == [
        {"name": "move_item", "published": True, "hash": actions["move_item"]["hash"]}
    ]
    assert shown["views"] == [
        {"name": "location_overview", "published": True, "hash": views["location_overview"]["hash"]}
    ]
    assert shown["catalog_only"] is True and shown["reader_rules"] == []
    # the hash follows a republish, and a name the graph stops publishing says so
    edit(work["actions"], "move_item.yaml", "summary: Store an item", "summary: Put an item")
    publish(run_cli, "actions", work["actions"])
    moved = explain("catalog_agent")["actions"][0]
    assert moved["published"] is True and moved["hash"] != actions["move_item"]["hash"]
    assert moved["hash"] == rows(run_cli, "actions")["move_item"]["hash"]
    (work["views"] / "location_overview.yaml").unlink()
    publish(run_cli, "views", work["views"])
    assert explain("catalog_agent")["views"] == [
        {"name": "location_overview", "published": False, "hash": None}
    ]
    # a role whose grants name one kind has the other lists empty
    second = explain("second_agent")
    assert [e["name"] for e in second["functions"]] == ["items_by_condition"]
    assert second["actions"] == [] and second["views"] == []


def test_policy_explain_fails_on_a_catalog_it_cannot_read_instead_of_saying_not_published(
    run_cli: CliRunner, work: dict[str, Path], isolated_env: dict[str, str]
) -> None:
    """``published: false`` says the grant gives nothing; a damaged slot says nothing of the
    kind, so the explanation stops with the slot's own error."""
    (slot,) = [
        p
        for p in Path(isolated_env["XDG_DATA_HOME"]).rglob("slot.json")
        if p.parent.name == "functions"
    ]
    slot.write_text("{not json")
    found = admin(
        run_cli, "policy", "explain", "--graph", "example_graph",
        "--project", "example_project", "--role", "catalog_agent",
    )  # fmt: skip
    assert found.code != 0 and found.error["code"] == "functions.slot_invalid", found.envelope
