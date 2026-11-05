"""Finding codes owned by storage (contracts R.4, R.6, section 1.5.2).

Produced by ``MaintenanceSession.verify`` (``blob_missing`` too) and
``verify_blobs`` (every ``blob_*`` code; ``blob_undecryptable`` and
``blob_content_mismatch`` only with ``decrypt``); the ``integrity.keys.*`` codes of
sealed graphs (T16) by ``verify_keys``. Every code has exactly one tamper scenario
under ``tests/tamper/``.
"""

from __future__ import annotations

from typing import Final

from graph_cli.kernel.findings import FindingCodeSpec, Layer, Severity


def _integrity(
    name: str, summary: str, severity: Severity = Severity.ERROR, *, layer: Layer = "storage"
) -> FindingCodeSpec:
    return FindingCodeSpec(
        code=f"integrity.{layer}.{name}",
        scope="integrity",
        layer=layer,
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
BLOB_UNDECRYPTABLE: Final = "integrity.storage.blob_undecryptable"
BLOB_CONTENT_MISMATCH: Final = "integrity.storage.blob_content_mismatch"
KEYS_MISSING: Final = "integrity.keys.missing"
KEYS_SHREDDED_UNRECORDED: Final = "integrity.keys.shredded_unrecorded"
KEYS_BLOB_NOT_REMOVED: Final = "integrity.keys.blob_not_removed"

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
    _integrity(
        "blob_undecryptable",
        "A sealed stored file does not decrypt: its key is missing or its bytes are damaged.",
    ),
    _integrity(
        "blob_content_mismatch",
        "The decrypted content of a stored file differs from the content hash naming it.",
    ),
)
KEY_FINDING_CODES: Final[tuple[FindingCodeSpec, ...]] = (
    _integrity("missing", "A record or stored file names a key the key store lacks.", layer="keys"),
    _integrity(
        "shredded_unrecorded",
        "A key is shredded, but no purge event in the graph's audit stream names it.",
        layer="keys",
    ),
    _integrity(
        "blob_not_removed",
        "A stored file whose key is shredded is still there (an interrupted purge).",
        Severity.WARNING,
        layer="keys",
    ),
)
