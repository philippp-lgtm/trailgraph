"""S: graph get, find, lookup and neighbors on seeded graphs of both fixtures (plan A4;
contracts §4.3, B8.8, B8.12, B9.7, B13.7)."""

import shlex
from pathlib import Path

import pytest
from harness import CliRunner
from support.activity import short_dir
from support.admin import init, write_documents
from support.app import app, create_graph
from support.fixtures import FIXTURES, TOY_ONTOLOGIES
from support.roles import Roles, load_roles
from support.trees import tree_hash

from graph_cli.catalog.activity import ActivityListener
from graph_cli.kernel.changeset import read_documents


@pytest.fixture(params=TOY_ONTOLOGIES)
def ontology(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str], ontology: str) -> Path:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", ontology)
    seeds = read_documents((FIXTURES / "seeds" / ontology / "changes.jsonl").read_bytes())
    write_documents(root, "example_graph", seeds)  # type: ignore[arg-type]
    return root


@pytest.fixture
def roles(ontology: str) -> Roles:
    return load_roles(ontology)


ENV = {"GRAPH_CLI_GRAPH": "example_graph"}


def test_get(run_cli: CliRunner, root: Path, roles: Roles) -> None:
    found = app(run_cli, "find", "--type", roles.term("lifecycle_type"), "--limit", "1", env=ENV)
    entity = found.data["items"][0]
    for ref in (entity["id"], entity["alias"], entity["id"].replace("-", "")[-10:]):
        outcome = app(run_cli, "get", ref, env=ENV)
        assert outcome.code == 0, outcome.envelope
        assert outcome.data["entity"]["id"] == entity["id"]
        assert outcome.envelope["graph"]["name"] == "example_graph"
    missing = app(run_cli, "get", "0192f3c0-11aa-7e01-b2c3-d4e5f6a7b8c9", env=ENV)
    assert (missing.code, missing.error["code"]) == (1, "entity.not_found")
    text = run_cli("graph", "get", entity["alias"], env=ENV)
    assert text.returncode == 0 and entity["id"] in text.stdout and text.stderr == ""


def test_superseded_read_warns(run_cli: CliRunner, root: Path) -> None:
    superseded = app(
        run_cli, "find", "--where", "record_status=superseded", "--include-inactive", env=ENV
    )
    old = superseded.data["items"][0]
    outcome = app(run_cli, "get", old["id"], env=ENV)
    assert outcome.warning_codes == ["entity.superseded"]
    assert outcome.data["superseded_by"]


def test_find_pages_with_a_next_command(run_cli: CliRunner, root: Path, roles: Roles) -> None:
    cls = roles.term("lifecycle_type")
    first = app(run_cli, "find", "--type", cls, "--limit", "2", env=ENV)
    assert first.code == 0
    page = first.envelope["page"]
    assert page["shown"] == 2 and page["truncated"] and page["next_offset"] == 2
    (hint,) = first.envelope["hints"]
    assert hint["command"].startswith("graph find --graph example_graph --type ")
    assert hint["command"].endswith("--limit 2 --offset 2")
    nxt = app(run_cli, *hint["command"].split()[1:])
    assert nxt.code == 0 and nxt.envelope["page"]["offset"] == 2
    enum_attr = roles.term("enum_attribute").rpartition(".")[2]
    filtered = app(run_cli, "find", "--type", cls, "--where", f"{enum_attr}:exists=true", env=ENV)
    assert 0 < filtered.envelope["page"]["total"] <= page["total"]
    rel = roles.term("required_relation")
    related = app(run_cli, "find", "--type", cls, "--rel", rel, "--limit", "500", env=ENV)
    assert related.envelope["page"]["total"] == page["total"]


