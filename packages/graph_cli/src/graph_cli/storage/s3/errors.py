"""Object store errors (O9) and what they may carry (O2).

Every message and every detail names only the operation, the endpoint host, the HTTP status,
the S3 error code and a fixed reason phrase. The XML error body of a store answers with the
access key id, the string to sign and the canonical request on a signature failure; it is
parsed for its ``Code`` alone and never echoed. Lower-level exceptions are never chained
(their text can name hosts or paths), so the errors here are built from classified values and
raised outside any ``except`` block.
"""

from __future__ import annotations

import re
from typing import Final

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError

READ_OPERATIONS: Final = frozenset({"GetObject", "HeadObject", "ListObjectsV2"})
WRITE_OPERATIONS: Final = frozenset({"PutObject", "DeleteObject"})

_CODE_RE: Final = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")

QUOTA_CODES: Final = frozenset(
    {
        "QuotaExceeded",
        "StorageQuotaExceeded",
        "XMinioAdminBucketQuotaExceeded",
        "XMinioStorageFull",
        "InsufficientStorage",
        "EntityTooLarge",
    }
)
CREDENTIAL_CODES: Final = frozenset(
    {"InvalidAccessKeyId", "SignatureDoesNotMatch", "ExpiredToken", "InvalidToken"}
)
RETRYABLE_CODES: Final = frozenset(
    {"SlowDown", "RequestTimeout", "InternalError", "ServiceUnavailable", "Throttling"}
)
_HTTP_INSUFFICIENT_STORAGE: Final = 507


class ObjectStoreError(Exception):
    """Typed results of the client that are not ``GraphCliError``s: the caller decides what
    they mean (a missing referenced object is a damaged store, an absent probe is not)."""


class ObjectNotFound(ObjectStoreError):
    """The object does not exist (404, ``NoSuchKey``)."""

    def __init__(self, operation: str) -> None:
        super().__init__(f"{operation}: the object does not exist")
        self.operation = operation


class ObjectContentMismatch(ObjectStoreError):
    """A streamed object did not match the expected SHA-256. Raised before the last chunk is
    handed out, so the caller never holds the unverified tail."""

    def __init__(self, expected: str, actual: str) -> None:
        super().__init__("the object's bytes do not match the expected SHA-256")
        self.expected = expected
        self.actual = actual


def sanitize_code(value: str | None) -> str | None:
    """An S3 error code is a short identifier; anything else is dropped, never echoed."""
    return value if value is not None and _CODE_RE.match(value) else None


def is_retryable_response(status: int, s3_code: str | None) -> bool:
    if status == _HTTP_INSUFFICIENT_STORAGE:
        return False
    return status >= 500 or status == 429 or s3_code in RETRYABLE_CODES


def _details(
    operation: str,
    host: str,
    *,
    status: int | None = None,
    s3_code: str | None = None,
    reason: str | None = None,
    attempts: int | None = None,
) -> dict[str, JsonValue]:
    details: dict[str, JsonValue] = {"operation": operation, "endpoint_host": host}
    if status is not None:
        details["status"] = status
    if s3_code is not None:
        details["s3_code"] = s3_code
    if reason is not None:
        details["reason"] = reason
    if attempts is not None and attempts > 1:
        details["attempts"] = attempts
    return details


def unavailable(
    message: str,
    operation: str,
    host: str,
    *,
    status: int | None = None,
    s3_code: str | None = None,
    reason: str | None = None,
    attempts: int | None = None,
) -> GraphCliError:
    details = _details(
        operation, host, status=status, s3_code=s3_code, reason=reason, attempts=attempts
    )
    return GraphCliError("backend.unavailable", message, details=details)


def network_error(operation: str, host: str, reason: str, attempts: int) -> GraphCliError:
    """The store could not be reached or stopped answering (``reason`` is a fixed phrase)."""
    return unavailable(
        f"the object store at {host} cannot be reached ({reason}) during {operation}",
        operation,
        host,
        reason=reason,
        attempts=attempts,
    )


def missing_credentials(operation: str, host: str, missing: list[str]) -> GraphCliError:
    names = " and ".join(missing)
    return GraphCliError(
        "backend.unavailable",
        f"the object store credentials are incomplete: {names} not set",
        details={**_details(operation, host), "missing": list(missing)},
    )


def response_error(
    operation: str, host: str, status: int, s3_code: str | None, attempts: int
) -> GraphCliError | ObjectNotFound:
    """Map an error answer by its S3 ``Code`` (status alone only where no code exists)."""
    write = operation in WRITE_OPERATIONS

    def refuse(message: str) -> GraphCliError:
        return unavailable(message, operation, host, status=status, s3_code=s3_code)

    if s3_code == "NoSuchBucket":
        return refuse(
            f"the bucket does not exist at {host}; check the bucket name and the endpoint"
        )
    if (
        s3_code in QUOTA_CODES
        or status == _HTTP_INSUFFICIENT_STORAGE
        or (write and s3_code == "AccessDenied")
    ):
        return GraphCliError(
            "files.store_refused",
            f"the object store at {host} refused {operation}: the bucket's quota may be"
            " exhausted, the key may lack write permission (PutObject, DeleteObject), or the"
            " credentials may be wrong (some stores answer AccessDenied for those too); run"
            " 'graph-admin files store check'",
            details=_details(operation, host, status=status, s3_code=s3_code),
        )
    if s3_code in CREDENTIAL_CODES:
        return refuse(
            f"the object store refused the credentials (HTTP {status}, {s3_code}); check the"
            " access key, the secret and the session token"
        )
    if s3_code == "RequestTimeTooSkewed":
        return refuse(
            f"the object store at {host} rejected the request time (clock skew): the clock of"
            " this machine differs from the store's by too much; correct the clock"
        )
    if s3_code == "AuthorizationHeaderMalformed":
        return refuse(
            f"the object store at {host} does not accept the signing region; check the region"
        )
    if s3_code in ("PermanentRedirect", "TemporaryRedirect") or 300 <= status < 400:
        return refuse(
            f"the bucket lives at another endpoint or region than {host}; check the endpoint,"
            " the region and the addressing style"
        )
    if s3_code == "AccessDenied" or (s3_code is None and status in (401, 403) and not write):
        # A HEAD answer has no body, so a denied HEAD arrives without a code. Some stores
        # (Garage) answer an unknown key or a wrong secret with AccessDenied as well.
        return refuse(
            f"the object store at {host} denied access during {operation}; check the"
            " credentials and the key's permissions (GetObject, ListBucket) or run"
            " 'graph-admin files store check'"
        )
    if (
        status == 404
        and s3_code in (None, "NoSuchKey", "NotFound")
        and operation != "ListObjectsV2"
    ):
        return ObjectNotFound(operation)
    if s3_code in ("XAmzContentSHA256Mismatch", "BadDigest"):
        return refuse(
            f"the object store at {host} received bytes that differ from their hash during"
            f" {operation}; the file changed while it was uploaded or the transfer was damaged"
        )
    suffix = f" ({s3_code})" if s3_code else ""
    return unavailable(
        f"the object store at {host} answered {operation} with status {status}{suffix};"
        " run 'graph-admin files store check'",
        operation,
        host,
        status=status,
        s3_code=s3_code,
        attempts=attempts,
    )


def unreadable_answer(operation: str, host: str, status: int | None = None) -> GraphCliError:
    """A success status with a body or headers this client cannot use."""
    return unavailable(
        f"the object store at {host} answered {operation} in a form that is not understood",
        operation,
        host,
        status=status,
        reason="unreadable response",
    )
