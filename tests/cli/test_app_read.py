"""S: graph get, history, find, lookup, search and neighbors on seeded graphs of both fixtures
(plan A4; contracts §4.3, B8.8, B8.12, B8.15, B9.7, B13.7)."""

import shlex
from pathlib import Path
from typing import Any

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


def retracted_now(run_cli: CliRunner, roles: Roles) -> dict[str, Any]:
    """An active entity retracted by this test: one revision more, with a reason."""
    found = app(run_cli, "find", "--type", roles.term("retractable_type"), "--limit", "1", env=ENV)
    entity = found.data["items"][0]
    retracted = app(run_cli, "retract", entity["id"], "--reason", "test", env=ENV)
    assert retracted.code == 0, retracted.envelope
    return {**entity, "rev": entity["rev"] + 1}


def test_history_and_an_earlier_revision(run_cli: CliRunner, root: Path, roles: Roles) -> None:
    entity = retracted_now(run_cli, roles)
    rev = entity["rev"]
    history = app(run_cli, "history", entity["id"], env=ENV)
    assert history.code == 0, history.envelope
    items = history.data["items"]
    assert [i["rev"] for i in items] == list(range(rev, 0, -1))
    assert history.data["current_rev"] == rev
    assert (items[0]["command"], items[0]["reason"]) == ("retract", "test")
    assert items[0]["changed"]["record"] == ["record_status"]
    assert items[-1]["changed"] is None
    earlier = app(run_cli, "get", entity["id"], "--rev", str(rev - 1), env=ENV)
    assert earlier.code == 0, earlier.envelope
    assert (earlier.data["entity"]["rev"], earlier.data["current_rev"]) == (rev - 1, rev)
    assert earlier.data["entity"]["record_status"] == "active"
    assert earlier.warning_codes == ["entity.retracted"]
    missing = app(run_cli, "get", entity["id"], "--rev", "99", env=ENV)
    assert (missing.code, missing.error["code"]) == (1, "entity.revision_not_found")
    assert missing.error["hint"] == f"graph history {entity['id']}"
    text = run_cli("graph", "history", entity["id"], env=ENV)
    assert text.returncode == 0 and text.stderr == ""


def test_history_pages_with_a_next_command(run_cli: CliRunner, root: Path, roles: Roles) -> None:
    entity = retracted_now(run_cli, roles)
    first = app(run_cli, "history", entity["id"], "--limit", "1", env=ENV)
    assert first.envelope["page"]["truncated"] is True
    command = first.envelope["hints"][0]["command"]
    assert command == f"graph history {entity['id']} --graph example_graph --limit 1 --offset 1"
    second = app(run_cli, *shlex.split(command)[1:])
    assert [i["rev"] for i in second.data["items"]] == [entity["rev"] - 1]
    refused = app(run_cli, "history", entity["id"], "--limit", "0", env=ENV)
    assert (refused.code, refused.error["code"]) == (2, "usage.invalid_argument")


def test_reads_as_of_an_earlier_tx(run_cli: CliRunner, root: Path, roles: Roles) -> None:
    """B8.8: the state before the retract; next pages stay on it."""
    entity = retracted_now(run_cli, roles)
    head = app(run_cli, "get", entity["id"], env=ENV).envelope["graph"]["seq"]
    before = f"tx:{head - 1}"
    got = app(run_cli, "get", entity["id"], "--as-of", before, env=ENV)
    assert got.code == 0, got.envelope
    assert got.envelope["graph"]["seq"] == head - 1
    assert got.data["entity"]["record_status"] == "active"
    assert got.data["current_rev"] == entity["rev"] - 1
    assert got.warning_codes == ["temporal.as_of_read"]
    (warning,) = got.envelope["warnings"]
    assert (warning["details"]["seq"], warning["details"]["head_seq"]) == (head - 1, head)
    pinned = app(run_cli, "get", entity["id"], "--rev", "1", "--as-of", before, env=ENV)
    assert pinned.code == 0 and pinned.data["entity"]["rev"] == 1
    history = app(run_cli, "history", entity["id"], "--as-of", before, "--limit", "1", env=ENV)
    assert [i["rev"] for i in history.data["items"]] == [entity["rev"] - 1]
    if history.envelope["page"]["truncated"]:
        command = history.envelope["hints"][0]["command"]
        assert command.endswith(f"--as-of {before} --limit 1 --offset 1")
    neighbors = app(run_cli, "neighbors", entity["id"], "--as-of", before, env=ENV)
    assert neighbors.code == 0 and neighbors.warning_codes == ["temporal.as_of_read"]
    cls = roles.term("retractable_type")
    now = app(run_cli, "find", "--type", cls, "--limit", "500", env=ENV)
    then = app(run_cli, "find", "--type", cls, "--limit", "500", "--as-of", before, env=ENV)
    assert then.envelope["page"]["total"] == now.envelope["page"]["total"] + 1


