"""Finding codes owned by temporal_knowledge (contracts R.4, B8.3-B8.6).

Produced by ``normalize`` while applying a change set; each code has one
broken change-set fixture under ``tests/fixtures/broken/<code>/`` (R.6).
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
)

FINDING_SPECS: Final = {spec.code: spec for spec in FINDING_CODES}
