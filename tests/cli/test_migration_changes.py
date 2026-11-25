"""S: the change feed across an ontology release that migrated data (change feed plan CF7, M7).

The inventory series 1.1.0 -> 2.0.0 renames ``inv:Item.name`` to ``label`` and maps the removed
condition ``worn`` to ``good`` (as in ``test_read_names_mapped.py``). The migration tx has an
entry of its own with the versions and the summary and the entities it rewrote, whatever it
changed in them; revisions before it show the current names. A second release retypes an entity
into a class that a reader's graph policy hides: for that reader the entity has no item in any
tx (the rule of the type at the head), the migration entry is still there and names nothing,
and the entity is gone like one that never existed. In the other direction (a retype out of a
hidden class into a visible one) the entity is visible and the revisions it had as the hidden
class are as absent as a revision that was never stored: ``get --rev``, ``history``, the feed and
an ``--as-of`` read, though the release maps them to the current class (F82 fix, F1b).
"""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init, write_documents
from support.app import ENV, app
from support.moved_supplier import INVENTORY
from support.policies import write_policy

MIGRATION = """\
format: graph_cli.migration/1
from: 1.1.0
summary: The condition worn is gone; those items count as good.
steps:
  - map_values: {type: inv:Item, attribute: condition, values: {worn: good}}
"""
PLACES = [
    {
        "op": "create",
        "type": "inv:Location",
        "local": "room",
        "attrs": {"name": "Room", "code": "R1"},
    },
    {"op": "create", "type": "inv:Category", "local": "tools", "attrs": {"name": "Tools"}},
]


def item(local: str, name: str, condition: str) -> dict[str, Any]:
    return {
        "op": "create",
        "type": "inv:Item",
        "local": local,
        "attrs": {"name": name, "condition": condition},
        "edges": [
            {"rel": "inv:located_in", "target": "@room"},
            {"rel": "inv:in_category", "target": "@tools"},
        ],
    }


SEED = [*PLACES, item("hammer", "Hammer", "worn"), item("saw", "Saw", "good")]


def create(run_cli: CliRunner) -> None:
    outcome = admin(
        run_cli, "graph", "create", "example_graph", "--ontology", str(INVENTORY / "1.1.0")
    )
    assert outcome.code == 0, outcome.envelope


def second(tmp_path: Path) -> Path:
    target = tmp_path / "v2"
    shutil.copytree(INVENTORY / "2.0.0", target)
    (target / "migration.yaml").write_text(MIGRATION)
    return target


def release(run_cli: CliRunner, source: Path, *extra: str) -> Outcome:
    outcome = admin(run_cli, "ontology", "release", "example_graph", str(source), *extra)
    assert outcome.code == 0, outcome.envelope
    return outcome


def hammer_id(run_cli: CliRunner) -> str:
    found = app(run_cli, "find", "--type", "inv:Item", "--sort", "alias", env=ENV)
    assert found.code == 0, found.envelope
    return str(found.data["items"][0]["id"])


def update(root: Path, entity: str, attrs: dict[str, Any], seed: int) -> None:
    write_documents(
        root, "example_graph", [{"ops": [{"op": "update", "id": entity, "set": attrs}]}], seed=seed
    )


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    return init(run_cli, isolated_env)


def feed(run_cli: CliRunner, since: str = "tx:0") -> dict[str, Any]:
    outcome = app(run_cli, "changes", "--since", since, "--limit", "500", env=ENV)
    assert outcome.code == 0, outcome.envelope
    data: dict[str, Any] = outcome.data
    return data


NONE = {"attrs": [], "edges": {"added": [], "removed": [], "changed": []}, "record": []}


