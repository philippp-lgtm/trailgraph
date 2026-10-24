"""S: graph create, update, link and unlink on both fixtures (plan A7; contracts B3, B3.10,
B3.11, B3.16, B3.18). Role-based: values come from the compiled model, no type is named."""

import json
from pathlib import Path

import pytest
from harness import CliRunner
from support.admin import Outcome, init
from support.app import ENV, app, cli_create, codes, create_args, create_graph
from support.fixtures import TOY_ONTOLOGIES, compiled_model
from support.roles import Roles, load_roles
from support.services import OpsFactory


@pytest.fixture(params=TOY_ONTOLOGIES)
def ontology(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str], ontology: str) -> Path:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", ontology)
    return root


@pytest.fixture
def roles(ontology: str) -> Roles:
    return load_roles(ontology)


@pytest.fixture
def factory(ontology: str) -> OpsFactory:
    return OpsFactory(compiled_model(ontology))


def string_attr(factory: OpsFactory, cls: str) -> str:
    return next(n for n, v in factory.attrs(cls).items() if isinstance(v, str))


def test_create_and_get(run_cli: CliRunner, root: Path, roles: Roles, factory: OpsFactory) -> None:
    cls = roles.term("lifecycle_type")
    ids = cli_create(run_cli, factory, cls)
    shown = app(run_cli, "get", ids["x"], env=ENV)
    assert shown.data["entity"]["type"] == cls and shown.data["entity"]["rev"] == 1
    assert shown.envelope["graph"]["seq"] == len(ids)
    text = run_cli("graph", "get", ids["x"], env=ENV)
    assert text.returncode == 0 and text.stderr == ""


def test_set_is_coerced_by_datatype(
    run_cli: CliRunner, root: Path, roles: Roles, factory: OpsFactory
) -> None:
    cls = roles.term("lifecycle_type")
    model = factory.model
    typed = [
        (name, model.value_base(a.kind, a.type))
        for name, a in sorted(model.classes[cls].attributes.items())
        if model.value_base(a.kind, a.type) in ("integer", "date")
        and a.mutability in ("content", "operational")
    ]
    if not typed:
        pytest.skip("the lifecycle type has no integer or date attribute")
    name, base = typed[0]
    ids = cli_create(run_cli, factory, cls)
    good = "3" if base == "integer" else "2026-03-04"
    updated = app(run_cli, "update", ids["x"], "--set", f"{name}={good}", env=ENV)
    assert updated.code == 0, updated.envelope
    value = app(run_cli, "get", ids["x"], env=ENV).data["entity"]["attrs"][name]
    assert value == (3 if base == "integer" else good)
    bad = app(run_cli, "update", ids["x"], "--set", f"{name}=not-a-value", env=ENV)
    assert (bad.code, bad.error["code"]) == (1, "write.invalid")
    assert codes(bad.error["details"]["findings"]) == ["data.structural.datatype_mismatch"]


def test_expect_conflict(run_cli: CliRunner, root: Path, roles: Roles, factory: OpsFactory) -> None:
    cls = roles.term("lifecycle_type")
    ids = cli_create(run_cli, factory, cls)
    attr = string_attr(factory, cls)
    stale = app(run_cli, "update", ids["x"], "--set", f"{attr}=new", "--expect", "5", env=ENV)
    assert (stale.code, stale.error["code"]) == (4, "conflict.version_mismatch")
    assert stale.error["hint"] == f"graph get {ids['x']}"
    fresh = app(run_cli, "update", ids["x"], "--set", f"{attr}=new", "--expect", "1", env=ENV)
    assert fresh.code == 0 and fresh.data["version"] == 2


def test_dry_run_and_invalid_writes(
    run_cli: CliRunner, root: Path, roles: Roles, factory: OpsFactory
) -> None:
    cls = roles.term("lifecycle_type")
    ids = cli_create(run_cli, factory, cls)
    seq = app(run_cli, "get", ids["x"], env=ENV).envelope["graph"]["seq"]
    attr = string_attr(factory, cls)
    dry = app(run_cli, "update", ids["x"], "--set", f"{attr}=preview", "--dry-run", env=ENV)
    assert dry.code == 0 and dry.data["dry_run"] is True and dry.data["tx_id"] is None
    assert app(run_cli, "get", ids["x"], env=ENV).envelope["graph"]["seq"] == seq
    invalid = app(run_cli, "update", ids["x"], "--set", "no_such_attribute=1", env=ENV)
    assert (invalid.code, invalid.error["code"]) == (1, "write.invalid")
    assert codes(invalid.error["details"]["findings"]) == ["data.structural.unknown_attribute"]
    missing = app(run_cli, "update", ids["x"], env=ENV)
    assert (missing.code, missing.error["code"]) == (2, "usage.missing_argument")
    bad_json = app(run_cli, "update", ids["x"], "--set-json", f"{attr}=[unclosed", env=ENV)
    assert (bad_json.code, bad_json.error["code"]) == (2, "usage.invalid_argument")


