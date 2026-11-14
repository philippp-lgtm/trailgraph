"""S: graph-admin ontology release (contracts B7.4, B7.12, B7.14, W.e; plan M7).

One scenario per refusal code plus the inventory version series.
"""

import shutil
from pathlib import Path

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init, write_documents
from support.app import ENV, app
from support.fixtures import ONTOLOGIES, compiled_model

from graph_cli.audit.verify import verify_stream
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM, graph_stream
from graph_cli.storage.files.audit_log import FileAuditLog

INVENTORY = ONTOLOGIES / "inventory"


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    return init(run_cli, isolated_env)


def create(run_cli: CliRunner, version: str = "1.0.0") -> str:
    outcome = admin(
        run_cli, "graph", "create", "example_graph", "--ontology", str(INVENTORY / version)
    )
    assert outcome.code == 0, outcome.envelope
    graph_id: str = outcome.data["id"]
    return graph_id


def release(run_cli: CliRunner, source: Path) -> Outcome:
    return admin(run_cli, "ontology", "release", "example_graph", str(source))


def variant(tmp_path: Path, base: str, version: str, *, summary: str | None = None) -> Path:
    """An authored copy of a fixture version under another version number."""
    text = (INVENTORY / base / "inventory.yaml").read_text()
    text = text.replace(f"  version: {base}\n", f"  version: {version}\n", 1)
    if summary is not None:
        text = text.replace("  summary: ", f"  summary: {summary} ", 1)
    target = tmp_path / f"{base}_as_{version}"
    target.mkdir()
    (target / "inventory.yaml").write_text(text)
    return target


def test_the_inventory_series(run_cli: CliRunner, root: Path) -> None:
    graph_id = create(run_cli)
    series = (("1.0.1", "patch"), ("1.1.0", "minor"), ("2.0.0", "major"), ("2.1.0", "minor"))
    for version, level in series:
        outcome = release(run_cli, INVENTORY / version)
        assert outcome.code == 0, outcome.envelope
        assert (outcome.data["released"], outcome.data["pinned"]) == (True, True)
        assert outcome.data["diff"]["level"] == level
        model = compiled_model("inventory", version)
        assert outcome.data["to"]["artifact_hash"] == model.artifact_hash
        assert outcome.envelope["graph"]["ontology"]["version"] == version
        assert (root / "graphs" / graph_id / "ontology" / version / "diff.json").is_file()
    shown = admin(run_cli, "graph", "show", "example_graph").data
    assert shown["releases"] == ["1.0.0", "1.0.1", "1.1.0", "2.0.0", "2.1.0"]
    assert shown["ontology"]["version"] == "2.1.0"
    audit = FileAuditLog(root)
    graph_report = verify_stream(
        audit.read_lines(graph_stream(graph_id)), stream=graph_stream(graph_id)
    )
    installation = verify_stream(audit.read_lines(INSTALLATION_STREAM), stream=INSTALLATION_STREAM)
    assert graph_report.findings == () and installation.findings == ()
    assert [e["actions"][0] for e in graph_report.events] == [
        "maint.graph_create",
        *["maint.ontology_release", "maint.ontology_pin"] * 4,
    ]
    assert len(installation.events) == 9  # create plus one companion per operation


def test_rerunning_a_pinned_release_changes_nothing(run_cli: CliRunner, root: Path) -> None:
    create(run_cli)
    assert release(run_cli, INVENTORY / "1.0.1").code == 0
    before = FileAuditLog(root).head(INSTALLATION_STREAM)
    outcome = release(run_cli, INVENTORY / "1.0.1")
    assert outcome.code == 0
    assert (outcome.data["released"], outcome.data["pinned"]) == (False, False)
    assert FileAuditLog(root).head(INSTALLATION_STREAM) == before


def test_a_release_seeds_the_alias_prefixes_of_added_classes(
    run_cli: CliRunner, root: Path
) -> None:
    """Classes new in a release get their display prefix; existing prefixes stay."""
    create(run_cli)
    before = admin(run_cli, "graph", "show", "example_graph").data["alias_prefixes"]
    outcome = release(run_cli, INVENTORY / "1.1.0")
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["alias_prefixes_added"] == {"inv:Supplier": "SUP"}
    after = admin(run_cli, "graph", "show", "example_graph").data["alias_prefixes"]
    assert after == {**before, "inv:Supplier": "SUP"}
    supplier = {"op": "create", "type": "inv:Supplier", "attrs": {"name": "Example supplier"}}
    write_documents(root, "example_graph", [{"ops": [supplier]}])
    found = app(run_cli, "find", "--type", "inv:Supplier", env=ENV)
    assert [item["alias"] for item in found.data["items"]] == ["SUP-0001"]


