"""U: skill freshness claims (contracts B14.17, plan origin_lifecycle E3).

Markers are read from both scopes, sent and parsed as ``Graph-Cli-Skills``,
and judged against the graph's slot and pin: a stale copy refuses a write
and warns on a read.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from graph_cli.catalog.skill_claims import (
    MAX_CLAIMS,
    SkillClaim,
    SkillGate,
    app_stale,
    claims_header,
    graph_stale,
    parse_claims_header,
    read_claims,
    report,
)
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ids import GraphId, parse_id
from graph_cli.kernel.ports.catalog import GraphEntry, SkillSlot
from graph_cli.kernel.records import OntologyPin
from graph_cli.kernel.skill_marker import MARKER_FILE, MARKER_SCHEMA

GRAPH = "0192e000-0000-7000-8000-000000000001"
OTHER = "0192e000-0000-7000-8000-000000000002"
H1, H2, H3 = ("sha256:" + c * 64 for c in "123")
PIN = OntologyPin("inventory", "1.2.0", H3)


def entry(graph: str = GRAPH) -> GraphEntry:
    return GraphEntry(GraphId(parse_id(graph)), "example_graph", "active", PIN, {}, ())


def slot(kind: str = "static", digest: str = H1) -> SkillSlot:
    return SkillSlot(kind, digest, PIN, {"SKILL.md": b"x"})


def marker(directory: Path, name: str, kind: str, source: dict[str, Any]) -> Path:
    path = directory / name / MARKER_FILE
    path.parent.mkdir(parents=True)
    document = {"schema": MARKER_SCHEMA, "skill": name, "kind": kind, "source": source}
    path.write_text(json.dumps(document))
    return path


def graph_source(graph: str = GRAPH, digest: str = H1, **extra: Any) -> dict[str, Any]:
    return {"graph": {"id": graph, "name": "example_graph"}, "hash": digest, **extra}


# ---- reading markers -------------------------------------------------------------


def test_claims_of_both_scopes(tmp_path: Path) -> None:
    project, user = tmp_path / "project", tmp_path / "user"
    marker(project, "graph-cli", "app", {"inventory_hash": H1})
    marker(project, "graph-example-graph", "graph", graph_source())
    model = {"bundle": "inventory", "version": "1.2.0"}
    marker(user, "graph-example-graph", "graph", graph_source(digest=H2, model=model))
    marker(user, "unrelated", "app", {"inventory_hash": H2})  # not one of ours: no claim
    claims = read_claims([("project", project), ("user", user)])
    assert claims == (
        SkillClaim("project", H1),
        SkillClaim("project", H1, GRAPH),
        SkillClaim("user", H2, GRAPH, ("inventory", "1.2.0")),
    )


def test_a_directory_seen_twice_counts_once(tmp_path: Path) -> None:
    marker(tmp_path, "graph-cli", "app", {"inventory_hash": H1})
    assert read_claims([("project", tmp_path), ("user", tmp_path)]) == (SkillClaim("project", H1),)
    assert read_claims([("user", tmp_path / "missing")]) == ()


@pytest.mark.parametrize(
    ("kind", "source"),
    [
        ("app", {"inventory_hash": "not a hash"}),
        ("app", {}),
        ("graph", graph_source(graph="has space")),
        ("graph", graph_source(digest="sha256:short")),
        ("graph", graph_source(model={"bundle": "inventory"})),
        ("other", graph_source()),
    ],
)
def test_unusable_markers_claim_nothing(tmp_path: Path, kind: str, source: dict[str, Any]) -> None:
    marker(tmp_path, "graph-x", kind, source)
    assert read_claims([("user", tmp_path)]) == ()


def test_other_schemas_broken_json_and_symlinks_claim_nothing(tmp_path: Path) -> None:
    path = marker(tmp_path, "graph-cli", "app", {"inventory_hash": H1})
    path.write_text(path.read_text().replace(MARKER_SCHEMA, "graph_cli.skill_install/9"))
    (tmp_path / "graph-a").mkdir()
    (tmp_path / "graph-a" / MARKER_FILE).write_text("{")
    real = marker(tmp_path / "elsewhere", "graph-b", "app", {"inventory_hash": H1})
    (tmp_path / "graph-b").mkdir()
    (tmp_path / "graph-b" / MARKER_FILE).symlink_to(real)
    assert read_claims([("user", tmp_path)]) == ()


# ---- the header ------------------------------------------------------------------


def test_the_header_carries_graph_claims_only() -> None:
    claims = (
        SkillClaim("user", H1),
        SkillClaim("project", H1, GRAPH),
        SkillClaim("user", H2, OTHER, ("inventory", "1.2.0")),
    )
    header = claims_header(claims)
    assert header == (
        f"graph={GRAPH};hash={H1};scope=project,"
        f" graph={OTHER};hash={H2};bundle=inventory;version=1.2.0;scope=user"
    )
    assert parse_claims_header(header) == claims[1:]
    assert claims_header(claims[:1]) is None


def test_malformed_items_are_left_out() -> None:
    good = f"graph={GRAPH};hash={H1};scope=user"
    bad = [
        f"graph={GRAPH};hash=nope;scope=user",
        f"graph={GRAPH};hash={H1};scope=team",
        f"graph={GRAPH};hash={H1}",
        f"graph={GRAPH};hash={H1};bundle=inventory;scope=user",
        "garbage",
        "",
    ]
    assert parse_claims_header(", ".join([*bad, good, good])) == (SkillClaim("user", H1, GRAPH),)
    assert parse_claims_header(None) == ()
    many = ", ".join([f"graph={GRAPH};hash={H2};scope=user"] * MAX_CLAIMS + [good])
    assert parse_claims_header(many) == (SkillClaim("user", H2, GRAPH),)  # the rest is cut


# ---- judging ---------------------------------------------------------------------


def test_a_static_skill_is_stale_when_the_slot_changed() -> None:
    assert graph_stale(SkillClaim("user", H1, GRAPH), entry(), slot()) is None
    stale = graph_stale(SkillClaim("user", H2, GRAPH), entry(), slot())
    assert stale == {
        "skill": "graph-example-graph",
        "kind": "graph",
        "scope": "user",
        "graph": "example_graph",
        "installed": {"hash": H2, "model": None},
        "current": {"hash": H1, "model": None},
    }


def test_a_template_is_stale_when_the_pin_moved() -> None:
    generated = slot("generated")
    fresh = SkillClaim("user", H1, GRAPH, ("inventory", "1.2.0"))
    assert graph_stale(fresh, entry(), generated) is None
    older = SkillClaim("user", H1, GRAPH, ("inventory", "1.1.0"))
    stale = graph_stale(older, entry(), generated)
    assert stale is not None and stale["current"] == {
        "hash": H1,
        "model": {"bundle": "inventory", "version": "1.2.0"},
    }
    assert graph_stale(SkillClaim("user", H1, GRAPH), entry(), generated) is not None


def test_skill_one_is_stale_with_another_inventory() -> None:
    claims = [SkillClaim("project", H1), SkillClaim("user", H2), SkillClaim("user", H1, GRAPH)]
    assert [s["scope"] for s in app_stale(claims, H1)] == ["user"]
    assert app_stale(claims, H1)[0]["installed"] == {"inventory_hash": H2}


def test_a_write_is_refused_and_a_read_warns_once(tmp_path: Path) -> None:
    stale = app_stale([SkillClaim("project", H2)], H1)
    warnings: list[WarningRecord] = []
    with pytest.raises(GraphCliError) as refused:
        report(stale, write=True, warnings=warnings, project_dir=tmp_path / "my project")
    assert refused.value.code == "skills.outdated"
    assert refused.value.details["dir"] == str(tmp_path / "my project")
    assert refused.value.details["scope"] == "project"
    for _ in range(2):
        report(stale, write=False, warnings=warnings, project_dir=tmp_path / "my project")
    assert [w.code for w in warnings] == ["skills.stale"]
    assert f"--dir '{tmp_path}/my project'" in warnings[0].message
    user = app_stale([SkillClaim("user", H2)], H1)
    report(user, write=False, warnings=warnings, project_dir=tmp_path)
    assert "dir" not in warnings[1].details  # the user scope needs no directory
    report([], write=True, warnings=warnings, project_dir=None)  # nothing stale


class Catalog:
    def __init__(self, found: SkillSlot | GraphCliError | None) -> None:
        self.found = found
        self.reads = 0

    def read_skill(self, graph: GraphId) -> SkillSlot | None:
        self.reads += 1
        if isinstance(self.found, GraphCliError):
            raise self.found
        return self.found


def test_the_gate_reads_the_slot_only_for_a_claimed_graph() -> None:
    warnings: list[WarningRecord] = []
    claims = [SkillClaim("user", H2, OTHER)]
    catalog = Catalog(slot())
    gate = SkillGate(lambda: claims, True, warnings, None)
    gate.check(catalog, entry())  # type: ignore[arg-type]
    assert catalog.reads == 0
    claims.append(SkillClaim("user", H2, GRAPH))
    with pytest.raises(GraphCliError):
        gate.check(catalog, entry())  # type: ignore[arg-type]
    for found in (None, GraphCliError("skills.source_invalid", "damaged")):
        gate.check(Catalog(found), entry())  # type: ignore[arg-type]
    assert warnings == []
