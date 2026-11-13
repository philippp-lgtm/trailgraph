"""The ``inventory`` series 1.1.0 -> 2.0.0 with data: the ``name`` rename is derived, the removed
enum value ``worn`` needs a ``map_values`` step, and ``check_graph`` judges the migrated
overlay (migration plan M3: the release is refused for data that does not fit *after* the
migration)."""

from datetime import UTC, datetime
from uuid import UUID

from support.fixtures import compiled_model
from support.snapshots import DictSnapshot, edge, eid, revision

from graph_cli.kernel.findings import Finding, Severity
from graph_cli.kernel.ids import TxId
from graph_cli.kernel.records import Revision
from graph_cli.ontology_layer.data_validator import check_graph
from graph_cli.ontology_layer.diff import derive_renames
from graph_cli.ontology_layer.migration import check_migration, parse_migration
from graph_cli.temporal_knowledge.migrate import MigrationPlan, MigrationStamp, plan_migration

STAMP = MigrationStamp(
    TxId(UUID("0192f000-0000-7000-8000-0000000000cc")),
    9,
    datetime(2026, 11, 13, tzinfo=UTC),
    "2.0.0",
)
MIGRATION = """\
format: graph_cli.migration/1
from: 1.1.0
summary: The condition worn is gone; those items count as good.
steps:
  - map_values: {type: inv:Item, attribute: condition, values: {worn: good}}
"""


def item(name: str, condition: str, *, status: str = "active") -> Revision:
    return revision(
        name,
        "inv:Item",
        {"name": name, "quantity": 1, "status": "ordered", "condition": condition},
        [
            edge("inv:located_in", "shelf", name=f"{name}-loc"),
            edge("inv:in_category", "tools", name=f"{name}-cat"),
        ],
        status=status,  # type: ignore[arg-type]
        alias=f"ITEM-{name}",
    )


def seed() -> DictSnapshot:
    return DictSnapshot(
        revision("room", "inv:Location", {"name": "Room", "code": "R1"}),
        revision(
            "shelf",
            "inv:Location",
            {"name": "Shelf", "code": "R1-S1"},
            [edge("inv:within", "room", name="shelf-in")],
        ),
        revision("tools", "inv:Category", {"name": "Tools"}),
        item("hammer", "worn"),
        item("saw", "good"),
        item("rasp", "worn", status="retracted"),  # judged too, so migrated too
    )


def errors(findings: list[Finding]) -> list[tuple[str, str]]:
    return [(f.code, str(f.target["id"])) for f in findings if f.severity is Severity.ERROR]


def plan(snapshot: DictSnapshot, *, with_step: bool) -> MigrationPlan:
    old, new = compiled_model("inventory", "1.1.0"), compiled_model("inventory", "2.0.0")
    renames = derive_renames(old, new)
    doc = parse_migration(MIGRATION).doc
    assert doc is not None
    assert check_migration(doc, old, new, renames, language=None) == ()
    return plan_migration(
        old, new, renames, doc if with_step else None, snapshot, stamp=STAMP, language=None
    )


def test_the_seed_does_not_fit_the_new_version_without_a_migration() -> None:
    new = compiled_model("inventory", "2.0.0")
    codes = {code for code, _ in errors(check_graph(new, seed()))}
    assert codes == {
        "data.structural.missing_required",  # label
        "data.structural.unknown_attribute",  # name
        "data.structural.enum_value_unknown",  # worn
    }


def test_the_renames_alone_leave_the_removed_enum_value() -> None:
    snapshot = seed()
    result = plan(snapshot, with_step=False)
    found = errors(check_graph(compiled_model("inventory", "2.0.0"), result.overlay(snapshot)))
    assert {code for code, _ in found} == {"data.structural.enum_value_unknown"}
    assert {entity for _, entity in found} == {str(eid("hammer")), str(eid("rasp"))}


def test_with_the_step_the_migrated_graph_passes() -> None:
    snapshot = seed()
    result = plan(snapshot, with_step=True)
    new = compiled_model("inventory", "2.0.0")
    assert errors(check_graph(new, result.overlay(snapshot))) == []
    assert not result.findings
    assert {str(e) for e in result.revisions} == {str(eid(n)) for n in ("hammer", "saw", "rasp")}
    hammer = result.revisions[eid("hammer")]
    assert hammer.attrs == {
        "label": "hammer",
        "quantity": 1,
        "status": "ordered",
        "condition": "good",
    }
    assert (result.revisions[eid("rasp")].record_status, hammer.alias) == (
        "retracted",
        "ITEM-hammer",
    )
    assert result.unused_steps == ()
    preview = result.to_json()
    assert preview["entities"] == 3 and preview["unchanged"] == 3
