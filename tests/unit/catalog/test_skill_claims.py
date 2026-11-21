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


# ---- readers with type read limits (type read limits plan V7) ---------------------


def test_a_marker_records_the_digest_of_what_a_limited_reader_saw(tmp_path: Path) -> None:
    model = {"bundle": "inventory", "version": "1.2.0"}
    marker(tmp_path / "a", "graph-example-graph", "graph", graph_source(model=model, visible=H2))
    marker(tmp_path / "b", "graph-example-graph", "graph", graph_source(model=model))
    marker(tmp_path / "c", "graph-example-graph", "graph", graph_source(visible="not a hash"))
    assert read_claims([("user", tmp_path / "a")]) == (
        SkillClaim("user", H1, GRAPH, ("inventory", "1.2.0"), H2),
    )
    # without the key the claim is the one an unlimited reader always had
    assert read_claims([("user", tmp_path / "b")]) == (
        SkillClaim("user", H1, GRAPH, ("inventory", "1.2.0")),
    )
    assert read_claims([("user", tmp_path / "c")]) == ()


def test_the_header_carries_the_digest_only_for_a_limited_reader() -> None:
    plain = SkillClaim("user", H1, GRAPH, ("inventory", "1.2.0"))
    limited = SkillClaim("project", H1, GRAPH, None, H2)
    header = claims_header((plain, limited))
    assert header == (
        f"graph={GRAPH};hash={H1};bundle=inventory;version=1.2.0;scope=user,"
        f" graph={GRAPH};hash={H1};visible={H2};scope=project"
    )
    assert parse_claims_header(header) == (plain, limited)
    assert parse_claims_header(f"graph={GRAPH};hash={H1};visible=nope;scope=user") == ()


def test_a_template_is_stale_when_the_visible_set_moved() -> None:
    generated = slot("generated")
    model = ("inventory", "1.2.0")
    seen = SkillClaim("user", H1, GRAPH, model, H2)
    assert graph_stale(seen, entry(), generated, H2) is None
    stale = graph_stale(seen, entry(), generated, H3)  # the policy changed
    assert stale is not None
    assert stale["installed"] == {
        "hash": H1,
        "model": {"bundle": "inventory", "version": "1.2.0"},
        "visible": H2,
    }
    assert stale["current"]["visible"] == H3  # type: ignore[index]
    # a copy rendered for an unlimited reader is stale for a reader the policy limits now, and
    # the other way round
    plain = SkillClaim("user", H1, GRAPH, model)
    assert graph_stale(plain, entry(), generated, H2) is not None
    assert graph_stale(seen, entry(), generated, None) is not None
    # an unlimited reader's entry is the one it always had
    unlimited = graph_stale(SkillClaim("user", H2, GRAPH, model), entry(), generated)
    assert unlimited is not None and set(unlimited["installed"]) == {"hash", "model"}  # type: ignore[arg-type]


def test_the_gate_compares_the_digest_for_a_template_only() -> None:
    model = ("inventory", "1.2.0")
    warnings: list[WarningRecord] = []
    claims = [SkillClaim("user", H1, GRAPH, model, H2)]
    gate = SkillGate(lambda: claims, False, warnings, None, lambda found: H3)
    gate.check(Catalog(slot("generated")), entry())  # type: ignore[arg-type]
    assert [w.code for w in warnings] == ["skills.stale"]
    fresh: list[WarningRecord] = []
    SkillGate(lambda: claims, False, fresh, None, lambda found: H2).check(
        Catalog(slot("generated")),
        entry(),  # type: ignore[arg-type]
    )
    assert fresh == []
    # a static skill is the same for every reader: the policy does not make it stale
    static: list[WarningRecord] = []
    plain = [SkillClaim("user", H1, GRAPH)]
    SkillGate(lambda: plain, False, static, None, lambda found: H3).check(
        Catalog(slot("static")),
        entry(),  # type: ignore[arg-type]
    )
    assert static == []


# ---- what the render shows of the catalogs (graph start plan S6) --------------------------

F1, F2 = ("sha256:" + c * 64 for c in "ab")


