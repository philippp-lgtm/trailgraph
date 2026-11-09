"""Named fault points of the commit and recovery paths (contracts section 4.1, K.d).

Adapters call ``fire(hook, point)`` at each point. Production wiring passes
no hook. Tests pass one that raises ``OSError`` (fault tests, in-process) or
sends ``SIGKILL`` to its own process (crash tests). There are no env-based
hooks.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Protocol


class CrashPoint(StrEnum):
    # Section 4.1 windows K0-K5, in commit order.
    AUDIT_BEFORE_WRITE = "audit.before_write"
    AUDIT_AFTER_WRITE_BEFORE_SYNC = "audit.after_write_before_sync"
    AUDIT_AFTER_SYNC = "audit.after_sync"
    TX_BEFORE_WRITE = "tx.before_write"
    TX_AFTER_WRITE_BEFORE_SYNC = "tx.after_write_before_sync"
    TX_AFTER_SYNC = "tx.after_sync"
    MATERIALIZE_AFTER_FIRST_FILE = "materialize.after_first_file"
    MATERIALIZE_BEFORE_DIR_SYNC = "materialize.before_dir_sync"
    HEAD_BEFORE_RENAME = "head.before_rename"
    HEAD_AFTER_RENAME = "head.after_rename"
    # The disposable index after the checkpoint (W.15): the commit point is the rename.
    INDEX_BEFORE_RENAME = "index.before_rename"
    INDEX_AFTER_RENAME = "index.after_rename"
    # Section 4.1 windows KB0-KB2: a blob of the request, staged before the lock.
    BLOB_AFTER_STAGE = "blob.after_stage"
    BLOB_BEFORE_RENAME = "blob.before_rename"
    BLOB_AFTER_RENAME = "blob.after_rename"
    # Recovery must be idempotent when it is itself interrupted.
    RECOVERY_AFTER_TRUNCATE = "recovery.after_truncate"
    RECOVERY_AFTER_MATERIALIZE = "recovery.after_materialize"
    # Maintenance effects (W.e, W.f): the commit point is the rename.
    CREATE_BEFORE_ANNOUNCE = "create.before_announce"
    CREATE_BEFORE_RENAME = "create.before_rename"
    RELEASE_BEFORE_RENAME = "release.before_rename"
    MANIFEST_BEFORE_RENAME = "manifest.before_rename"
    SKILL_BEFORE_RENAME = "skill.before_rename"
    FUNCTIONS_BEFORE_RENAME = "functions.before_rename"
    ACTIONS_BEFORE_RENAME = "actions.before_rename"
    POLICY_BEFORE_RENAME = "policy.before_rename"
    # The key store of a sealed graph (plan T16): the commit point is the SQLite commit.
    KEYS_BEFORE_COMMIT = "keys.before_commit"
    KEYS_AFTER_COMMIT = "keys.after_commit"
    SHRED_BEFORE_COMMIT = "shred.before_commit"
    SHRED_BEFORE_VACUUM = "shred.before_vacuum"
    PURGE_AFTER_COMMIT = "purge.after_commit"
    PURGE_AFTER_SHRED = "purge.after_shred"


# Points added to the section 4.1 table by the storage plan (DECISIONS.md "P1 item 3")
# and the disposable index plan (DECISIONS.md "Disposable index: plan approved").
EXTRA_POINTS: Final = frozenset(
    {
        CrashPoint.TX_BEFORE_WRITE,
        CrashPoint.RECOVERY_AFTER_TRUNCATE,
        CrashPoint.RECOVERY_AFTER_MATERIALIZE,
        CrashPoint.INDEX_BEFORE_RENAME,
        CrashPoint.INDEX_AFTER_RENAME,
    }
)
# Points of the maintenance effects (P1 item 5), outside the write-path table.
MAINT_POINTS: Final = frozenset(
    {
        CrashPoint.CREATE_BEFORE_ANNOUNCE,
        CrashPoint.CREATE_BEFORE_RENAME,
        CrashPoint.RELEASE_BEFORE_RENAME,
        CrashPoint.MANIFEST_BEFORE_RENAME,
        CrashPoint.SKILL_BEFORE_RENAME,
        CrashPoint.FUNCTIONS_BEFORE_RENAME,
        CrashPoint.ACTIONS_BEFORE_RENAME,
        CrashPoint.POLICY_BEFORE_RENAME,
    }
)
# Points of the key store and of a purge (plan T16), outside the write-path table.
KEYS_POINTS: Final = frozenset(
    {
        CrashPoint.KEYS_BEFORE_COMMIT,
        CrashPoint.KEYS_AFTER_COMMIT,
        CrashPoint.SHRED_BEFORE_COMMIT,
        CrashPoint.SHRED_BEFORE_VACUUM,
        CrashPoint.PURGE_AFTER_COMMIT,
        CrashPoint.PURGE_AFTER_SHRED,
    }
)


class FaultHook(Protocol):
    def __call__(self, point: CrashPoint) -> None:
        """Return to continue, raise ``OSError`` to fail the step, or never return."""
        ...


def fire(hook: FaultHook | None, point: CrashPoint) -> None:
    if hook is not None:
        hook(point)
