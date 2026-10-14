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
    # Recovery must be idempotent when it is itself interrupted.
    RECOVERY_AFTER_TRUNCATE = "recovery.after_truncate"
    RECOVERY_AFTER_MATERIALIZE = "recovery.after_materialize"


# Points added to the section 4.1 table by the storage plan (DECISIONS.md "P1 item 3").
EXTRA_POINTS: Final = frozenset(
    {
        CrashPoint.TX_BEFORE_WRITE,
        CrashPoint.RECOVERY_AFTER_TRUNCATE,
        CrashPoint.RECOVERY_AFTER_MATERIALIZE,
    }
)


class FaultHook(Protocol):
    def __call__(self, point: CrashPoint) -> None:
        """Return to continue, raise ``OSError`` to fail the step, or never return."""
        ...


def fire(hook: FaultHook | None, point: CrashPoint) -> None:
    if hook is not None:
        hook(point)