def test_link_and_unlink(run_cli: CliRunner, root: Path, roles: Roles, factory: OpsFactory) -> None:
    rel = roles.term("acyclic_relation")
    relation = factory.model.relations[rel]
    cls = factory.concrete(relation.domain[0])
    a = cli_create(run_cli, factory, cls, "a")["a"]
    b = cli_create(run_cli, factory, cls, "b")["b"]
    linked = app(run_cli, "link", a, rel, b, env=ENV)
    assert linked.code == 0 and linked.data["entities"][0]["ops"] == ["link"]
    cycle = app(run_cli, "link", b, rel, a, env=ENV)
    assert (cycle.code, cycle.error["code"]) == (1, "write.invalid")
    assert codes(cycle.error["details"]["findings"]) == ["data.semantic.cycle"]
    unlinked = app(run_cli, "unlink", a, rel, b, env=ENV)
    assert unlinked.code == 0 and unlinked.data["version"] == 3
    again = app(run_cli, "unlink", a, rel, b, env=ENV)
    assert codes(again.error["details"]["findings"]) == ["data.referential.edge_missing"]
    assert app(run_cli, "unlink", a, env=ENV).code == 2


def _manifest(root: Path) -> Path:
    (graph_dir,) = list((root / "graphs").iterdir())
    return graph_dir / "manifest.toml"


def test_archived_graph_and_tampered_artifact(
    run_cli: CliRunner, root: Path, roles: Roles, factory: OpsFactory
) -> None:
    cls = roles.term("lifecycle_type")
    manifest = _manifest(root)
    original = manifest.read_text()
    manifest.write_text(original.replace('state = "active"', 'state = "archived"'))
    op = factory.create(cls, "x")[0]
    archived: Outcome = app(run_cli, *create_args(op, {}), env=ENV)
    assert (archived.code, archived.error["code"]) == (1, "graph.archived")
    manifest.write_text(original)
    (compiled,) = list((manifest.parent / "ontology").glob("*/compiled.json"))
    compiled.write_text(compiled.read_text().replace('"summary": "', '"summary": "X', 1))
    tampered = app(run_cli, *create_args(op, {}), env=ENV)
    assert (tampered.code, tampered.error["code"]) == (5, "ontology.pin_mismatch")
    read = app(run_cli, "graphs")
    assert read.code == 0


def test_qualified_link_unlink_by_edge_id_and_text(
    run_cli: CliRunner, root: Path, roles: Roles, factory: OpsFactory
) -> None:
    rel = str(roles.get("qualified_relation"))
    relation = factory.model.relations[rel]
    (qualifier, *_) = sorted(relation.qualifiers)
    source = cli_create(run_cli, factory, factory.concrete(relation.domain[0]), "s")["s"]
    target = cli_create(run_cli, factory, factory.concrete(relation.range[0]), "t")["t"]
    linked = app(run_cli, "link", source, rel, target, "--qualifier", f"{qualifier}=p. 7", env=ENV)
    assert linked.code == 0, linked.envelope
    (edge,) = [
        e for e in app(run_cli, "get", source, env=ENV).data["entity"]["edges"] if e["rel"] == rel
    ]
    assert edge["qualifiers"] == {qualifier: "p. 7"} and edge["target"] == target
    both = app(run_cli, "unlink", source, rel, target, "--edge-id", edge["edge_id"], env=ENV)
    assert (both.code, both.error["code"]) == (2, "usage.missing_argument")
    text = run_cli("graph", "unlink", source, "--edge-id", edge["edge_id"], env=ENV)
    assert text.returncode == 0 and text.stderr == "" and text.stdout.strip()
    edges = app(run_cli, "get", source, env=ENV).data["entity"]["edges"]
    assert [e for e in edges if e["rel"] == rel] == []


