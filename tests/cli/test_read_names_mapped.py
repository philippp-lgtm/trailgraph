"""S: old revisions under current names after a migration (migration plan M7).

The inventory series 1.1.0 -> 2.0.0 renames ``inv:Item.name`` to ``label`` and maps the removed
condition ``worn`` to ``good``; a 3.0.0 built here renames ``inv:Item`` to ``inv:Article``, the
attribute again to ``title`` and ``located_in`` to ``stored_in``. ``history``, ``get --rev`` and
``--as-of`` show every revision under the pinned names, the current revisions are never mapped,
old names on input fail with their current name, and read limits judge the mapped type.
"""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init, write_documents
from support.app import ENV, app
from support.moved_supplier import INVENTORY, moved_source, write_folder
from support.policies import write_policy

MIGRATION = """\
format: graph_cli.migration/1
from: 1.1.0
summary: The condition worn is gone; those items count as good.
steps:
  - map_values: {type: inv:Item, attribute: condition, values: {worn: good}}
"""
THIRD = """\
format: graph_cli.migration/1
from: 2.0.0
summary: Items are articles now.
"""
HIDE_SUPPLIERS = """\
schema: graph_cli.policy/1
id: graph
types:
  "inv:Supplier": none
  "sup:Supplier": none
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


def item(local: str, name: str, condition: str, *extra: dict[str, str]) -> dict[str, Any]:
    return {
        "op": "create",
        "type": "inv:Item",
        "local": local,
        "attrs": {"name": name, "condition": condition},
        "edges": [
            {"rel": "inv:located_in", "target": "@room"},
            {"rel": "inv:in_category", "target": "@tools"},
            *extra,
        ],
    }


SEED = [*PLACES, item("hammer", "Hammer", "worn"), item("saw", "Saw", "good")]


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    return init(run_cli, isolated_env)


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


def third(tmp_path: Path) -> Path:
    """3.0.0: ``inv:Item`` -> ``inv:Article``, ``label`` -> ``title``, ``located_in`` ->
    ``stored_in``."""
    text = (INVENTORY / "2.0.0" / "inventory.yaml").read_text()
    text = text.replace("  version: 2.0.0\n", "  version: 3.0.0\n", 1)
    text = text.replace("  Item:\n", "  Article:\n    renamed_from: Item\n", 1)
    text = text.replace("      label:\n", "      title:\n", 1)
    text = text.replace("renamed_from: name", "renamed_from: label", 1)
    text = text.replace("  located_in:\n", "  stored_in:\n    renamed_from: located_in\n", 1)
    text = text.replace("rel: located_in", "rel: stored_in")
    for old in ("domain: Item", "range: Item", "scope: Item"):
        text = text.replace(old, old.replace("Item", "Article"))
    target = tmp_path / "v3"
    target.mkdir()
    (target / "inventory.yaml").write_text(text)
    (target / "migration.yaml").write_text(THIRD)
    return target


def release(run_cli: CliRunner, source: Path, *extra: str) -> Outcome:
    outcome = admin(run_cli, "ontology", "release", "example_graph", str(source), *extra)
    assert outcome.code == 0, outcome.envelope
    return outcome


def hammer_id(run_cli: CliRunner, type_: str = "inv:Item") -> str:
    found = app(run_cli, "find", "--type", type_, "--sort", "alias", env=ENV)
    assert found.code == 0, found.envelope
    ids = [i["id"] for i in found.data["items"]]
    return str(ids[0])


def update(root: Path, entity: str, attrs: dict[str, Any], seed: int) -> None:
    write_documents(
        root, "example_graph", [{"ops": [{"op": "update", "id": entity, "set": attrs}]}], seed=seed
    )


def migrated(run_cli: CliRunner, root: Path, tmp_path: Path) -> str:
    """The seed at 1.1.0, the hammer renamed once, then the release to 2.0.0 (tx 3)."""
    create(run_cli)
    write_documents(root, "example_graph", [{"ops": SEED}])
    hammer = hammer_id(run_cli)
    update(root, hammer, {"name": "Big hammer"}, seed=14)
    release(run_cli, second(tmp_path), "--reason", "rename name to label")
    return hammer


def test_history_shows_every_revision_under_current_names(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    hammer = migrated(run_cli, root, tmp_path)
    update(root, hammer, {"label": "Bigger hammer"}, seed=15)
    shown = app(run_cli, "history", hammer, env=ENV)
    assert shown.code == 0, shown.envelope
    items = {i["rev"]: i for i in shown.data["items"]}
    assert sorted(items) == [1, 2, 3, 4]
    assert items[2]["changed"]["attrs"] == ["label"]  # stored as ``name``
    assert items[4]["changed"]["attrs"] == ["label"]
    marked = items[3]
    assert marked["reason"] == "rename name to label"
    assert marked["changed"] == {
        "attrs": [],
        "edges": {"added": [], "removed": [], "changed": []},
        "record": [],
    }
    assert marked["migration"] == {
        "from": "1.1.0",
        "to": "2.0.0",
        "summary": "The condition worn is gone; those items count as good.",
        "renames": [
            {"kind": "rename_attribute", "class": "inv:Item", "from": "name", "to": "label"}
        ],
        "steps": [
            {
                "kind": "map_values",
                "type": "inv:Item",
                "attribute": "condition",
                "values": {"worn": "good"},
                "index": 0,
            }
        ],
    }
    assert all("migration" not in items[r] for r in (1, 2, 4))
    saw = next(
        i
        for i in app(run_cli, "find", "--type", "inv:Item", env=ENV).data["items"]
        if i["id"] != hammer
    )
    saw_history = app(run_cli, "history", saw["id"], env=ENV).data["items"]
    # the saw was good already: only the rename applied to it
    assert [s["kind"] for s in saw_history[0]["migration"]["steps"]] == []
    assert [r["to"] for r in saw_history[0]["migration"]["renames"]] == ["label"]


def test_get_rev_maps_a_past_revision_and_never_the_current_one(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    hammer = migrated(run_cli, root, tmp_path)
    first = app(run_cli, "get", hammer, "--rev", "1", "--full", env=ENV)
    assert first.code == 0, first.envelope
    entity = first.data["entity"]
    assert entity["attrs"]["label"] == "Hammer" and "name" not in entity["attrs"]
    assert entity["attrs"]["condition"] == "good"  # the removed value is mapped as well
    assert entity["names_mapped"] is True
    assert entity["ontology_version"] == "1.1.0"  # what it was written under
    lean = app(run_cli, "get", hammer, "--rev", "1", env=ENV).data["entity"]
    assert "names_mapped" not in lean
    current = app(run_cli, "get", hammer, "--full", env=ENV).data["entity"]
    assert "names_mapped" not in current and current["ontology_version"] == "2.0.0"
    as_current = app(run_cli, "get", hammer, "--rev", "3", "--full", env=ENV).data["entity"]
    assert as_current == current
    # the recomputed hash is the hash of what is shown: rev 2 and the migration revision differ
    # only in the version, so after mapping they hold the same content
    second_rev = app(run_cli, "get", hammer, "--rev", "2", "--full", env=ENV).data["entity"]
    assert second_rev["content_hash"] == current["content_hash"]
    assert second_rev["rev"] == 2 and second_rev["tx_seq"] == 2


def test_as_of_answers_with_current_names(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    hammer = migrated(run_cli, root, tmp_path)
    found = app(
        run_cli, "find", "--type", "inv:Item", "--where", "label=Hammer", "--as-of", "tx:1", env=ENV
    )
    assert found.code == 0, found.envelope
    assert [i["id"] for i in found.data["items"]] == [hammer]
    assert found.data["items"][0]["attrs"]["condition"] == "good"
    [warning] = [w for w in found.warnings if w["code"] == "temporal.as_of_read"]
    assert warning["details"]["names_mapped"] is True
    assert warning["details"]["ontology_version"] == "1.1.0"
    assert "names are mapped to those of 2.0.0" in warning["message"]
    queried = app(
        run_cli,
        "query",
        "MATCH (i:Item)-[:located_in]->(l:Location) WHERE i.label = 'Hammer' RETURN i.label AS x",
        "--as-of",
        "tx:1",
        env=ENV,
    )
    assert queried.code == 0, queried.envelope
    assert queried.data["items"] == [{"x": "Hammer"}]
    near = app(run_cli, "neighbors", hammer, "--rel", "located_in", "--as-of", "tx:1", env=ENV)
    assert near.code == 0, near.envelope
    assert [n["entity"]["attrs"]["name"] for n in near.data["items"]] == ["Room"]
    room = near.data["items"][0]["entity"]["id"]
    back = app(run_cli, "neighbors", room, "--direction", "in", "--as-of", "tx:2", env=ENV)
    assert {n["entity"]["attrs"]["label"] for n in back.data["items"]} == {"Big hammer", "Saw"}
    # the head state is not mapped and says nothing about names
    head = app(run_cli, "find", "--type", "inv:Item", "--as-of", "tx:3", env=ENV)
    [plain] = [w for w in head.warnings if w["code"] == "temporal.as_of_read"]
    assert "names_mapped" not in plain["details"] and "mapped" not in plain["message"]


def test_old_names_fail_with_their_current_name(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    migrated(run_cli, root, tmp_path)
    old = app(run_cli, "find", "--type", "inv:Item", "--where", "name=Hammer", env=ENV)
    assert (old.code, old.error["code"]) == (1, "term.unknown")
    assert "name was renamed to label (on inv:Item) in 2.0.0" in old.error["message"]
    assert old.error["details"]["renamed"] == [
        {
            "from": "name",
            "to": "label",
            "steps": [{"to": "label", "in": "2.0.0", "class": "inv:Item"}],
        }
    ]
    query = app(run_cli, "query", "MATCH (i:Item) RETURN i.name", "--as-of", "tx:1", env=ENV)
    assert query.error["code"] == "term.unknown"
    assert "renamed to label" in query.error["message"]
    written = app(run_cli, "create", "inv:Item", "--set", "name=Drill", env=ENV)
    assert written.error["code"] == "write.invalid"
    [finding] = [
        f
        for f in written.error["details"]["findings"]
        if f["code"] == "data.structural.unknown_attribute"
    ]
    assert finding["hint"] == "name was renamed to label (on inv:Item) in 2.0.0"
    unknown = app(run_cli, "find", "--type", "inv:Item", "--where", "nothing=1", env=ENV)
    assert unknown.error["code"] == "term.unknown" and "renamed" not in unknown.error["details"]


def test_two_majors_compose(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    hammer = migrated(run_cli, root, tmp_path)
    release(run_cli, third(tmp_path))
    first = app(run_cli, "get", hammer, "--rev", "1", "--full", env=ENV).data["entity"]
    assert first["type"] == "inv:Article" and first["attrs"]["title"] == "Hammer"
    assert {e["rel"] for e in first["edges"]} == {"inv:stored_in", "inv:in_category"}
    assert first["ontology_version"] == "1.1.0" and first["names_mapped"] is True
    shown = app(run_cli, "history", hammer, env=ENV).data["items"]
    marks = {i["rev"]: i["migration"] for i in shown if "migration" in i}
    assert sorted(marks) == [3, 4]
    assert (marks[4]["from"], marks[4]["to"]) == ("2.0.0", "3.0.0")
    assert {r["kind"] for r in marks[4]["renames"]} == {
        "rename_class",
        "rename_attribute",
        "rename_relation",
    }
    found = app(
        run_cli,
        "find",
        "--type",
        "inv:Article",
        "--where",
        "title=Hammer",
        "--as-of",
        "tx:1",
        env=ENV,
    )
    assert [i["id"] for i in found.data["items"]] == [hammer]
    old = app(run_cli, "find", "--type", "inv:Item", env=ENV)
    assert "inv:Item was renamed to inv:Article in 3.0.0" in old.error["message"]
    older = app(run_cli, "find", "--type", "inv:Article", "--fields", "name", env=ENV)
    assert (
        "name was renamed to label (on inv:Item) in 2.0.0, then to title (on inv:Article) in 3.0.0"
        in older.error["message"]
    )


@pytest.mark.parametrize("policy", [True, False])
def test_a_hidden_type_stays_hidden_under_its_mapped_name(
    run_cli: CliRunner, root: Path, tmp_path: Path, policy: bool
) -> None:
    create(run_cli)
    supplier = {"op": "create", "type": "inv:Supplier", "local": "s", "attrs": {"name": "Acme"}}
    seed = [
        *PLACES,
        supplier,
        item("drill", "Drill", "good", {"rel": "inv:supplied_by", "target": "@s"}),
    ]
    write_documents(root, "example_graph", [{"ops": seed}])
    drill = hammer_id(run_cli)
    if policy:
        path = write_policy(tmp_path / "policies", "hide.yaml", HIDE_SUPPLIERS)
        applied = admin(
            run_cli, "policy", "apply", str(path), "--graph", "example_graph", "--reason", "test"
        )
        assert applied.code == 0, applied.envelope
    admin_release = admin(
        run_cli,
        "ontology",
        "release",
        "example_graph",
        str(moved_source(tmp_path, migration=True)),
        "--module",
        "inventory",
    )
    assert admin_release.code == 0, admin_release.envelope
    seen = app(run_cli, "find", "--type", "sup:Supplier", "--as-of", "tx:1", env=ENV)
    edges = app(run_cli, "get", drill, "--as-of", "tx:1", env=ENV).data["entity"]["edges"]
    if policy:
        assert seen.error["code"] == "term.unknown" and "renamed" not in seen.error["details"]
        assert {e["rel"] for e in edges} == {"inv:located_in", "inv:in_category"}
        old = app(run_cli, "find", "--type", "inv:Supplier", env=ENV)
        assert "renamed" not in old.error["details"]  # the new name is hidden too
    else:
        assert seen.code == 0, seen.envelope
        assert [(i["type"], i["alias"]) for i in seen.data["items"]] == [
            ("sup:Supplier", "SUP-0001")
        ]
        assert "inv:supplied_by" in {e["rel"] for e in edges}
        old = app(run_cli, "find", "--type", "inv:Supplier", env=ENV)
        assert "inv:Supplier was renamed to sup:Supplier in 2.0.0" in old.error["message"]


def test_a_graph_without_renames_reads_as_before(run_cli: CliRunner, root: Path) -> None:
    """A minor release: nothing is mapped and nothing says so."""
    outcome = admin(
        run_cli, "graph", "create", "example_graph", "--ontology", str(INVENTORY / "1.0.0")
    )
    assert outcome.code == 0, outcome.envelope
    write_documents(root, "example_graph", [{"ops": PLACES}])
    assert (
        admin(run_cli, "ontology", "release", "example_graph", str(INVENTORY / "1.1.0")).code == 0
    )
    room = app(run_cli, "find", "--type", "inv:Location", env=ENV).data["items"][0]["id"]
    write_documents(
        root,
        "example_graph",
        [{"ops": [{"op": "update", "id": room, "set": {"name": "Hall"}}]}],
        seed=14,
    )
    old = app(run_cli, "get", room, "--rev", "1", "--full", env=ENV).data["entity"]
    assert "names_mapped" not in old and old["attrs"]["name"] == "Room"
    past = app(run_cli, "find", "--type", "inv:Location", "--as-of", "tx:1", env=ENV)
    [warning] = [w for w in past.warnings if w["code"] == "temporal.as_of_read"]
    assert warning["message"] == (
        "read as of tx:1; the graph is at tx:2; that state was written under ontology 1.0.0"
    )
    assert set(warning["details"]) == {"as_of", "seq", "head_seq", "ontology_version"}


def test_convert_is_not_mapped(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    """A plain string where the model now has ``lang_string``: old revisions keep it, and
    reads take it (filters compare its text)."""
    outcome = admin(
        run_cli,
        "graph",
        "create",
        "example_graph",
        "--ontology",
        str(INVENTORY / "1.1.0"),
        "--language",
        "en",
    )
    assert outcome.code == 0, outcome.envelope
    write_documents(root, "example_graph", [{"ops": PLACES}])
    text = (INVENTORY / "1.1.0" / "inventory.yaml").read_text()
    text = text.replace("  version: 1.1.0\n", "  version: 2.0.0\n", 1)
    start = text.index("  Location:\n")
    head, rest = text[:start], text[start:]
    rest = rest.replace("        type: string\n", "        type: lang_string\n", 1)
    source = tmp_path / "lang"
    source.mkdir()
    (source / "inventory.yaml").write_text(head + rest)
    (source / "migration.yaml").write_text(
        "format: graph_cli.migration/1\nfrom: 1.1.0\nsummary: Location names get a language.\n"
        "steps:\n  - convert: {type: inv:Location, attribute: name, to: lang_string}\n"
    )
    release(run_cli, source)
    room = app(run_cli, "find", "--type", "inv:Location", env=ENV).data["items"][0]
    assert room["attrs"]["name"] == {"lang": "en", "value": "Room"}
    old = app(run_cli, "get", room["id"], "--rev", "1", "--full", env=ENV).data["entity"]
    assert old["attrs"]["name"] == "Room" and "names_mapped" not in old
    past = app(
        run_cli,
        "find",
        "--type",
        "inv:Location",
        "--where",
        "name=Room",
        "--as-of",
        "tx:1",
        env=ENV,
    )
    assert past.code == 0, past.envelope
    assert [i["attrs"]["name"] for i in past.data["items"]] == ["Room"]


def test_publish_names_the_current_name_of_an_old_curie(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    from support.moved_supplier import ACTION, CURIE_FUNCTION

    create(run_cli)
    admin_release = admin(
        run_cli,
        "ontology",
        "release",
        "example_graph",
        str(moved_source(tmp_path, migration=True)),
        "--module",
        "inventory",
    )
    assert admin_release.code == 0, admin_release.envelope
    functions = write_folder(tmp_path, "functions", {"suppliers_exact": CURIE_FUNCTION})
    refused = admin(run_cli, "graph", "functions", "publish", "example_graph", str(functions))
    assert (refused.code, refused.error["code"]) == (1, "admin.functions_invalid")
    [problem] = refused.error["details"]["problems"]
    assert "inv:Supplier was renamed to sup:Supplier in 2.0.0" in problem["message"]
    assert problem["details"]["renamed"][0]["to"] == "sup:Supplier"
    assert "renamed to sup:Supplier" in refused.error["message"]
    actions = write_folder(tmp_path, "actions", {"rename_supplier": ACTION})
    blocked = admin(run_cli, "graph", "actions", "publish", "example_graph", str(actions))
    assert blocked.error["code"] == "admin.actions_invalid"
    hinted = [p for p in blocked.error["details"]["problems"] if "renamed" in p["details"]]
    assert hinted and "renamed to sup:Supplier in 2.0.0" in hinted[0]["message"]


RETYPE = """\
format: graph_cli.migration/1
from: 1.1.0
summary: Suppliers are categories now.
steps:
  - retype: {from: inv:Supplier, to: inv:Category}