def test_find_rel_matches_any_listed_target(run_cli: CliRunner, root: Path, roles: Roles) -> None:
    cls, rel = roles.term("lifecycle_type"), roles.term("required_relation")
    items = app(run_cli, "find", "--type", cls, "--limit", "500", env=ENV).data["items"]
    targets = sorted({e["target"] for i in items for e in i["edges"] if e["rel"] == rel})
    assert len(targets) >= 2
    first, second = targets[:2]
    alias = app(run_cli, "get", second, env=ENV).data["entity"]["alias"] or second

    def matching(*refs: str) -> set[str]:
        flag = f"{rel}={','.join(refs)}"
        outcome = app(run_cli, "find", "--type", cls, "--rel", flag, "--limit", "500", env=ENV)
        assert outcome.code == 0, outcome.envelope
        return {i["id"] for i in outcome.data["items"]}

    either = matching(first, alias)
    assert either == matching(first) | matching(second)
    assert matching(first) and matching(second) and either != matching(first)
    empty = app(run_cli, "find", "--type", cls, "--rel", f"{rel}={first},", env=ENV)
    assert (empty.code, empty.error["code"]) == (2, "usage.invalid_argument")


def test_find_errors(run_cli: CliRunner, root: Path, roles: Roles) -> None:
    cls = roles.term("lifecycle_type")
    unknown = app(run_cli, "find", "--type", cls + "x", env=ENV)
    assert (unknown.code, unknown.error["code"]) == (1, "term.unknown")
    assert unknown.error["hint"] == "graph describe types"
    bad = app(run_cli, "find", "--type", cls, "--where", "nothing", env=ENV)
    assert (bad.code, bad.error["code"]) == (2, "usage.invalid_argument")
    too_many = app(run_cli, "find", "--limit", "501", env=ENV)
    assert too_many.code == 2
    as_of = app(run_cli, "find", "--as-of", "tx:3", env=ENV)
    assert (as_of.code, as_of.error["code"]) == (1, "temporal.as_of_unsupported")


def test_neighbors(run_cli: CliRunner, root: Path, roles: Roles) -> None:
    rel = roles.term("required_relation")
    source = app(
        run_cli, "find", "--type", roles.term("lifecycle_type"), "--limit", "1", env=ENV
    ).data["items"][0]
    outcome = app(run_cli, "neighbors", source["id"], "--rel", rel, "--direction", "out", env=ENV)
    assert outcome.code == 0 and outcome.data["items"]
    assert {i["direction"] for i in outcome.data["items"]} == {"out"}
    deep = app(run_cli, "neighbors", source["id"], "--depth", "3", env=ENV)
    assert len(deep.data["items"]) >= len(outcome.data["items"])
    assert app(run_cli, "neighbors", source["id"], "--depth", "4", env=ENV).code == 2


def test_reads_leave_the_data_root_unchanged(run_cli: CliRunner, root: Path, roles: Roles) -> None:
    for graph_dir in (root / "graphs").iterdir():
        (graph_dir / "data" / "head.json").unlink()  # B13.7: reads never rewrite it
    before = tree_hash(root)
    cls = roles.term("lifecycle_type")
    found = app(run_cli, "find", "--type", cls, env=ENV)
    assert found.code == 0
    assert app(run_cli, "get", found.data["items"][0]["id"], env=ENV).code == 0
    assert app(run_cli, "describe", "type", cls, env=ENV).code == 0
    assert app(run_cli, "graphs").code == 0
    assert app(run_cli, "lookup", "x", "--match", "prefix", env=ENV).code in (0, 1)
    assert tree_hash(root) == before


def a_label(run_cli: CliRunner, roles: Roles, role: str = "label_attribute") -> tuple[str, str]:
    """``(id, value)`` of an active entity with a value for a label role."""
    curie, _, attr = str(roles.get(role)).rpartition(".")
    found = app(run_cli, "find", "--type", curie, "--where", f"{attr}:exists=true", env=ENV)
    entity = found.data["items"][0]
    value = entity["attrs"][attr]
    return entity["id"], value[0] if isinstance(value, list) else value


