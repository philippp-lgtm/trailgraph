"""Typed errors and warnings (contracts R.3, B9).

Services and below raise ``GraphCliError`` with a registered code. They never
format CLI syntax or choose exit codes; ``cli_kit`` maps the category.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.error_codes import ErrorCategory, ErrorSpec, lookup_error_code

EMPTY: Final[Mapping[str, JsonValue]] = MappingProxyType({})


class GraphCliError(Exception):
    """An error with a registered code.

    An unregistered code, or a warning code, turns the error itself into
    ``internal.error`` (R.3): a wrong code is a bug, not a user problem.
    ``warnings`` ride along with the error into the envelope (e.g.
    ``audit.refusal_not_recorded``, B3.14).
    """

    code: str
    spec: ErrorSpec
    message: str
    details: Mapping[str, JsonValue]
    warnings: tuple[WarningRecord, ...]

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, JsonValue] | None = None,
        warnings: tuple[WarningRecord, ...] = (),
    ) -> None:
        spec = lookup_error_code(code)
        if spec is None or spec.category is ErrorCategory.WARNING:
            details = {"unregistered_code": code, **(details or {})}
            message = f"unregistered error code {code!r}: {message}"
            spec = lookup_error_code("internal.error")
            assert spec is not None
        super().__init__(message)
        self.code = spec.code
        self.spec = spec
        self.message = message
        self.details = MappingProxyType(dict(details or {}))
        self.warnings = warnings

    @property
    def category(self) -> ErrorCategory:
        return self.spec.category

    @property
    def retryable(self) -> bool:
        return self.spec.retryable


@dataclass(frozen=True, slots=True)
class WarningRecord:
    """A non-fatal notice carried next to a result (contract name: ``Warning``).

    Renamed to avoid shadowing the builtin ``Warning``.
    """

    code: str
    message: str
    details: Mapping[str, JsonValue] = field(default=EMPTY)

    def to_json(self) -> dict[str, JsonValue]:
        return {"code": self.code, "message": self.message, "details": dict(self.details)}