def supplier_as_vendor(tmp_path: Path, *, renamed: bool) -> Path:
    """Version 2.0.0 of 1.1.0 with the class Supplier now called Vendor (same prefix)."""
    text = (INVENTORY / "1.1.0" / "inventory.yaml").read_text()
    text = text.replace("  version: 1.1.0\n", "  version: 2.0.0\n", 1)
    marker = "\n    renamed_from: Supplier" if renamed else ""
    text = text.replace("  Supplier:\n", f"  Vendor:{marker}\n", 1)
    text = text.replace("range: Supplier", "range: Vendor", 1)
    target = tmp_path / ("renamed" if renamed else "replaced")
    target.mkdir()
    (target / "inventory.yaml").write_text(text)
    return target


def test_a_renamed_class_keeps_its_alias_prefix(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    """A class renamed with renamed_from takes over the prefix of its old name."""
    create(run_cli, "1.1.0")
    outcome = release(run_cli, supplier_as_vendor(tmp_path, renamed=True))
    assert outcome.code == 0, outcome.envelope
    assert outcome.warning_codes == []
    assert outcome.data["alias_prefixes_added"] == {"inv:Vendor": "SUP"}
    vendor = {"op": "create", "type": "inv:Vendor", "attrs": {"name": "Example vendor"}}
    write_documents(root, "example_graph", [{"ops": [vendor]}])
    found = app(run_cli, "find", "--type", "inv:Vendor", env=ENV)
    assert [item["alias"] for item in found.data["items"]] == ["SUP-0001"]


def vendor_variant(tmp_path: Path, name: str, renamed_from: str | None, *, keep: bool) -> Path:
    """Version 2.0.0 of 1.1.0 with a class Vendor naming ``renamed_from``; ``keep``: the
    class Supplier stays."""
    text = (INVENTORY / "1.1.0" / "inventory.yaml").read_text()
    text = text.replace("  version: 1.1.0\n", "  version: 2.0.0\n", 1)
    marker = "" if renamed_from is None else f"\n    renamed_from: {renamed_from}"
    if keep:
        vendor = f"  Vendor:{marker}\n    summary: A vendor.\n    display_prefix: VEN\n"
        text = text.replace("  Category:\n", vendor + "  Category:\n", 1)
    else:
        text = text.replace("  Supplier:\n", f"  Vendor:{marker}\n", 1)
        text = text.replace("range: Supplier", "range: Vendor", 1)
    target = tmp_path / name
    target.mkdir()
    (target / "inventory.yaml").write_text(text)
    return target


@pytest.mark.parametrize(
    ("renamed_from", "keep", "rule"),
    [("Missing", False, "dangling"), ("Supplier", True, "source_remains")],
)
def test_an_invalid_renamed_from_refuses_the_release(
    run_cli: CliRunner, root: Path, tmp_path: Path, renamed_from: str, keep: bool, rule: str
) -> None:
    create(run_cli, "1.1.0")
    source = vendor_variant(tmp_path, "invalid", renamed_from, keep=keep)
    outcome = release(run_cli, source)
    assert (outcome.code, outcome.error["code"]) == (1, "ontology.rename_invalid")
    details = outcome.error["details"]
    assert (details["from"], details["to"], details["count"]) == ("1.1.0", "2.0.0", 1)
    assert [(p["rule"], p["path"]) for p in details["problems"]] == [(rule, "classes.inv:Vendor")]
    shown = admin(run_cli, "graph", "show", "example_graph").data
    assert shown["releases"] == ["1.1.0"]
    assert shown["ontology"]["version"] == "1.1.0"


def test_diff_refuses_an_invalid_renamed_from(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create(run_cli, "1.1.0")
    source = vendor_variant(tmp_path, "invalid", "Missing", keep=False)
    against = admin(run_cli, "ontology", "diff", str(source), "--against", str(INVENTORY / "1.1.0"))
    graph = admin(run_cli, "ontology", "diff", str(source), "--graph", "example_graph")
    for outcome in (against, graph):
        assert (outcome.code, outcome.error["code"]) == (1, "ontology.rename_invalid")
        assert outcome.error["details"]["problems"][0]["rule"] == "dangling"
    valid = vendor_variant(tmp_path, "valid", "Supplier", keep=False)
    outcome = admin(run_cli, "ontology", "diff", str(valid), "--graph", "example_graph")
    assert outcome.code == 0
    assert ("classes.inv:Supplier", "rename") in {
        (c["path"], c["kind"]) for c in outcome.data["changes"]
    }


def test_a_leftover_renamed_from_is_a_warning(run_cli: CliRunner, root: Path) -> None:
    """2.1.0 still carries the renamed_from of 2.0.0: it releases and says so."""
    create(run_cli, "1.1.0")
    assert release(run_cli, INVENTORY / "2.0.0").warning_codes == []
    diffed = admin(
        run_cli, "ontology", "diff", str(INVENTORY / "2.1.0"), "--graph", "example_graph"
    )
    outcome = release(run_cli, INVENTORY / "2.1.0")
    for found in (diffed, outcome):
        assert found.code == 0, found.envelope
        stale = [w for w in found.warnings if w["code"] == "ontology.rename_stale"]
        assert len(stale) == 1
        assert stale[0]["details"]["count"] == 1
        problem = stale[0]["details"]["problems"][0]
        assert (problem["rule"], problem["path"]) == (
            "leftover",
            "classes.inv:Item.attributes.label",
        )


def test_a_class_moved_to_another_module_takes_its_alias_prefix(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    """renamed_from with a CURIE: a rename in the diff, and the prefix moves to the new class."""
    create(run_cli, "1.1.0")
    text = (INVENTORY / "1.1.0" / "inventory.yaml").read_text()
    text = text.replace("  version: 1.1.0\n", "  version: 2.0.0\n", 1)
    text = text.replace(
        "  summary: Physical items",
        '  imports:\n    - {module: suppliers, version: "^1.0"}\n  summary: Physical items',
        1,
    )
    start = text.index("  Supplier:\n")
    end = text.index("  Category:\n")
    text = text[:start] + text[end:]
    text = text.replace("range: Supplier", "range: sup:Supplier", 1)
    source = tmp_path / "moved"
    source.mkdir()
    (source / "inventory.yaml").write_text(text)
    (source / "suppliers.yaml").write_text(
        """format: graph_cli.ir/1
module: {id: suppliers, prefix: sup, version: 1.0.0, summary: Suppliers.}
classes:
  Supplier:
    summary: A company that delivers items.
    display_prefix: SUP
    renamed_from: inv:Supplier
    attributes:
      name: {summary: Name of the supplier., type: string, required: true}
"""
    )
    outcome = admin(
        run_cli, "ontology", "release", "example_graph", str(source), "--module", "inventory"
    )
    assert outcome.code == 0, outcome.envelope
    assert outcome.warning_codes == []
    assert ("classes.inv:Supplier", "rename") in {
        (c["path"], c["kind"]) for c in outcome.data["diff"]["changes"]
    }
    assert "classes.inv:Supplier" not in {
        c["path"] for c in outcome.data["diff"]["changes"] if c["kind"].startswith("remove")
    }
    assert outcome.data["alias_prefixes_added"] == {"sup:Supplier": "SUP"}
    prefixes = admin(run_cli, "graph", "show", "example_graph").data["alias_prefixes"]
    assert prefixes["sup:Supplier"] == "SUP"
    assert "inv:Supplier" not in prefixes
    supplier = {"op": "create", "type": "sup:Supplier", "attrs": {"name": "Example supplier"}}
    write_documents(root, "example_graph", [{"ops": [supplier]}])
    found = app(run_cli, "find", "--type", "sup:Supplier", env=ENV)
    assert [item["alias"] for item in found.data["items"]] == ["SUP-0001"]


def test_alias_prefix_taken_is_a_warning(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    """A new class whose prefix another class holds gets no aliases, and the release says so."""
    create(run_cli, "1.1.0")
    outcome = release(run_cli, supplier_as_vendor(tmp_path, renamed=False))
    assert outcome.code == 0, outcome.envelope
    assert outcome.warning_codes == ["admin.alias_prefix_taken"]
    assert outcome.envelope["warnings"][0]["details"] == {
        "type": "inv:Vendor",
        "prefix": "SUP",
        "used_by": "inv:Supplier",
    }
    assert outcome.data["alias_prefixes_added"] == {}


def test_release_exists(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    create(run_cli)
    other = variant(tmp_path, "1.0.0", "1.0.0", summary="Changed.")
    outcome = release(run_cli, other)
    assert (outcome.code, outcome.error["code"]) == (1, "admin.release_exists")


def test_version_bump_insufficient(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    create(run_cli)
    minor_as_patch = variant(tmp_path, "1.1.0", "1.0.2")
    outcome = release(run_cli, minor_as_patch)
    assert (outcome.code, outcome.error["code"]) == (1, "admin.version_bump_insufficient")
    assert outcome.error["details"] == {"from": "1.0.0", "to": "1.0.2", "level": "minor"}


def test_downgrades_are_insufficient(run_cli: CliRunner, root: Path) -> None:
    create(run_cli, "1.0.1")
    outcome = release(run_cli, INVENTORY / "1.0.0")
    assert (outcome.code, outcome.error["code"]) == (1, "admin.version_bump_insufficient")


def test_version_bump_excessive_is_a_warning(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create(run_cli)
    patch_as_minor = variant(tmp_path, "1.0.1", "1.1.0")
    outcome = release(run_cli, patch_as_minor)
    assert outcome.code == 0
    assert outcome.warning_codes == ["admin.version_bump_excessive"]


LOCATIONS = [
    {
        "op": "create",
        "type": "inv:Location",
        "local": "room",
        "attrs": {"name": "Room", "code": "R1"},
    },
    {
        "op": "create",
        "type": "inv:Location",
        "local": "shelf",
        "attrs": {"name": "Shelf", "code": "R1-S1"},
        "edges": [{"rel": "inv:within", "target": "@room"}],
    },
]
ITEM = {
    "op": "create",
    "type": "inv:Item",
    "attrs": {"name": "Hammer"},
    "edges": [{"rel": "inv:located_in", "target": "@shelf"}],
}


def test_migration_required(run_cli: CliRunner, root: Path) -> None:
    """B7.14: data with error findings under the new major refuses the release."""
    create(run_cli, "1.1.0")
    write_documents(root, "example_graph", [{"ops": [*LOCATIONS, ITEM]}])
    outcome = release(run_cli, INVENTORY / "2.0.0")
    assert (outcome.code, outcome.error["code"]) == (1, "admin.migration_required")
    details = outcome.error["details"]
    assert details["seq"] == 1
    assert details["counts"] == {
        "data.referential.cardinality_out": 1,  # in_category tightened to 1..n
        "data.structural.missing_required": 1,  # label
        "data.structural.unknown_attribute": 1,  # name, renamed to label
    }
    assert {f["code"] for f in details["findings"]} == set(details["counts"])
    show = admin(run_cli, "graph", "show", "example_graph")
    assert show.data["ontology"]["version"] == "1.1.0"


def test_major_release_on_data_that_checks_clean(run_cli: CliRunner, root: Path) -> None:
    """B7.14: no migration needed when check_graph finds no errors under the new major."""
    create(run_cli, "1.1.0")
    write_documents(root, "example_graph", [{"ops": LOCATIONS}])
    outcome = release(run_cli, INVENTORY / "2.0.0")
    assert outcome.code == 0, outcome.envelope
    assert outcome.data["pinned"] is True and outcome.data["to"]["version"] == "2.0.0"


def test_archived_graph(run_cli: CliRunner, root: Path) -> None:
    graph_id = create(run_cli)
    manifest = root / "graphs" / graph_id / "manifest.toml"
    manifest.write_text(manifest.read_text().replace('state = "active"', 'state = "archived"'))
    outcome = release(run_cli, INVENTORY / "1.0.1")
    assert (outcome.code, outcome.error["code"]) == (1, "graph.archived")


def test_a_module_without_its_own_bump_is_a_warning(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    base = ONTOLOGIES / "company" / "1.1.0"
    created = admin(
        run_cli, "graph", "create", "example_graph", "--ontology", str(base), "--module", "company"
    )
    assert created.code == 0, created.envelope
    source = tmp_path / "company"
    shutil.copytree(base, source)
    main, people = source / "company.yaml", source / "people.yaml"
    main.write_text(main.read_text().replace("version: 1.1.0", "version: 1.1.1"))
    people.write_text(people.read_text().replace("company knows", "company knows of"))
    outcome = admin(
        run_cli, "ontology", "release", "example_graph", str(source), "--module", "company"
    )
    assert (outcome.code, outcome.data["pinned"]) == (0, True)
    assert outcome.warning_codes == ["admin.module_bump_insufficient"]


# ---- installed policies against the new model (write allow lists plan W8) -----------------

TERM_POLICY = """\
schema: graph_cli.policy/1
id: installation
grants:
  - entrypoint: app
    graphs: [example_graph]
    level: read
    writes:
      inv:Item:
        create: [name, quantity]
        update: [name, note]
        link: [inv:located_in, inv:supplied_by]
        transition: [status]
      inv:Supplier: {create: true}
      inv:Category: {update: [name]}
no_project: {"*": write}
"""
TERM_DENY = """\
schema: graph_cli.policy/1
id: graph
deny:
  - {id: no_note, action: [write.update], type: [inv:Item], attribute: [note]}
  - {id: no_category_link, action: [write.link], type: [inv:Item], relation: [inv:in_category]}
"""
PLAIN_POLICY = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {entrypoint: app, graphs: [example_graph], level: write}
no_project: {"*": write}
"""


def install_terms(run_cli: CliRunner, tmp_path: Path, installation: str, graph: str = "") -> None:
    from support.policies import write_policy

    path = write_policy(tmp_path / "policies", "installation.yaml", installation)
    applied = admin(run_cli, "policy", "apply", str(path), "--reason", "test policy")
    assert applied.code == 0, applied.envelope
    if graph:
        path = write_policy(tmp_path / "policies", "graph.yaml", graph)
        args = ("policy", "apply", str(path), "--graph", "example_graph")
        assert admin(run_cli, *args, "--reason", "test policy").code == 0


def authored(tmp_path: Path, edits: list[tuple[str, str]], version: str = "2.0.0") -> Path:
    """The 1.1.0 inventory under another version with text replacements (each must occur)."""
    text = (INVENTORY / "1.1.0" / "inventory.yaml").read_text()
    text = text.replace("  version: 1.1.0\n", f"  version: {version}\n", 1)
    for old, new in edits:
        assert old in text, old
        text = text.replace(old, new, 1)
    target = tmp_path / f"edited_{version}"
    target.mkdir(exist_ok=True)
    (target / "inventory.yaml").write_text(text)
    return target


def policy_warnings(outcome: Outcome) -> list[tuple[str, dict[str, object]]]:
    return [
        (w["code"], w["details"]) for w in outcome.warnings if w["code"].startswith("admin.policy_")
    ]


def test_a_release_that_renames_an_attribute_a_policy_names_warns(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create(run_cli, "1.1.0")
    install_terms(run_cli, tmp_path, TERM_POLICY, TERM_DENY)
    candidate = admin(
        run_cli, "ontology", "diff", str(INVENTORY / "2.0.0"), "--graph", "example_graph"
    )
    outcome = release(run_cli, INVENTORY / "2.0.0")  # 2.0.0 renames inv:Item.name to label
    assert outcome.code == 0, outcome.envelope
    expected = [
        (
            "admin.policy_unknown_term",
            {"graph": "example_graph", "type": "inv:Item", "term": "name", "kind": "attribute"},
        )
    ]
    assert policy_warnings(outcome) == expected
    assert policy_warnings(candidate) == expected  # ontology diff --graph shows it ahead
    message = next(w for w in outcome.warnings if w["code"] == "admin.policy_unknown_term")
    assert "fail closed" in message["message"] and "2.0.0" in message["message"]


def test_a_release_that_drops_a_type_and_a_relation_a_policy_names_warns(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create(run_cli, "1.1.0")
    install_terms(run_cli, tmp_path, TERM_POLICY, TERM_DENY)
    supplier = "  Supplier:\n    summary: A company that delivers items.\n"
    text = (INVENTORY / "1.1.0" / "inventory.yaml").read_text()
    block = text[text.index(supplier) : text.index("  Category:\n")]
    relation = text[text.index("  supplied_by:\n") : text.index("  in_category:\n")]
    source = authored(tmp_path, [(block, ""), (relation, "")])
    outcome = release(run_cli, source)
    assert outcome.code == 0, outcome.envelope
    assert policy_warnings(outcome) == [  # in the order of the types
        (
            "admin.policy_unknown_term",
            {
                "graph": "example_graph",
                "type": "inv:Item",
                "term": "inv:supplied_by",
                "kind": "relation",
            },
        ),
        ("admin.policy_unknown_type", {"graph": "example_graph", "type": "inv:Supplier"}),
    ]


def test_a_release_that_gives_a_granted_type_subclasses_warns(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    """A write grant is an exact rule on its type: once the type has subclasses it misses
    their entities. The same code as for a type that is gone, with the subclass message."""
    create(run_cli, "1.1.0")
    install_terms(run_cli, tmp_path, TERM_POLICY, TERM_DENY)
    subclass = (
        "  Tag:\n    summary: A kind of category.\n    is_a: Category\n    display_prefix: TAG\n"
    )
    source = authored(tmp_path, [("relations:\n", subclass + "\nrelations:\n")])
    outcome = release(run_cli, source)
    assert outcome.code == 0, outcome.envelope
    ((code, details),) = policy_warnings(outcome)
    assert (code, details) == (
        "admin.policy_unknown_type",
        {"graph": "example_graph", "type": "inv:Category"},
    )
    text = next(w["message"] for w in outcome.warnings if w["code"] == code)
    assert "subclasses" in text and "misses" in text


READ_POLICY = """\
schema: graph_cli.policy/1
id: installation
roles:
  reporting: {description: "Reporting: categories and suppliers"}
grants:
  - entrypoint: server
    principal: {role: reporting}
    graphs: [example_graph]
    level: read
    reads: [inv:Category, inv:Supplier]
no_project: {"*": read}
"""
READ_DENY = """\
schema: graph_cli.policy/1
id: graph
types:
  inv:Item: none
deny:
  - {id: hide_locations, action: [read.get], type: [inv:Location]}
"""


def test_a_release_that_gives_a_read_allow_list_type_subclasses_warns(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    """A ``reads`` entry is an exact rule: the subclass of its type stays hidden (fail closed)."""
    create(run_cli, "1.1.0")
    install_terms(run_cli, tmp_path, READ_POLICY, READ_DENY)
    subclass = (
        "  Tag:\n    summary: A kind of category.\n    is_a: Category\n    display_prefix: TAG\n"
    )
    outcome = release(run_cli, authored(tmp_path, [("relations:\n", subclass + "\nrelations:\n")]))
    assert outcome.code == 0, outcome.envelope
    ((code, details),) = policy_warnings(outcome)
    assert (code, details) == (
        "admin.policy_unknown_type",
        {"graph": "example_graph", "type": "inv:Category"},
    )
    text = next(w["message"] for w in outcome.warnings if w["code"] == code)
    assert "read allow list (reads)" in text and "subclasses stay hidden" in text


def test_a_release_that_gives_a_hidden_type_subclasses_warns(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    """A read deny names its type exactly: the subclass of the hidden type is visible."""
    create(run_cli, "1.1.0")
    install_terms(run_cli, tmp_path, READ_POLICY, READ_DENY)
    subclass = "  Shelf:\n    summary: A shelf.\n    is_a: Location\n    display_prefix: SHELF\n"
    source = authored(tmp_path, [("relations:\n", subclass + "\nrelations:\n")])
    candidate = admin(run_cli, "ontology", "diff", str(source), "--graph", "example_graph")
    outcome = release(run_cli, source)
    assert outcome.code == 0, outcome.envelope
    expected = [("admin.policy_unknown_type", {"graph": "example_graph", "type": "inv:Location"})]
    assert policy_warnings(outcome) == expected
    assert policy_warnings(candidate) == expected
    text = next(w["message"] for w in outcome.warnings if w["code"] == expected[0][0])
    assert "read deny" in text and "visible" in text


def test_a_release_that_drops_a_type_a_read_allow_list_names_warns(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create(run_cli, "1.1.0")
    install_terms(run_cli, tmp_path, READ_POLICY, READ_DENY)
    supplier = "  Supplier:\n    summary: A company that delivers items.\n"
    text = (INVENTORY / "1.1.0" / "inventory.yaml").read_text()
    block = text[text.index(supplier) : text.index("  Category:\n")]
    relation = text[text.index("  supplied_by:\n") : text.index("  in_category:\n")]
    outcome = release(run_cli, authored(tmp_path, [(block, ""), (relation, "")]))
    assert outcome.code == 0, outcome.envelope
    assert policy_warnings(outcome) == [
        ("admin.policy_unknown_type", {"graph": "example_graph", "type": "inv:Supplier"})
    ]
    text = outcome.warnings[0]["message"]
    assert "read allow list (reads)" in text and "lets nobody see the type" in text


def test_a_release_without_term_rules_installed_warns_nothing(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    create(run_cli, "1.1.0")
    candidate = admin(
        run_cli, "ontology", "diff", str(INVENTORY / "2.0.0"), "--graph", "example_graph"
    )
    assert policy_warnings(candidate) == []
    install_terms(run_cli, tmp_path, PLAIN_POLICY)  # a policy, but no writes grant, no term rule
    outcome = release(run_cli, INVENTORY / "2.0.0")
    assert outcome.code == 0, outcome.envelope
    assert policy_warnings(outcome) == []