def test_as_of_a_timestamp_pins_the_next_page_to_its_tx(
    run_cli: CliRunner, root: Path, roles: Roles
) -> None:
    cls = roles.term("lifecycle_type")
    future = "2999-01-01T00:00:00Z"
    first = app(run_cli, "find", "--type", cls, "--limit", "2", "--as-of", future, env=ENV)
    assert first.code == 0, first.envelope
    seq = first.envelope["graph"]["seq"]
    (hint,) = first.envelope["hints"]
    assert hint["command"].endswith(f"--as-of tx:{seq} --limit 2 --offset 2")
    nxt = app(run_cli, *shlex.split(hint["command"])[1:])
    assert nxt.code == 0 and nxt.envelope["graph"]["seq"] == seq
    empty = app(run_cli, "find", "--as-of", "2000-01-01T00:00:00+01:00", env=ENV)
    assert empty.envelope["graph"]["seq"] == 0 and empty.data["items"] == []


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
    for as_of in ("yesterday", "2030-01-01", "tx:x", "tx:1000000"):
        refused = app(run_cli, "find", "--as-of", as_of, env=ENV)
        assert (refused.code, refused.error["code"]) == (2, "usage.invalid_argument"), as_of
        assert refused.error["details"]["argument"] == "--as-of"


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
    before = app(run_cli, "lookup", partial, "--include-inactive", "--as-of", "tx:0", env=ENV)
    (hint,) = before.envelope["hints"]
    assert hint["command"].endswith("--match prefix --as-of tx:0 --include-inactive")
    assert app(run_cli, *shlex.split(hint["command"])[1:]).data["items"] == []


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
    blank = app(run_cli, "lookup", "  ", env=ENV)
    assert blank.code == 2
    bad_match = app(run_cli, "lookup", "x", "--match", "contains", env=ENV)
    assert bad_match.code == 2


def test_search_finds_names_inside_words_and_with_typos(
    run_cli: CliRunner, root: Path, roles: Roles
) -> None:
    entity, value = a_label(run_cli, roles)
    attr = str(roles.get("label_attribute")).rpartition(".")[2]
    for text, kind in ((value.upper(), "exact"), (value[1:-1], "phrase")):
        outcome = app(run_cli, "search", text, env=ENV)
        assert outcome.code == 0, outcome.envelope
        item = next(i for i in outcome.data["items"] if i["id"] == entity)
        assert set(item) == {"id", "alias", "type", "record_status", "label", "match", "fields"}
        assert item["label"] == value
        assert item["match"] | {"excerpt": None} == {
            "attr": attr,
            "role": "label",
            "kind": kind,
            "score": 100,
            "excerpt": None,
        }
        assert attr in item["fields"]
    if len(value) >= 8:  # one dropped letter in a longer name is still similar
        typo = value[:3] + value[4:]
        found = app(run_cli, "search", typo, "--field", attr, env=ENV)
        assert entity in [i["id"] for i in found.data["items"]]
    hidden_id, hidden = a_label(run_cli, roles, "hidden_label_attribute")
    found = app(run_cli, "search", hidden, "--type", str(roles.get("label_type")), env=ENV)
    item = next(i for i in found.data["items"] if i["id"] == hidden_id)
    assert item["match"]["role"] == "hidden_label" and item["match"]["excerpt"] is None
    text = run_cli("graph", "search", value, env=ENV)
    assert text.returncode == 0 and entity in text.stdout and text.stderr == ""


def test_search_hints_and_errors(run_cli: CliRunner, root: Path, roles: Roles) -> None:
    label_type = str(roles.get("label_type"))
    nothing = app(run_cli, "search", "zzqqxxjj", "--type", label_type, env=ENV)
    assert (nothing.code, nothing.data["items"]) == (0, [])
    (hint,) = nothing.envelope["hints"]
    assert hint["command"] == "graph search zzqqxxjj --graph example_graph"
    unknown = app(run_cli, "search", "xy", "--field", "no_such_field", env=ENV)
    assert (unknown.code, unknown.error["code"]) == (1, "term.unknown")
    short = app(run_cli, "search", "x", env=ENV)
    assert short.code == 2
    lookup = app(run_cli, "lookup", "zzqqxxjj", "--match", "prefix", env=ENV)
    (hint,) = lookup.envelope["hints"]
    assert hint["command"] == "graph search zzqqxxjj --graph example_graph"


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
