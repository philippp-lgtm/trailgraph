"""Admin-only error and warning codes (contracts R.1, section 1.5.1 "Admin-only codes").

They live in the maintenance distribution, so the app distribution carries
no maintenance vocabulary. The admin composition root registers them.
"""

from __future__ import annotations

from typing import Final

from graph_cli.kernel.error_codes import ErrorCategory, ErrorSpec, register_error_codes

_C = ErrorCategory

ADMIN_CODES: Final[tuple[ErrorSpec, ...]] = (
    ErrorSpec("admin.model_invalid", _C.INVALID, False, "The ontology has error findings."),
    ErrorSpec(
        "admin.version_bump_insufficient", _C.INVALID, False, "The version bump is too small."
    ),
    ErrorSpec("admin.release_exists", _C.REFUSED, False, "The version exists with other content."),
    ErrorSpec("admin.graph_name_taken", _C.REFUSED, False, "The graph name is already used."),
    ErrorSpec("admin.migration_required", _C.REFUSED, False, "The change needs a migration."),
    ErrorSpec("admin.integrity_errors", _C.INVALID, False, "Verification found errors."),
    ErrorSpec("admin.verify_not_recorded", _C.WARNING, False, "The verification was not audited."),
)


def register() -> None:
    register_error_codes(ADMIN_CODES)