def test_a_migration_has_an_entry_with_the_entities_it_rewrote(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create(run_cli)
    write_documents(root, "example_graph", [{"ops": SEED}])
    hammer = hammer_id(run_cli)
    update(root, hammer, {"name": "Big hammer"}, seed=14)
    stored = feed(run_cli)
    assert [e["tx_seq"] for e in stored["items"]] == [1, 2] and stored["next"] == "tx:2"
    release(run_cli, second(tmp_path), "--reason", "rename name to label")
    update(root, hammer, {"label": "Bigger hammer"}, seed=15)

    data = feed(run_cli)
    assert [e["tx_seq"] for e in data["items"]] == [1, 2, 3, 4] and data["head"] == 4
    _created, renamed, migration, after = data["items"]
    # a migration tx: the release's command and reason, the versions and the summary
    assert (migration["command"], migration["reason"]) == (
        "ontology release",
        "rename name to label",
    )
    assert migration["migration"] == {
        "from": "1.1.0",
        "to": "2.0.0",
        "summary": "The condition worn is gone; those items count as good.",
    }
    assert [e["tx_seq"] for e in data["items"] if "migration" in e] == [3]
    # the entities it rewrote, each a new revision; what the name mapping explains (the
    # rename, the mapped value) is not a change, as in history
    rewritten = {i["id"]: i for i in migration["entities"]}
    items = app(run_cli, "find", "--type", "inv:Item", env=ENV).data["items"]
    assert sorted(rewritten) == sorted(i["id"] for i in items) and len(items) == 2
    assert {i["rev"] for i in rewritten.values()} == {2, 3}  # the hammer had two revisions
    for item in rewritten.values():
        assert item["changed"] == NONE and item["type"] == "inv:Item"
    # revisions before it are shown under the current names
    assert [i["changed"]["attrs"] for i in renamed["entities"]] == [["label"]]  # stored: name
    assert [i["changed"]["attrs"] for i in after["entities"]] == [["label"]]
    assert "migration" not in renamed and "migration" not in after
    # a consumer that stopped before the release gets exactly the migration entry next
    resumed = feed(run_cli, stored["next"])
    assert [e["tx_seq"] for e in resumed["items"]] == [3, 4]
    assert resumed["since_tx_id"] == stored["next_tx_id"]  # the log it read is the one here
    assert resumed["items"][0] == migration
    # the text form
    text = run_cli("graph", "changes", "--since", "tx:2", env=ENV)
    assert text.returncode == 0 and text.stderr == "", text.stderr
    lines = text.stdout.splitlines()
    assert lines[0].split()[0:1] == ["tx:3"] and "ontology release" in lines[0]
    assert "  reason: rename name to label" in lines
    assert (
        "  migration 1.1.0 -> 2.0.0: The condition worn is gone; those items count as good."
        in lines
    )


RETYPE = """\
format: graph_cli.migration/1
from: 1.1.0
summary: Reorganised.
steps:
  - retype: {from: inv:Category, to: inv:Supplier}
"""
HIDE_SUPPLIERS = """\
schema: graph_cli.policy/1
id: graph
types:
  "inv:Supplier": none
"""


@pytest.mark.doc_claim("security_model.change_feed_activity")
def test_a_retype_into_a_hidden_type_leaves_the_entity_out_and_keeps_the_migration_entry(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create(run_cli)
    ops = [*PLACES[:1], {"op": "create", "type": "inv:Category", "attrs": {"name": "Misc"}}]
    write_documents(root, "example_graph", [{"ops": ops}])
    category = app(run_cli, "find", "--type", "inv:Category", env=ENV).data["items"][0]["id"]
    before = feed(run_cli)
    assert before["view"] is None and len(before["items"][0]["entities"]) == 2
    source = tmp_path / "retyped"
    source.mkdir()
    text = (INVENTORY / "1.1.0" / "inventory.yaml").read_text()
    (source / "inventory.yaml").write_text(
        text.replace("  version: 1.1.0\n", "  version: 2.0.0\n", 1)
    )
    (source / "migration.yaml").write_text(RETYPE)
    release(run_cli, source)
    # without a read limit: the entity, and the migration that rewrote it
    plain = feed(run_cli)
    assert [e["tx_seq"] for e in plain["items"]] == [1, 2]
    assert [i["id"] for i in plain["items"][1]["entities"]] == [category]
    assert plain["items"][1]["entities"][0]["type"] == "inv:Supplier"
    policy = write_policy(tmp_path / "policies", "hide.yaml", HIDE_SUPPLIERS)
    applied = admin(
        run_cli, "policy", "apply", str(policy), "--graph", "example_graph", "--reason", "test"
    )
    assert applied.code == 0, applied.envelope

    limited = feed(run_cli)
    assert limited["view"] is not None  # a consumer that stored None must start again
    first, migration = limited["items"]
    assert [i["type"] for i in first["entities"]] == ["inv:Location"]  # the category: no item
    assert (migration["tx_seq"], migration["entities"]) == (2, [])  # the entry stays, empty
    assert migration["migration"] == {"from": "1.1.0", "to": "2.0.0", "summary": "Reorganised."}
    assert limited["next"] == "tx:2" and limited["more"] is False
    # no item of it in any tx, whatever the position; resumed from the first tx
    for since in ("tx:0", "tx:1"):
        text = json.dumps(feed(run_cli, since))
        assert category not in text and "Supplier" not in text and "Category" not in text
    resumed = feed(run_cli, "tx:1")
    assert resumed["items"] == [migration]
    # the entity is as gone as one that never existed: ``get`` answers alike
    hidden = app(run_cli, "get", category, env=ENV)
    missing = app(run_cli, "get", "0192f3c0-11aa-7e01-b2c3-d4e5f6a7b8c9", env=ENV)
    assert hidden.code == missing.code == 1
    assert hidden.error["code"] == missing.error["code"] == "entity.not_found"


MOVE_OUT = """\
format: graph_cli.migration/1
from: 1.1.0
summary: Reorganised.
steps:
  - retype: {from: inv:Supplier, to: inv:Category}
"""


@pytest.mark.doc_claim("security_model.revision_gaps")
def test_a_retype_out_of_a_hidden_type_leaves_its_earlier_revisions_absent(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    """The release is a major one, so the revisions before it are mapped to the current names,
    a retype included (``services/names.py``). A reader who may not see the class they were
    stored under sees none of them, nor the values in them: the entity starts, for this reader,
    with the revision the migration wrote (F1b, F2, F3)."""
    create(run_cli)
    ops = [{"op": "create", "type": "inv:Supplier", "attrs": {"name": "Alpha"}}]
    write_documents(root, "example_graph", [{"ops": ops}])
    supplier = app(run_cli, "find", "--type", "inv:Supplier", env=ENV).data["items"][0]["id"]
    update(root, supplier, {"name": "Beta"}, seed=14)
    update(root, supplier, {"name": "Gamma"}, seed=15)
    source = tmp_path / "moved_out"
    source.mkdir()
    text = (INVENTORY / "1.1.0" / "inventory.yaml").read_text()
    (source / "inventory.yaml").write_text(
        text.replace("  version: 1.1.0\n", "  version: 2.0.0\n", 1)
    )
    (source / "migration.yaml").write_text(MOVE_OUT)
    release(run_cli, source)
    # without a read limit every revision is there, mapped to the class it has now
    plain = app(run_cli, "history", supplier, env=ENV)
    assert [i["rev"] for i in plain.data["items"]] == [4, 3, 2, 1]
    first = app(run_cli, "get", supplier, "--rev", "1", env=ENV)
    assert first.data["entity"]["type"] == "inv:Category"
    policy = write_policy(tmp_path / "policies", "hide.yaml", HIDE_SUPPLIERS)
    applied = admin(
        run_cli, "policy", "apply", str(policy), "--graph", "example_graph", "--reason", "test"
    )
    assert applied.code == 0, applied.envelope

    # the entity is a category now, and visible, with the revision the migration wrote
    current = app(run_cli, "get", supplier, env=ENV)
    assert current.code == 0, current.envelope
    assert (current.data["entity"]["type"], current.data["current_rev"]) == ("inv:Category", 4)
    assert app(run_cli, "get", supplier, "--rev", "4", env=ENV).code == 0
    # its revisions of the hidden class answer as revisions that were never stored
    never = app(run_cli, "get", supplier, "--rev", "99", env=ENV)
    assert never.code == 1 and never.error["code"] == "entity.revision_not_found"
    seen = [never.envelope, current.envelope]
    for rev in ("1", "2", "3"):
        omitted = app(run_cli, "get", supplier, "--rev", rev, env=ENV)
        assert (omitted.code, omitted.error["code"]) == (1, "entity.revision_not_found")
        assert (
            omitted.error["message"].replace(f"revision {rev}", "revision 99")
            == (never.error["message"])
        )
        assert omitted.error["details"] == {**never.error["details"], "rev": int(rev)}
        seen.append(omitted.envelope)
        # the text form too
        text = run_cli("graph", "get", supplier, "--rev", rev, env=ENV)
        assert text.returncode == 1
        seen.append(text.stdout + text.stderr)
    # history lists the migration revision only, as a creation and without effects
    listed = app(run_cli, "history", supplier, env=ENV)
    assert listed.code == 0, listed.envelope
    assert [i["rev"] for i in listed.data["items"]] == [4]
    shown = listed.data["items"][0]
    assert shown.get("changed") is None
    assert not shown["migration"].get("renames") and not shown["migration"].get("steps")
    assert listed.envelope["page"]["total"] == 1
    seen.append(listed.envelope)
    seen.append(run_cli("graph", "history", supplier, env=ENV).stdout)
    # the feed: the creation for this reader is the migration revision
    data = feed(run_cli)
    assert [e["tx_seq"] for e in data["items"]] == [4]  # tx 1-3 hold revisions of the hidden class
    entities = data["items"][0]["entities"]
    assert [(i["id"], i["type"], i["rev"]) for i in entities] == [(supplier, "inv:Category", 4)]
    assert entities[0].get("changed") is None
    seen.append(data)
    seen.append(run_cli("graph", "changes", "--since", "tx:0", env=ENV).stdout)
    # an earlier state, when the entity was of the hidden class: absent as a whole
    for at in ("tx:1", "tx:3"):
        gone = app(run_cli, "get", supplier, "--as-of", at, env=ENV)
        assert (gone.code, gone.error["code"]) == (1, "entity.not_found")
        found = app(run_cli, "find", "--type", "inv:Category", "--as-of", at, env=ENV)
        assert found.code == 0 and found.data["items"] == [], found.envelope
        seen += [gone.envelope, found.envelope]
    ahead = app(run_cli, "get", supplier, "--as-of", "tx:4", env=ENV)
    assert ahead.code == 0 and ahead.data["entity"]["rev"] == 4
    # no old value and no hidden class name in any answer
    for answer in seen:
        rendered = answer if isinstance(answer, str) else json.dumps(answer)
        assert "Alpha" not in rendered and "Beta" not in rendered, rendered
        assert "Supplier" not in rendered, rendered