def test_add_and_remove_change_a_list_without_a_read(
    run_cli: CliRunner, root: Path, roles: Roles, factory: OpsFactory
) -> None:
    curie, _, attr = str(roles.get("alt_label_attribute")).rpartition(".")
    ids = cli_create(run_cli, factory, curie)
    first = app(
        run_cli, "update", ids["x"], "--add", f"{attr}=One", "--add", f"{attr}=Two", env=ENV
    )
    assert first.code == 0, first.envelope
    mixed = app(
        run_cli,
        "update",
        ids["x"],
        "--add",
        f"{attr}=Two",
        "--add-json",
        f'{attr}="Three"',
        env=ENV,
    )
    assert (mixed.code, mixed.error["details"]["argument"]) == (2, "--add-json")
    added = app(
        run_cli,
        "update",
        ids["x"],
        "--add-json",
        f'{attr}="Two"',
        "--add-json",
        f'{attr}="Three"',
        env=ENV,
    )
    assert added.code == 0, added.envelope
    shown = app(run_cli, "get", ids["x"], "--fields", attr, env=ENV)
    assert shown.data["entity"]["attrs"][attr] == ["One", "Two", "Three"]
    removed = app(run_cli, "update", ids["x"], "--remove", f"{attr}=One", "--expect", "3", env=ENV)
    assert removed.code == 0, removed.envelope
    shown = app(run_cli, "get", ids["x"], "--fields", attr, env=ENV)
    assert shown.data["entity"]["attrs"][attr] == ["Two", "Three"]
    label = str(roles.get("label_attribute")).rpartition(".")[2]
    single = app(run_cli, "update", ids["x"], "--add", f"{label}=Other", "--dry-run", env=ENV)
    assert (single.code, single.error["code"]) == (1, "write.invalid")
    assert codes(single.error["details"]["findings"]) == ["data.structural.cardinality_many"]
    both = app(run_cli, "update", ids["x"], "--set", f"{attr}=x", "--remove", f"{attr}=x", env=ENV)
    assert (both.code, both.error["details"]["argument"]) == (2, "--remove")
    none = app(run_cli, "update", ids["x"], env=ENV)
    assert (none.code, none.error["code"]) == (2, "usage.missing_argument")


def test_a_json_array_as_text_for_a_list_is_refused(
    run_cli: CliRunner, root: Path, roles: Roles, factory: OpsFactory
) -> None:
    curie, _, attr = str(roles.get("alt_label_attribute")).rpartition(".")
    ids = cli_create(run_cli, factory, curie)
    assert app(run_cli, "update", ids["x"], "--add", f"{attr}=One", env=ENV).code == 0
    for args in (("--set", f'{attr}=["One","Two"]'), ("--add", f'{attr}=["Two"]')):
        for dry in (("--dry-run",), ()):
            refused = app(run_cli, "update", ids["x"], *args, *dry, env=ENV)
            assert (refused.code, refused.error["code"]) == (1, "write.invalid"), args
            (finding,) = refused.error["details"]["findings"]
            assert finding["code"] == "data.structural.cardinality_many"
            assert "--set-json" in finding["message"]
    shown = app(run_cli, "get", ids["x"], "--fields", attr, env=ENV)
    assert shown.data["entity"]["attrs"][attr] == ["One"]
    fixed = app(run_cli, "update", ids["x"], "--set-json", f'{attr}=["One","Two"]', env=ENV)
    assert fixed.code == 0, fixed.envelope


def test_a_write_that_changes_nothing_commits_nothing(
    run_cli: CliRunner, root: Path, roles: Roles, factory: OpsFactory
) -> None:
    curie, _, attr = str(roles.get("alt_label_attribute")).rpartition(".")
    ids = cli_create(run_cli, factory, curie)
    assert app(run_cli, "update", ids["x"], "--add", f"{attr}=One", env=ENV).code == 0
    head = app(run_cli, "get", ids["x"], env=ENV)
    seq, rev = head.envelope["graph"]["seq"], head.data["entity"]["rev"]
    for dry in (("--dry-run",), ()):
        same = app(run_cli, "update", ids["x"], "--add", f"{attr}=One", *dry, env=ENV)
        assert same.code == 0, same.envelope
        assert (same.data["tx_id"], same.data["seq"], same.data["event_id"]) == (None, None, None)
        (entity,) = same.data["entities"]
        assert (entity["changed"], entity["rev"]) == (False, rev)
        assert same.warning_codes == ["entity.unchanged"]
        assert same.envelope["warnings"][0]["details"] == {"ids": [ids["x"]], "count": 1}
    after = app(run_cli, "get", ids["x"], env=ENV)
    assert (after.envelope["graph"]["seq"], after.data["entity"]["rev"]) == (seq, rev)
    changed = app(run_cli, "update", ids["x"], "--add", f"{attr}=Two", env=ENV)
    assert changed.data["entities"][0]["changed"] is True and changed.warning_codes == []

    # a dry-run batch predicts seq from the documents that commit
    def add(value: str, **more: int) -> str:
        op = {"op": "update", "id": ids["x"], "add": {attr: [value]}}
        return json.dumps({"ops": [op], **more})

    documents = "\n".join([add("Two"), add("Three"), add("Four", expect_seq=seq + 2)]) + "\n"
    batch = app(run_cli, "apply", "-", "--dry-run", env=ENV, input=documents)
    assert batch.code == 0, batch.envelope
    applied = app(run_cli, "apply", "-", env=ENV, input=documents)
    assert applied.code == 0, applied.envelope
    assert [r["result"]["seq"] for r in applied.data["results"]] == [None, seq + 2, seq + 3]
