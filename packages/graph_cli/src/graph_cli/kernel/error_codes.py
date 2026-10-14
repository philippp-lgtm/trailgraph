"""Error and warning code registry v1 (contracts R.1-R.3, section 1.5.1).

One registry for the whole process. The base set lives here; the admin
composition root and extensions add their codes once at startup through
``register_error_codes``. Codes are never reused with a different meaning.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

ERROR_CODE_RE: Final = re.compile(r"^[a-z]+(\.[a-z0-9_]+)+$")


class ErrorCategory(StrEnum):
    NOT_FOUND = "not_found"
    REFUSED = "refused"
    INVALID = "invalid"
    USAGE = "usage"
    DENIED = "denied"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"
    INTERNAL = "internal"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    code: str
    category: ErrorCategory
    retryable: bool
    summary: str  # one line, English, no CLI syntax

    def __post_init__(self) -> None:
        if not ERROR_CODE_RE.match(self.code):
            raise ValueError(f"invalid error code: {self.code!r}")


_C = ErrorCategory

_BASE: Final[tuple[ErrorSpec, ...]] = (
    ErrorSpec("usage.invalid_argument", _C.USAGE, False, "An argument has an invalid value."),
    ErrorSpec("usage.unknown_command", _C.USAGE, False, "The command does not exist."),
    ErrorSpec("usage.missing_argument", _C.USAGE, False, "A required argument is missing."),
    ErrorSpec("usage.invalid_input", _C.USAGE, False, "Input is not valid JSON, JSONL or UTF-8."),
    ErrorSpec("graph.not_selected", _C.USAGE, False, "No graph was selected."),
    ErrorSpec("graph.not_found", _C.NOT_FOUND, False, "The graph does not exist."),
    ErrorSpec("graph.not_in_project", _C.REFUSED, False, "The graph is not allowed here."),
    ErrorSpec("graph.archived", _C.REFUSED, False, "The graph is archived."),
    ErrorSpec("graph.readonly", _C.REFUSED, False, "The graph is read-only."),
    ErrorSpec("entity.not_found", _C.NOT_FOUND, False, "The entity does not exist."),
    ErrorSpec("entity.ambiguous_ref", _C.REFUSED, False, "The reference matches several entities."),
    ErrorSpec("term.unknown", _C.NOT_FOUND, False, "Unknown type, relation or attribute name."),
    ErrorSpec("changeset.malformed", _C.USAGE, False, "The change set document is malformed."),
    ErrorSpec("changeset.local_ref_unresolved", _C.REFUSED, False, "A local name is not bound."),
    ErrorSpec("changeset.local_ref_duplicate", _C.REFUSED, False, "A local name is bound twice."),
    ErrorSpec("changeset.too_large", _C.REFUSED, False, "The change set exceeds the size limits."),
    ErrorSpec("write.invalid", _C.INVALID, False, "The write violates the ontology."),
    ErrorSpec("temporal.as_of_unsupported", _C.REFUSED, False, "Time travel is not supported."),
    ErrorSpec("conflict.version_mismatch", _C.CONFLICT, True, "The entity has changed."),
    ErrorSpec("conflict.seq_mismatch", _C.CONFLICT, True, "The graph changed in the meantime."),
    ErrorSpec("conflict.idempotency_key_reuse", _C.CONFLICT, False, "Key reused, content differs."),
    ErrorSpec("access.denied", _C.DENIED, False, "The action is not permitted."),
    ErrorSpec("access.ceiling", _C.DENIED, False, "The action is not available to this program."),
    ErrorSpec("access.reason_required", _C.DENIED, False, "The action requires a reason."),
    ErrorSpec("access.batch_too_large", _C.DENIED, False, "The batch exceeds the permitted size."),
    ErrorSpec("ontology.pin_mismatch", _C.UNAVAILABLE, False, "Ontology artifact hash differs."),
    ErrorSpec("ontology.artifact_missing", _C.UNAVAILABLE, False, "Ontology release is missing."),
    ErrorSpec("backend.unavailable", _C.UNAVAILABLE, True, "The storage backend is unavailable."),
    ErrorSpec("backend.lock_timeout", _C.UNAVAILABLE, True, "The graph lock was not acquired."),
    ErrorSpec("backend.corrupt", _C.UNAVAILABLE, False, "The transaction log is damaged."),
    ErrorSpec("audit.unavailable", _C.UNAVAILABLE, True, "The audit log could not be written."),
    ErrorSpec("format.unsupported_version", _C.UNAVAILABLE, False, "Unsupported format version."),
    ErrorSpec("config.invalid", _C.UNAVAILABLE, False, "The configuration is invalid."),
    ErrorSpec("config.forbidden_key", _C.UNAVAILABLE, False, "A configuration key is not allowed."),
    ErrorSpec("policy.invalid", _C.UNAVAILABLE, False, "The policy document is invalid."),
    ErrorSpec("internal.error", _C.INTERNAL, False, "Internal error."),
    # Warnings never change the exit code.
    ErrorSpec("actor.facet_malformed", _C.WARNING, False, "An identity value was ignored."),
    ErrorSpec("root.overridden", _C.WARNING, False, "The data root was overridden."),
    ErrorSpec("entity.superseded", _C.WARNING, False, "The entity is superseded."),
    ErrorSpec("entity.retracted", _C.WARNING, False, "The entity is retracted."),
    ErrorSpec("entity.alias_renamed", _C.WARNING, False, "The alias was renamed."),
    ErrorSpec(
        "storage.materialization_deferred", _C.WARNING, False, "Derived files are not updated yet."
    ),
    ErrorSpec("storage.recovery_pending", _C.WARNING, False, "The graph needs recovery."),
    ErrorSpec("storage.index_stale", _C.WARNING, False, "The index is stale; a scan was used."),
    ErrorSpec("audit.refusal_not_recorded", _C.WARNING, False, "A refusal was not audited."),
    ErrorSpec("ontology.pin_mismatch_read", _C.WARNING, False, "Read with a mismatching artifact."),
    ErrorSpec("graph.archived_read", _C.WARNING, False, "The graph is archived."),
    ErrorSpec("config.unknown_key", _C.WARNING, False, "A configuration key is unknown."),
    ErrorSpec("extension.api_mismatch", _C.WARNING, False, "An extension targets another API."),
)

ERROR_CODES: Final[Mapping[str, ErrorSpec]] = MappingProxyType({s.code: s for s in _BASE})
assert len(ERROR_CODES) == len(_BASE), "duplicate error code in the base registry"

_registered: dict[str, ErrorSpec] = {}


def register_error_codes(specs: Iterable[ErrorSpec]) -> None:
    """Add codes owned outside the kernel (``admin.*``, ``ext.<ns>.*``).

    Re-registering an identical spec is a no-op; a different spec under a known
    code is refused, because codes never change meaning.
    """
    for spec in specs:
        known = ERROR_CODES.get(spec.code) or _registered.get(spec.code)
        if known is not None and known != spec:
            raise ValueError(f"error code {spec.code!r} is already registered differently")
        _registered[spec.code] = spec


def lookup_error_code(code: str) -> ErrorSpec | None:
    return ERROR_CODES.get(code) or _registered.get(code)


def all_error_codes() -> Mapping[str, ErrorSpec]:
    """Base registry plus everything registered so far, sorted by code."""
    merged = {**ERROR_CODES, **_registered}
    return MappingProxyType(dict(sorted(merged.items())))
