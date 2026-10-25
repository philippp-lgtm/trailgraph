"""Problem details and the error registry of the server API (contracts B14.22 G8, G9).

An error answer is the envelope unless the request's ``Accept`` names
``application/problem+json`` with a ``q`` above 0; then it is an RFC 9457 problem
built from that envelope: ``type``, ``title``, ``status``, ``detail`` and the
extensions ``code``, ``category``, ``retryable``, ``hint``, ``details``,
``exit_code``, ``warnings``, and ``data`` for a partial result. ``type`` is
``<public_url>/v1/errors/<code>`` with ``[server] public_url`` (``title`` is then the
code's summary, the same for every occurrence), else ``about:blank`` with the status
phrase as ``title``. Clients match on ``code``. ``GET /v1/errors`` answers the
registry these types point to: every error code the program knows, warnings left out
(no answer has one as its type).
"""

from __future__ import annotations

from collections.abc import Mapping
from http import HTTPStatus
from typing import Final

from graph_cli.cli_kit.exit_codes import exit_code_for
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.error_codes import ErrorCategory, ErrorSpec, all_error_codes
from graph_cli_server.http_status import status_for

PROBLEM_TYPE: Final = "application/problem+json"
ERRORS_ROUTE: Final = "/v1/errors"
BLANK: Final = "about:blank"


def wants_problem(accept: str | None) -> bool:
    """Whether ``Accept`` names ``application/problem+json`` with ``q > 0``; a wildcard
    does not count, so a client that names nothing keeps the envelope."""
    for item in (accept or "").split(","):
        media, *params = (part.strip() for part in item.split(";"))
        if media.lower() != PROBLEM_TYPE:
            continue
        quality = 1.0
        for param in params:
            name, _, value = param.partition("=")
            if name.strip().lower() == "q":
                try:
                    quality = float(value.strip())
                except ValueError:
                    quality = 0.0
        if quality > 0:
            return True
    return False


def problem(
    document: Mapping[str, JsonValue], status: int, exit_code: int, public_url: str | None
) -> dict[str, JsonValue]:
    """The problem details of a failed envelope."""
    error = document["error"]
    assert isinstance(error, Mapping)
    code = str(error["code"])
    spec = all_error_codes().get(code)
    if public_url is not None and spec is not None:
        kind, title = f"{public_url}{ERRORS_ROUTE}/{code}", spec.summary
    else:
        kind, title = BLANK, HTTPStatus(status).phrase
    found: dict[str, JsonValue] = {
        "type": kind,
        "title": title,
        "status": status,
        "detail": error["message"],
        "code": code,
        "category": error["category"],
        "retryable": error["retryable"],
        "hint": error["hint"],
        "details": error["details"],
        "exit_code": exit_code,
        "warnings": document.get("warnings", []),
    }
    if document.get("data") is not None:
        found["data"] = document["data"]
    return found


def registry() -> list[JsonValue]:
    """Every error code as ``GET /v1/errors`` answers it, sorted by code."""
    return [entry(spec) for spec in all_error_codes().values() if _is_error(spec)]


def lookup(code: str) -> dict[str, JsonValue] | None:
    spec = all_error_codes().get(code)
    return entry(spec) if spec is not None and _is_error(spec) else None


def entry(spec: ErrorSpec) -> dict[str, JsonValue]:
    return {
        "code": spec.code,
        "category": spec.category.value,
        "retryable": spec.retryable,
        "status": status_for(spec.code, spec.category),
        "exit_code": exit_code_for(spec.category),
        "summary": spec.summary,
    }


def _is_error(spec: ErrorSpec) -> bool:
    return spec.category is not ErrorCategory.WARNING
