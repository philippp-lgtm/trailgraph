"""Copy-pasteable hints for error codes (contracts B9.8, B9.9).

The only place that knows CLI syntax for errors. Every registered error
code has an entry: a function of ``(details, program)`` or an explicit
``None`` (U test). Admin and extension codes register theirs at startup.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final

from graph_cli.kernel.canonical import JsonValue

ADMIN_PROGRAM: Final = "graph-admin"

Hint = Callable[[Mapping[str, JsonValue], str], str | None]


def _init(details: Mapping[str, JsonValue], program: str) -> str | None:
    """A missing data root (B12.4); other unavailability has no single fix."""
    return f"{ADMIN_PROGRAM} init" if "data_root" in details else None


def _help(details: Mapping[str, JsonValue], program: str) -> str | None:
    return f"{program} --help"


def _graphs(details: Mapping[str, JsonValue], program: str) -> str | None:
    """Where the selectable graphs are listed (B12.5)."""
    return f"{program} graph list" if program == ADMIN_PROGRAM else f"{program} graphs"


def _types(details: Mapping[str, JsonValue], program: str) -> str | None:
    return f"{program} describe types"


def _get(details: Mapping[str, JsonValue], program: str) -> str | None:
    """Re-read the entity before retrying (B3.11)."""
    entity = details.get("id")
    return f"{program} get {entity}" if isinstance(entity, str) else None


_BASE: Final[dict[str, Hint | None]] = {
    "usage.invalid_argument": _help,
    "usage.unknown_command": _help,
    "usage.missing_argument": _help,
    "usage.invalid_input": None,
    "graph.not_selected": _graphs,
    "graph.not_found": _graphs,
    "graph.not_in_project": _graphs,
    "graph.archived": None,
    "graph.readonly": None,
    "entity.not_found": None,
    "entity.ambiguous_ref": None,
    "term.unknown": _types,
    "changeset.malformed": None,
    "changeset.local_ref_unresolved": None,
    "changeset.local_ref_duplicate": None,
    "changeset.too_large": None,
    "write.invalid": None,
    "temporal.as_of_unsupported": None,
    "conflict.version_mismatch": _get,
    "conflict.seq_mismatch": None,
    "conflict.idempotency_key_reuse": None,
    "access.denied": None,
    "access.ceiling": None,
    "access.reason_required": None,
    "access.batch_too_large": None,
    "ontology.pin_mismatch": None,
    "ontology.artifact_missing": None,
    "backend.unavailable": _init,
    "backend.lock_timeout": None,
    "backend.corrupt": None,
    "audit.unavailable": None,
    "format.unsupported_version": None,
    "config.invalid": None,
    "config.forbidden_key": None,
    "policy.invalid": None,
    "internal.error": None,
}
_registered: dict[str, Hint | None] = {}


def register_hints(hints: Mapping[str, Hint | None]) -> None:
    _registered.update(hints)


def known_hint_codes() -> frozenset[str]:
    return frozenset({*_BASE, *_registered})


def hint_for(code: str, details: Mapping[str, JsonValue], *, program: str) -> str | None:
    hint = _registered.get(code, _BASE.get(code))
    return hint(details, program) if hint is not None else None
