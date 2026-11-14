"""Finding codes owned by temporal_knowledge (contracts R.4, B8.3-B8.6).

The ``data.temporal`` codes are produced by ``normalize`` while applying a change set; each
has one broken change-set fixture under ``tests/fixtures/broken/<code>/`` (R.6). The
``data.migration`` codes are produced by ``migrate`` while planning a migration.
"""

from __future__ import annotations

from typing import Final

from graph_cli.kernel.findings import FindingCodeSpec, Severity


def _temporal(name: str, summary: str) -> FindingCodeSpec:
    return FindingCodeSpec(
        code=f"data.temporal.{name}",
        scope="data",
        layer="temporal",
        default_severity=Severity.ERROR,
        when="write",
        fixture="broken_changeset",
        summary=summary,
    )


FINDING_CODES: Final[tuple[FindingCodeSpec, ...]] = (
    _temporal("invalid_interval", "valid_from is after valid_to."),
    _temporal("reserved_relation", "A provenance relation is written outside its operation."),
    _temporal("record_not_active", "The entity is superseded or retracted."),
    # Produced by ``migrate.plan_migration`` at release, not by a write.
    FindingCodeSpec(
        code="data.migration.many_to_single",
        scope="data",
        layer="migration",
        default_severity=Severity.ERROR,
        when="release",
        fixture="none",
        summary="A convert to single met an entity with more than one value.",
    ),
)

FINDING_SPECS: Final = {spec.code: spec for spec in FINDING_CODES}