def test_lookup_finds_candidates(run_cli: CliRunner, root: Path, roles: Roles) -> None:
    entity, value = a_label(run_cli, roles)
    outcome = app(run_cli, "lookup", value.upper(), env=ENV)
    assert outcome.code == 0, outcome.envelope
    item = next(i for i in outcome.data["items"] if i["id"] == entity)
    assert item["label"] == value
    assert item["match"] == {
        "attr": str(roles.get("label_attribute")).rpartition(".")[2],
        "role": "label",
        "kind": "exact",
        "value": value,
    }
    assert set(item) == {"id", "alias", "type", "record_status", "label", "match"}
    assert outcome.envelope["page"]["total"] == len(outcome.data["items"])
    hidden_id, hidden = a_label(run_cli, roles, "hidden_label_attribute")
    found = app(run_cli, "lookup", hidden, "--type", str(roles.get("label_type")), env=ENV)
    (item,) = found.data["items"]
    assert item["id"] == hidden_id and item["match"]["value"] is None
    text = run_cli("graph", "lookup", value, env=ENV)
    assert text.returncode == 0 and entity in text.stdout and text.stderr == ""


def test_lookup_pages_with_a_next_command(run_cli: CliRunner, root: Path, roles: Roles) -> None:
    _, value = a_label(run_cli, roles)
    every = app(run_cli, "lookup", value[0], "--match", "prefix", env=ENV)
    assert every.envelope["page"]["total"] >= 2, "the seeds need two names with one initial"
    first = app(run_cli, "lookup", value[0], "--match", "prefix", "--limit", "1", env=ENV)
    (hint,) = first.envelope["hints"]
    assert hint["command"] == (
        f"graph lookup {shlex.quote(value[0])} --graph example_graph --match prefix "
        "--limit 1 --offset 1"
    )
    nxt = app(run_cli, *shlex.split(hint["command"])[1:])
    assert nxt.data["items"] == every.data["items"][1:2]


def test_lookup_without_exact_match_suggests_prefix(
    run_cli: CliRunner, root: Path, roles: Roles
) -> None:
    _, value = a_label(run_cli, roles)
    partial = value[: max(1, len(value) - 1)]
    outcome = app(run_cli, "lookup", partial, "--include-inactive", env=ENV)
    assert (outcome.code, outcome.data["items"]) == (0, [])
    (hint,) = outcome.envelope["hints"]
    assert hint["command"] == (
        f"graph lookup {shlex.quote(partial)} --graph example_graph --match prefix "
        "--include-inactive"
    )
    assert app(run_cli, *shlex.split(hint["command"])[1:]).data["items"]


def test_lookup_errors(run_cli: CliRunner, root: Path) -> None:
    types = app(run_cli, "describe", "types", env=ENV).data["types"]
    curies = [t["curie"] for t in types]
    bare = next(
        c
        for c in curies
        if not any(
            f["role"] for f in app(run_cli, "describe", "type", c, env=ENV).data["type"]["fields"]
        )
    )
    refused = app(run_cli, "lookup", "x", "--type", bare, env=ENV)
    assert (refused.code, refused.error["code"]) == (1, "term.no_labels")
    assert refused.error["hint"] == f"graph describe type {bare}"
    assert refused.error["details"] == {"types": [bare]}
    as_of = app(run_cli, "lookup", "x", "--as-of", "tx:3", env=ENV)
    assert (as_of.code, as_of.error["code"]) == (1, "temporal.as_of_unsupported")
    blank = app(run_cli, "lookup", "  ", env=ENV)
    assert blank.code == 2
    bad_match = app(run_cli, "lookup", "x", "--match", "contains", env=ENV)
    assert bad_match.code == 2


def test_reads_reach_a_listening_view_only_when_enabled(
    run_cli: CliRunner, root: Path, roles: Roles, isolated_env: dict[str, str]
) -> None:
    config = Path(isolated_env["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    with short_dir() as state:
        env = {**ENV, "XDG_STATE_HOME": str(state)}
        listener = ActivityListener.open(state / "graph_cli" / "activity")
        try:
            found = app(run_cli, "find", "--type", roles.term("lifecycle_type"), env=env)
            assert found.code == 0 and listener.receive(0.2) == []  # off by default
            config.write_text(config.read_text() + "\n[activity]\nsignal_reads = true\n")
            entity = found.data["items"][0]
            assert app(run_cli, "get", entity["alias"], env=env).code == 0
            (event,) = listener.receive(2.0)
            assert (event.graph, event.ids) == (found.envelope["graph"]["id"], (entity["id"],))
        finally:
            listener.close()
        assert app(run_cli, "get", entity["alias"], env=env).code == 0  # no viewer left
