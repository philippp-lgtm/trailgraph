"""Finding codes owned by storage (contracts R.4, R.6, section 1.5.2).

Produced by ``MaintenanceSession.verify`` (``blob_missing`` too) and
``verify_blobs`` (every ``blob_*`` code). Every code has exactly one tamper
scenario under ``tests/tamper/``.
"""

from __future__ import annotations

from typing import Final

from graph_cli.kernel.findings import FindingCodeSpec, Severity


def _integrity(name: str, summary: str, severity: Severity = Severity.ERROR) -> FindingCodeSpec:
    return FindingCodeSpec(
        code=f"integrity.storage.{name}",
        scope="integrity",
        layer="storage",
        default_severity=severity,
        when="verify",
        fixture="tamper",
        summary=summary,
    )


TX_CHAIN_BROKEN: Final = "integrity.storage.tx_chain_broken"
REVISION_HASH_MISMATCH: Final = "integrity.storage.revision_hash_mismatch"
REVISION_MISSING: Final = "integrity.storage.revision_missing"
REVISION_UNEXPECTED: Final = "integrity.storage.revision_unexpected"
HEAD_INCONSISTENT: Final = "integrity.storage.head_inconsistent"
INDEX_INCONSISTENT: Final = "integrity.storage.index_inconsistent"
PIN_MISMATCH: Final = "integrity.storage.pin_mismatch"
BLOB_MISSING: Final = "integrity.storage.blob_missing"
BLOB_HASH_MISMATCH: Final = "integrity.storage.blob_hash_mismatch"
BLOB_ORPHAN: Final = "integrity.storage.blob_orphan"
BLOB_STAGING_LEFTOVER: Final = "integrity.storage.blob_staging_leftover"

FINDING_CODES: Final[tuple[FindingCodeSpec, ...]] = (
    _integrity("tx_chain_broken", "A tx log line is damaged or does not continue the chain."),
    _integrity("revision_hash_mismatch", "A revision file differs from its copy in the tx log."),
    _integrity("revision_missing", "A revision recorded in the tx log has no revision file."),
    _integrity("revision_unexpected", "A revision file has no copy in the tx log."),
    _integrity("head_inconsistent", "The head checkpoint does not match the tx log."),
    _integrity("index_inconsistent", "The disposable index does not match the tx log."),
    _integrity("pin_mismatch", "The manifest pin does not match the installed release."),
    _integrity("blob_missing", "A file referenced by a tx is not stored."),
    _integrity("blob_hash_mismatch", "The bytes of a stored file differ from its hash."),
    _integrity("blob_orphan", "A stored file is referenced by no tx.", Severity.INFO),
    _integrity("blob_staging_leftover", "A staged file was never published.", Severity.INFO),
)
