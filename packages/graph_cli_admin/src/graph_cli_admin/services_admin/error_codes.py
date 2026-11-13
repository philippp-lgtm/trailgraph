"""Admin-only error and warning codes (contracts R.1, section 1.5.1 "Admin-only codes").

They live in the maintenance distribution, so the app distribution carries
no maintenance vocabulary. The admin composition root registers them. Most are
``admin.*``; the ``ontology.rename_*`` and ``migration.*`` codes belong to the release
checks that only maintainers run.
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
    ErrorSpec(
        "admin.migration_unexpected",
        _C.REFUSED,
        False,
        "A migration file accompanies a release that is not a major one.",
    ),
    ErrorSpec(
        "admin.migration_from_mismatch",
        _C.REFUSED,
        False,
        "The migration starts from another version than the graph is pinned to.",
    ),
    ErrorSpec(
        "admin.migration_incomplete",
        _C.REFUSED,
        False,
        "Another migration is committed but not pinned yet.",
    ),
    ErrorSpec("migration.invalid", _C.INVALID, False, "The migration file is invalid."),
    ErrorSpec(
        "admin.migration_step_unused",
        _C.WARNING,
        False,
        "A migration step matches nothing in the data.",
    ),
    ErrorSpec("ontology.rename_invalid", _C.INVALID, False, "A renamed_from does not fit."),
    ErrorSpec(
        "ontology.rename_stale",
        _C.WARNING,
        False,
        "A renamed_from was applied by the previous release.",
    ),
    ErrorSpec("admin.integrity_errors", _C.INVALID, False, "Verification found errors."),
    ErrorSpec("admin.data_errors", _C.INVALID, False, "The data has error findings."),
    ErrorSpec("admin.skill_invalid", _C.INVALID, False, "The skill directory is invalid."),
    ErrorSpec("admin.functions_invalid", _C.INVALID, False, "The function files are invalid."),
    ErrorSpec("admin.actions_invalid", _C.INVALID, False, "The action files are invalid."),
    ErrorSpec(
        "admin.functions_broken", _C.REFUSED, False, "A published function would not resolve."
    ),
    ErrorSpec("admin.actions_broken", _C.REFUSED, False, "A published action would not compile."),
    ErrorSpec(
        "admin.skill_render_exists", _C.REFUSED, False, "The preview directory exists already."
    ),
    ErrorSpec("admin.source_exists", _C.REFUSED, False, "The directory holds pulled files."),
    ErrorSpec(
        "admin.source_conflict", _C.CONFLICT, False, "The graph changed since it was pulled."
    ),
    ErrorSpec("admin.workspace_conflict", _C.REFUSED, False, "A folder of the workspace is taken."),
    ErrorSpec(
        "admin.policy_lockout", _C.REFUSED, False, "The policy would lock out its maintainer."
    ),
    ErrorSpec("admin.remote_mode", _C.REFUSED, False, "This host is a client of a server."),
    ErrorSpec("admin.token_exists", _C.REFUSED, False, "A token with this name exists."),
    ErrorSpec("admin.token_not_found", _C.NOT_FOUND, False, "There is no token with this name."),
    ErrorSpec("admin.role_unknown", _C.REFUSED, False, "The policy does not declare this role."),
    ErrorSpec(
        "admin.enrollment_not_found", _C.NOT_FOUND, False, "There is no unused code with this id."
    ),
    ErrorSpec("admin.token_not_granted", _C.WARNING, False, "The policy grants the token nothing."),
    ErrorSpec(
        "admin.token_not_maintainer", _C.WARNING, False, "The policy lets the token not maintain."
    ),
    ErrorSpec(
        "admin.role_orphaned", _C.WARNING, False, "A role is no longer declared but still used."
    ),
    ErrorSpec("admin.verify_not_recorded", _C.WARNING, False, "The verification was not audited."),
    ErrorSpec("admin.alias_prefix_taken", _C.WARNING, False, "A new type gets no alias prefix."),
    ErrorSpec("admin.skill_over_budget", _C.WARNING, False, "A graph skill is over its budget."),
    ErrorSpec(
        "admin.module_bump_insufficient",
        _C.WARNING,
        False,
        "A module's own version does not cover its changes.",
    ),
    ErrorSpec(
        "admin.functions_affected", _C.WARNING, False, "A release would break published functions."
    ),
    ErrorSpec(
        "admin.actions_affected", _C.WARNING, False, "A release would break published actions."
    ),
    ErrorSpec("admin.policy_unknown_type", _C.WARNING, False, "The policy names an unknown type."),
    ErrorSpec(
        "admin.policy_unknown_term",
        _C.WARNING,
        False,
        "The policy names an attribute or relation its type does not have.",
    ),
    ErrorSpec(
        "admin.policy_unusable_write",
        _C.WARNING,
        False,
        "A write grant of the policy can never succeed.",
    ),
    ErrorSpec("admin.policy_fallback", _C.WARNING, False, "An installed policy is invalid."),
    ErrorSpec(
        "admin.view_port_unavailable", _C.UNAVAILABLE, True, "The view port cannot be bound."
    ),
    ErrorSpec("admin.view_reads_unavailable", _C.WARNING, False, "The view cannot show reads."),
)


def register() -> None:
    register_error_codes(ADMIN_CODES)
