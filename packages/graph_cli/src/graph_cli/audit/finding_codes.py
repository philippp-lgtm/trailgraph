"""Finding codes owned by audit (contracts R.4, R.6, section 1.5.2, B5.12).

Produced by ``graph_cli.audit.verify``. Every code has exactly one tamper
scenario under ``tests/tamper/``.
"""

from __future__ import annotations

from typing import Final

from graph_cli.kernel.findings import FindingCodeSpec, Severity


def _integrity(name: str, severity: Severity, summary: str) -> FindingCodeSpec:
    return FindingCodeSpec(
        code=f"integrity.audit.{name}",
        scope="integrity",
        layer="audit",
        default_severity=severity,
        when="verify",
        fixture="tamper",
        summary=summary,
    )


CHAIN_BROKEN: Final = "integrity.audit.chain_broken"
SEQ_GAP: Final = "integrity.audit.seq_gap"
HASH_MISMATCH: Final = "integrity.audit.hash_mismatch"
TORN_INTERIOR_LINE: Final = "integrity.audit.torn_interior_line"
UNKNOWN_SCHEMA: Final = "integrity.audit.unknown_schema"
EVENT_MALFORMED: Final = "integrity.audit.event_malformed"
UNKNOWN_ACTION: Final = "integrity.audit.unknown_action"
INTENT_WITHOUT_EFFECT: Final = "integrity.audit.intent_without_effect"
TX_WITHOUT_INTENT: Final = "integrity.audit.tx_without_intent"
ANCHOR_MISMATCH: Final = "integrity.audit.anchor_mismatch"
PIN_MISMATCH: Final = "integrity.audit.pin_mismatch"
POLICY_MISMATCH: Final = "integrity.audit.policy_mismatch"
POLICY_HASH_MISMATCH: Final = "integrity.audit.policy_hash_mismatch"
POLICY_FALLBACK: Final = "integrity.audit.policy_fallback"
POLICY_HASH_UNCHECKED: Final = "integrity.audit.policy_hash_unchecked"
TOKEN_MISMATCH: Final = "integrity.audit.token_mismatch"
ENROLLMENT_MISMATCH: Final = "integrity.audit.enrollment_mismatch"
SLOT_MISMATCH: Final = "integrity.audit.slot_mismatch"
MIGRATION_CHANGED_IDENTITY: Final = "integrity.audit.migration_changed_identity"
MIGRATION_VERSION_MISMATCH: Final = "integrity.audit.migration_version_mismatch"
MIGRATION_REVISIONS_MISMATCH: Final = "integrity.audit.migration_revisions_mismatch"
MIGRATION_UNPINNED: Final = "integrity.audit.migration_unpinned"
MIGRATION_INCOMPLETE: Final = "integrity.audit.migration_incomplete"

FINDING_CODES: Final[tuple[FindingCodeSpec, ...]] = (
    _integrity(
        "chain_broken", Severity.ERROR, "An event does not continue the stream's hash chain."
    ),
    _integrity("seq_gap", Severity.ERROR, "An event's seq does not follow its predecessor's."),
    _integrity("hash_mismatch", Severity.ERROR, "An event's content does not match its hash."),
    _integrity(
        "torn_interior_line", Severity.ERROR, "A line inside the stream is not a JSON object."
    ),
    _integrity(
        "unknown_schema", Severity.ERROR, "An event carries a schema this version cannot read."
    ),
    _integrity(
        "event_malformed", Severity.ERROR, "An event does not have the shape of its schema."
    ),
    _integrity(
        "unknown_action", Severity.WARNING, "An event names an action outside the known taxonomy."
    ),
    _integrity(
        "intent_without_effect", Severity.INFO, "An intent has no committed effect (benign crash)."
    ),
    _integrity(
        "tx_without_intent",
        Severity.ERROR,
        "A tx has no matching write intent in the graph stream.",
    ),
    _integrity(
        "anchor_mismatch", Severity.ERROR, "An installation anchor does not match the graph stream."
    ),
    _integrity(
        "pin_mismatch", Severity.ERROR, "The manifest pin differs from the recorded pin events."
    ),
    _integrity(
        "policy_mismatch",
        Severity.ERROR,
        "An installed policy differs from the recorded policy events.",
    ),
    _integrity(
        "policy_hash_mismatch",
        Severity.ERROR,
        "A decision was made under a policy that was not in effect.",
    ),
    _integrity(
        "policy_fallback",
        Severity.WARNING,
        "A maintenance decision was made without an installed policy layer.",
    ),
    _integrity(
        "policy_hash_unchecked",
        Severity.INFO,
        "Decisions before the first mirrored installation policy change are checked loosely.",
    ),
    _integrity(
        "token_mismatch", Severity.ERROR, "The token file differs from the recorded token events."
    ),
    _integrity(
        "enrollment_mismatch",
        Severity.ERROR,
        "The enrollment file differs from the recorded enrollment events.",
    ),
    _integrity(
        "slot_mismatch",
        Severity.ERROR,
        "A published skill or function catalog differs from the recorded change events.",
    ),
    _integrity(
        "migration_changed_identity",
        Severity.ERROR,
        "A migration revision changes an alias, status, creation time or valid time.",
    ),
    _integrity(
        "migration_version_mismatch",
        Severity.ERROR,
        "A migration tx, its revisions and its intent do not name the same target version.",
    ),
    _integrity(
        "migration_revisions_mismatch",
        Severity.ERROR,
        "A migration intent's revisions digest or entity count differs from its tx.",
    ),
    _integrity(
        "migration_unpinned",
        Severity.ERROR,
        "A migration tx is not followed by a pin to its target version.",
    ),
    _integrity(
        "migration_incomplete",
        Severity.WARNING,
        "A migration tx is committed but its pin switch has not happened yet.",
    ),
)
