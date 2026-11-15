"""AWS Signature Version 4 for S3 (``AWS4-HMAC-SHA256``, service ``s3``).

Pure functions over strings and bytes; the client decides what to sign. The building blocks
are public so the test vectors can check each step (canonical request, string to sign,
signing key, signature). Nothing here logs or returns the canonical request or the string to
sign together with the credentials: ``sign_request`` returns the request headers only.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Final

from graph_cli.storage.s3.settings import ObjectStoreCredentials

ALGORITHM: Final = "AWS4-HMAC-SHA256"
SERVICE: Final = "s3"
EMPTY_PAYLOAD_SHA256: Final = hashlib.sha256(b"").hexdigest()

_UNRESERVED: Final = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
_SPACES: Final = re.compile(r" +")


def uri_encode(value: str, *, keep_slash: bool = False) -> str:
    """The SigV4 URI encoding: UTF-8 bytes, everything but ``A-Za-z0-9-._~`` as ``%XX``
    (upper-case hex); ``/`` stays only in an object key path."""
    out: list[str] = []
    for byte in value.encode("utf-8"):
        if byte in _UNRESERVED or (keep_slash and byte == 0x2F):
            out.append(chr(byte))
        else:
            out.append(f"%{byte:02X}")
    return "".join(out)


def canonical_uri(path: str) -> str:
    """The canonical URI of S3: the path encoded once (no double encoding), ``/`` kept."""
    return uri_encode(path, keep_slash=True)


def canonical_query_string(params: Iterable[tuple[str, str]]) -> str:
    """Names and values encoded, sorted by encoded name and then value, joined with ``&``."""
    encoded = sorted((uri_encode(name), uri_encode(value)) for name, value in params)
    return "&".join(f"{name}={value}" for name, value in encoded)


def canonical_headers(headers: Mapping[str, str]) -> tuple[str, str]:
    """``(canonical headers block, signed headers list)``: lower-case names sorted, values
    trimmed with runs of spaces collapsed."""
    normal = {name.lower(): _SPACES.sub(" ", value.strip()) for name, value in headers.items()}
    names = sorted(normal)
    block = "".join(f"{name}:{normal[name]}\n" for name in names)
    return block, ";".join(names)


def canonical_request(
    method: str,
    canonical_path: str,
    canonical_query: str,
    headers: Mapping[str, str],
    payload_sha256: str,
) -> str:
    block, signed = canonical_headers(headers)
    return "\n".join([method, canonical_path, canonical_query, block, signed, payload_sha256])


def amz_timestamp(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def credential_scope(datestamp: str, region: str) -> str:
    return f"{datestamp}/{region}/{SERVICE}/aws4_request"


def string_to_sign(timestamp: str, scope: str, canonical: str) -> str:
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return "\n".join([ALGORITHM, timestamp, scope, digest])


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def derive_signing_key(secret_access_key: str, datestamp: str, region: str) -> bytes:
    key = _hmac(("AWS4" + secret_access_key).encode("utf-8"), datestamp)
    key = _hmac(key, region)
    key = _hmac(key, SERVICE)
    return _hmac(key, "aws4_request")


def signature_of(signing_key: bytes, to_sign: str) -> str:
    return hmac.new(signing_key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()


def sign_request(
    *,
    method: str,
    path: str,
    query: Iterable[tuple[str, str]],
    headers: Mapping[str, str],
    payload_sha256: str,
    credentials: ObjectStoreCredentials,
    region: str,
    now: datetime,
) -> dict[str, str]:
    """Headers to send for one request: ``headers`` (all of them are signed, so pass ``host``
    and anything else that must be integrity-protected) plus ``x-amz-date``,
    ``x-amz-content-sha256``, ``x-amz-security-token`` when a session token exists, and
    ``authorization``. ``path`` is the unencoded absolute path; the canonical form is
    derived here and must be what goes on the wire (``canonical_uri``)."""
    timestamp = amz_timestamp(now)
    signed = {name.lower(): value for name, value in headers.items()}
    signed["x-amz-date"] = timestamp
    signed["x-amz-content-sha256"] = payload_sha256
    if credentials.session_token:
        signed["x-amz-security-token"] = credentials.session_token
    canonical = canonical_request(
        method, canonical_uri(path), canonical_query_string(query), signed, payload_sha256
    )
    datestamp = timestamp[:8]
    scope = credential_scope(datestamp, region)
    key = derive_signing_key(credentials.secret_access_key, datestamp, region)
    signature = signature_of(key, string_to_sign(timestamp, scope, canonical))
    signed_names = canonical_headers(signed)[1]
    signed["authorization"] = (
        f"{ALGORITHM} Credential={credentials.access_key_id}/{scope},"
        f"SignedHeaders={signed_names},Signature={signature}"
    )
    return signed
