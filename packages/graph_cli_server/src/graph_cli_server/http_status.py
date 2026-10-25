"""The HTTP status of an error answer (contracts B14.5).

The status follows the error category; one code differs: ``remote.unauthorized`` is 401
for a request without a valid token and 403 for a valid token of another scope. The
routes answer with it, the OpenAPI documents list which statuses a command's codes reach.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from graph_cli.kernel.error_codes import ErrorCategory

STATUS_BY_CATEGORY: Final[Mapping[ErrorCategory, int]] = {
    ErrorCategory.USAGE: 400,
    ErrorCategory.DENIED: 403,
    ErrorCategory.NOT_FOUND: 404,
    ErrorCategory.CONFLICT: 409,
    ErrorCategory.REFUSED: 422,
    ErrorCategory.INVALID: 422,
    ErrorCategory.UNAVAILABLE: 503,
    ErrorCategory.INTERNAL: 500,
}
UNAUTHORIZED: Final = "remote.unauthorized"
# A token of another scope is 403 with the same code (``app._scope_refusal``).
SCOPE_REFUSED: Final = 403


def status_for(code: str, category: ErrorCategory) -> int:
    """The status of an error answer that is not a scope refusal."""
    return 401 if code == UNAUTHORIZED else STATUS_BY_CATEGORY[category]


def reachable(code: str, category: ErrorCategory) -> tuple[int, ...]:
    """Every status an answer with ``code`` can have."""
    status = status_for(code, category)
    return (status, SCOPE_REFUSED) if code == UNAUTHORIZED else (status,)