"""
HIDE_SUPPLIER_CLASS = """\
schema: graph_cli.policy/1
id: graph
types:
  "inv:Supplier": none
"""


def test_a_retype_out_of_a_hidden_type_does_not_name_that_type(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    """``inv:Supplier`` stays in the model and is hidden; its entity was retyped to the visible
    ``inv:Category``. The history of that entity shows the migration, but not what it was."""
    create(run_cli)
    supplier = {"op": "create", "type": "inv:Supplier", "attrs": {"name": "Acme"}}
    write_documents(root, "example_graph", [{"ops": [supplier]}])
    source = tmp_path / "retyped"
    source.mkdir()
    text = (INVENTORY / "1.1.0" / "inventory.yaml").read_text()
    (source / "inventory.yaml").write_text(
        text.replace("  version: 1.1.0\n", "  version: 2.0.0\n", 1)
    )
    (source / "migration.yaml").write_text(RETYPE)
    released = admin(run_cli, "ontology", "release", "example_graph", str(source))
    assert released.code == 0, released.envelope
    entity = app(run_cli, "find", "--type", "inv:Category", env=ENV).data["items"][0]["id"]
    seen = app(run_cli, "history", entity, env=ENV)
    marked = next(i["migration"] for i in seen.data["items"] if "migration" in i)
    assert [s["kind"] for s in marked["steps"]] == ["retype"]  # the reader may see both types
    assert marked["steps"][0]["from"] == "inv:Supplier"
    path = write_policy(tmp_path / "policies", "hide.yaml", HIDE_SUPPLIER_CLASS)
    applied = admin(
        run_cli, "policy", "apply", str(path), "--graph", "example_graph", "--reason", "test"
    )
    assert applied.code == 0, applied.envelope
    limited = app(run_cli, "history", entity, env=ENV)
    assert limited.code == 0, limited.envelope
    marks = [i["migration"] for i in limited.data["items"] if "migration" in i]
    assert len(marks) == 1
    assert (marks[0]["renames"], marks[0]["steps"]) == ([], [])  # nothing of what changed
    assert "inv:Supplier" not in json.dumps(limited.envelope)