def test_a_marker_records_the_featured_digest_and_a_bad_one_drops_the_claim(
    tmp_path: Path,
) -> None:
    model = {"bundle": "inventory", "version": "1.2.0"}
    marker(tmp_path / "a", "graph-example-graph", "graph", graph_source(model=model, featured=F1))
    marker(tmp_path / "b", "graph-example-graph", "graph", graph_source(model=model))
    marker(tmp_path / "c", "graph-example-graph", "graph", graph_source(featured="nope"))
    assert read_claims([("user", tmp_path / "a")]) == (
        SkillClaim("user", H1, GRAPH, ("inventory", "1.2.0"), None, F1),
    )
    # a copy installed before the digest was recorded has none
    assert read_claims([("user", tmp_path / "b")])[0].featured is None
    assert read_claims([("user", tmp_path / "c")]) == ()


def test_the_header_carries_the_featured_digest_when_the_copy_recorded_it() -> None:
    recorded = SkillClaim("user", H1, GRAPH, ("inventory", "1.2.0"), H2, F1)
    old = SkillClaim("project", H1, GRAPH, ("inventory", "1.2.0"))
    header = claims_header((recorded, old))
    assert header == (
        f"graph={GRAPH};hash={H1};bundle=inventory;version=1.2.0;visible={H2};featured={F1};"
        f"scope=user, graph={GRAPH};hash={H1};bundle=inventory;version=1.2.0;scope=project"
    )
    assert parse_claims_header(header) == (recorded, old)
    assert parse_claims_header(f"graph={GRAPH};hash={H1};featured=nope;scope=user") == ()


def test_a_template_is_stale_when_what_the_catalogs_show_moved() -> None:
    generated = slot("generated")
    model = ("inventory", "1.2.0")
    claim = SkillClaim("user", H1, GRAPH, model, None, F1)
    assert graph_stale(claim, entry(), generated, None, F1) is None
    stale = graph_stale(claim, entry(), generated, None, F2)
    assert stale is not None
    assert stale["installed"]["featured"] == F1 and stale["current"]["featured"] == F2  # type: ignore[index]
    # the digest is named only when it is a reason
    other = graph_stale(
        SkillClaim("user", H2, GRAPH, model, None, F1), entry(), generated, None, F1
    )
    assert other is not None and "featured" not in other["installed"]  # type: ignore[operator]
    # a copy without a digest is not judged on it, and neither is an unknown current one
    old = SkillClaim("user", H1, GRAPH, model)
    assert graph_stale(old, entry(), generated, None, F2) is None
    assert graph_stale(claim, entry(), generated, None, None) is None


def test_the_gate_judges_the_featured_digest_for_a_template_only_and_survives_a_bad_slot() -> None:
    model = ("inventory", "1.2.0")
    claims = [SkillClaim("user", H1, GRAPH, model, None, F1)]

    def gate(now: Any, write: bool = False) -> list[WarningRecord]:
        warnings: list[WarningRecord] = []
        SkillGate(lambda: claims, write, warnings, None, lambda e: None, now).check(
            Catalog(slot("generated")),  # type: ignore[arg-type]
            entry(),
        )
        return warnings

    assert [w.code for w in gate(lambda e: F2)] == ["skills.stale"]
    assert gate(lambda e: F1) == []

    def broken(found: GraphEntry) -> str:
        raise GraphCliError("functions.slot_invalid", "damaged")

    assert gate(broken) == []  # a damaged catalog is for the maintainer
    with pytest.raises(GraphCliError) as refused:
        SkillGate(lambda: claims, True, [], None, lambda e: None, lambda e: F2).check(
            Catalog(slot("generated")),  # type: ignore[arg-type]
            entry(),
        )
    assert refused.value.code == "skills.outdated"
    static: list[WarningRecord] = []
    plain = [SkillClaim("user", H1, GRAPH, None, None, F1)]
    SkillGate(lambda: plain, False, static, None, lambda e: None, lambda e: F2).check(
        Catalog(slot("static")),  # type: ignore[arg-type]
        entry(),
    )
    assert static == []  # a static skill shows no catalog
