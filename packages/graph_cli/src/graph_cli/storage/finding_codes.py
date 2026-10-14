"""Finding codes owned by storage (contracts R.4, R.6, section 1.5.2).

Produced by ``MaintenanceSession.verify``. Every code has exactly one tamper
scenario under ``tests/tamper/``. ``integrity.storage.pin_mismatch`` follows
with the manifest checks (P1 item 5).
"""

from __future__ import annotations

from typing import Final

from graph_cli.kernel.findings import FindingCodeSpec, Severity


def _integrity(name: str, summary: str) -> FindingCodeSpec:
    return FindingCodeSpec(
        code=f"integrity.storage.{name}",
        scope="integrity",
        layer="storage",
        default_severity=Severity.ERROR,
        when="verify",
        fixture="tamper",
        summary=summary,
    )


TX_CHAIN_BROKEN: Final = "integrity.storage.tx_chain_broken"
REVISION_HASH_MISMATCH: Final = "integrity.storage.revision_hash_mismatch"
REVISION_MISSING: Final = "integrity.storage.revision_missing"
REVISION_UNEXPECTED: Final = "integrity.storage.revision_unexpected"
HEAD_INCONSISTENT: Final = "integrity.storage.head_inconsistent"

FINDING_CODES: Final[tuple[FindingCodeSpec, ...]] = (
    _integrity("tx_chain_broken", "A tx log line is damaged or does not continue the chain."),
    _integrity("revision_hash_mismatch", "A revision file differs from its copy in the tx log."),
    _integrity("revision_missing", "A revision recorded in the tx log has no revision file."),
    _integrity("revision_unexpected", "A revision file has no copy in the tx log."),
    _integrity("head_inconsistent", "The head checkpoint does not match the tx log."),
)
