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
    ErrorSpec("admin.data_errors", _C.INVALID, False, "The data has error findings."),
    ErrorSpec("admin.skill_invalid", _C.INVALID, False, "The skill directory is invalid."),
    ErrorSpec(
        "admin.skill_render_exists", _C.REFUSED, False, "The preview directory exists already."
    ),
    ErrorSpec(
        "admin.policy_lockout", _C.REFUSED, False, "The policy would lock out its maintainer."
    ),
    ErrorSpec("admin.remote_mode", _C.REFUSED, False, "This host is a client of a server."),
    ErrorSpec("admin.token_exists", _C.REFUSED, False, "A token with this name exists."),
    ErrorSpec("admin.token_not_found", _C.NOT_FOUND, False, "There is no token with this name."),
    ErrorSpec("admin.token_not_granted", _C.WARNING, False, "The policy grants the token nothing."),
    ErrorSpec("admin.verify_not_recorded", _C.WARNING, False, "The verification was not audited."),
    ErrorSpec("admin.alias_prefix_taken", _C.WARNING, False, "A new type gets no alias prefix."),
    ErrorSpec("admin.policy_unknown_type", _C.WARNING, False, "The policy names an unknown type."),
    ErrorSpec("admin.policy_fallback", _C.WARNING, False, "An installed policy is invalid."),
    ErrorSpec(
        "admin.view_port_unavailable", _C.UNAVAILABLE, True, "The view port cannot be bound."
    ),
    ErrorSpec("admin.view_reads_unavailable", _C.WARNING, False, "The view cannot show reads."),
)


def register() -> None:
    register_error_codes(ADMIN_CODES)
