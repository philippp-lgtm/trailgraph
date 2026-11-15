"""A small S3 client over ``http.client`` and ``ssl`` (object storage stage 1, O2, O9).

Five calls, all idempotent because keys are content addresses: ``put_object``,
``get_object`` (streamed, optionally verified against an expected SHA-256 while it streams),
``head_object``, ``delete_object`` and ``list_objects_v2`` (paged). Every key the caller
passes and every key it gets back is relative to ``ObjectStoreSettings.prefix``; the client
applies and strips the prefix, so a caller cannot leave its namespace.

Uploads are signed with the real payload hash. Failures that can pass (connection errors,
timeouts, 5xx, 429 and ``SlowDown``) are retried with exponential backoff and jitter; the
rest are mapped by S3 error code (``errors.response_error``). Nothing secret reaches a
message, a detail or an exception chain (see ``errors``).
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import random
import re
import socket
import ssl
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import BinaryIO, Final
from urllib.parse import urlsplit

from graph_cli.kernel.clock import Clock, SystemClock
from graph_cli.storage.s3 import errors, signing, xml_safe
from graph_cli.storage.s3.errors import ObjectContentMismatch, ObjectNotFound, ObjectStoreError
from graph_cli.storage.s3.settings import ObjectStoreCredentials, ObjectStoreSettings

DEFAULT_CHUNK_SIZE: Final = 1024 * 1024
_SIGNING_REGION_DEFAULT: Final = "us-east-1"
_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_MAX_KEY_BYTES: Final = 1024
_USER_AGENT: Final = "graph_cli-object-store"


@dataclass(frozen=True, slots=True)
class Timeouts:
    connect_s: float = 10.0
    read_s: float = 60.0


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """``max_retries`` retries after the first attempt; the wait before retry ``n`` is a random
    fraction (full jitter) of ``min(cap_s, base_s * 2**(n-1))``."""

    max_retries: int = 3
    base_s: float = 0.5
    cap_s: float = 8.0


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    """One object as HEAD or a listing reports it. ``response_date`` is the store's own clock
    (the ``Date`` header of that answer), for grace periods that must not depend on the
    caller's clock."""

    key: str
    size: int
    last_modified: datetime
    response_date: datetime | None


@dataclass(frozen=True, slots=True)
class ListPage:
    entries: tuple[ObjectInfo, ...]
    response_date: datetime | None


class ObjectSourceError(ObjectStoreError):
    """The data handed to ``put_object`` ended before its declared size."""


class _SourceTooShort(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _Endpoint:
    tls: bool
    connect_host: str
    port: int
    host_header: str
    base_path: str
    bucket_in_path: bool
    display_host: str


@dataclass(slots=True)
class _Failed:
    reason: str
    retryable: bool
    source_short: bool = False


@dataclass(slots=True)
class _Answer:
    status: int
    s3_code: str | None
    response: http.client.HTTPResponse
    conn: http.client.HTTPConnection
    body: bytes

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def close(self) -> None:
        self.conn.close()


def _classify(exc: BaseException) -> _Failed:
    """A fixed reason phrase for a lower-level failure; its own text is never used."""
    if isinstance(exc, ssl.SSLCertVerificationError):
        return _Failed("TLS certificate verification failed", False)
    if isinstance(exc, TimeoutError):
        return _Failed("timeout", True)
    if isinstance(exc, ssl.SSLError):
        return _Failed("TLS handshake failed", True)
    if isinstance(exc, socket.gaierror):
        return _Failed("name resolution failed", True)
    if isinstance(exc, ConnectionRefusedError):
        return _Failed("connection refused", True)
    if isinstance(exc, ConnectionError):
        return _Failed("connection reset", True)
    if isinstance(exc, http.client.HTTPException):
        return _Failed("invalid or interrupted response", True)
    return _Failed("network error", True)


def _http_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_sha256(value: str) -> str:
    if not _SHA256_RE.match(value):
        raise ValueError("a SHA-256 is 64 lower-case hex digits")
    return value


def _check_key(key: str) -> str:
    segments = key.split("/")
    if (
        not key
        or "\x00" in key
        or any(segment in ("", ".", "..") for segment in segments)
        or len(key.encode("utf-8")) > _MAX_KEY_BYTES
    ):
        raise ValueError("not a usable object key")
    return key


class ObjectBody:
    """The streamed body of ``get_object``: iterate it once, then it is closed.

    With an expected SHA-256 every chunk is hashed and the last chunk is held back until the
    digest matches, so a mismatch raises ``ObjectContentMismatch`` before the caller sees the
    unverified tail. Chunks before the last were already handed out: write them to a staging
    file and keep the file only when the iteration ended without an error. A body that ends
    before its declared length or a stalled read raises ``backend.unavailable``.
    """

    def __init__(
        self,
        answer: _Answer,
        *,
        host: str,
        expected_sha256: str | None,
        chunk_size: int,
        info: ObjectInfo | None,
    ) -> None:
        self._answer = answer
        self._host = host
        self._expected = expected_sha256
        self._chunk_size = chunk_size
        self._started = False
        length = answer.response.getheader("Content-Length")
        self.size: int | None = int(length) if length and length.isdigit() else None
        self.info = info

    def close(self) -> None:
        self._answer.close()

    def __enter__(self) -> ObjectBody:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __iter__(self) -> Iterator[bytes]:
        if self._started:
            raise RuntimeError("an object body can be read once")
        self._started = True
        return self._chunks()

    def _read(self) -> bytes:
        failure: _Failed | None = None
        data = b""
        try:
            data = self._answer.response.read(self._chunk_size)
        except (OSError, http.client.HTTPException) as exc:
            failure = _classify(exc)
        if failure is not None:
            self.close()
            raise errors.network_error("GetObject", self._host, failure.reason, 1)
        return data

    def _chunks(self) -> Iterator[bytes]:
        hasher = hashlib.sha256() if self._expected is not None else None
        held: bytes | None = None
        total = 0
        try:
            while chunk := self._read():
                total += len(chunk)
                if hasher is None:
                    yield chunk
                    continue
                hasher.update(chunk)
                if held is not None:
                    yield held
                held = chunk
            if self.size is not None and total != self.size:
                raise errors.network_error("GetObject", self._host, "response ended early", 1)
            if hasher is not None:
                actual = hasher.hexdigest()
                if actual != self._expected:
                    assert self._expected is not None
                    raise ObjectContentMismatch(self._expected, actual)
                if held is not None:
                    yield held
        finally:
            self.close()


class ObjectStoreClient:
    """One bucket of an S3-compatible store. Cheap to build; no connection is kept: every call
    (and every page of a listing) opens its own connection and closes it."""

    def __init__(
        self,
        settings: ObjectStoreSettings,
        *,
        clock: Clock | None = None,
        timeouts: Timeouts = Timeouts(),  # noqa: B008 (frozen value)
        retry: RetryPolicy = RetryPolicy(),  # noqa: B008 (frozen value)
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> None:
        self._settings = settings
        self._clock: Clock = clock or SystemClock()
        self._timeouts = timeouts
        self._retry = retry
        self._sleep = sleep
        self._rng = rng
        self._chunk_size = chunk_size
        self._context: ssl.SSLContext | None = None

    def __repr__(self) -> str:
        return f"ObjectStoreClient(bucket={self._settings.bucket!r})"

    @property
    def bucket(self) -> str:
        return self._settings.bucket

    def endpoint_host(self) -> str:
        """The store's host (and port) as messages name it; raises ``backend.unavailable``
        for an endpoint the client cannot use."""
        return self._endpoint("ListObjectsV2").display_host

    # -- public calls ---------------------------------------------------------------------

    def put_object(self, key: str, body: BinaryIO, size: int, sha256_hex: str) -> None:
        """Store ``size`` bytes read from ``body`` under ``key``, signed with their SHA-256 (the
        store verifies it). ``body`` should be seekable so a retry can send it again; a
        non-seekable body gets one attempt. Raises ``ObjectSourceError`` when it is shorter
        than ``size``."""
        payload = _check_sha256(sha256_hex)
        self._call(
            "PutObject",
            "PUT",
            self._object_path(_check_key(key)),
            (),
            payload,
            source=body,
            size=size,
            extra={"content-type": "application/octet-stream"},
        ).close()

    def get_object(
        self, key: str, *, expected_sha256: str | None = None, chunk_size: int | None = None
    ) -> ObjectBody:
        """Open ``key`` for streaming. Raises ``ObjectNotFound`` when it does not exist."""
        expected = _check_sha256(expected_sha256) if expected_sha256 is not None else None
        answer = self._call("GetObject", "GET", self._object_path(_check_key(key)), ())
        host = self._endpoint("GetObject").display_host
        return ObjectBody(
            answer,
            host=host,
            expected_sha256=expected,
            chunk_size=chunk_size or self._chunk_size,
            info=self._info(_check_key(key), answer, "GetObject", host, required=False),
        )

    def head_object(self, key: str) -> ObjectInfo | None:
        """Size, last-modified time and the store's ``Date``; ``None`` when absent."""
        _check_key(key)
        try:
            answer = self._call("HeadObject", "HEAD", self._object_path(key), ())
        except ObjectNotFound:
            return None
        try:
            host = self._endpoint("HeadObject").display_host
            info = self._info(key, answer, "HeadObject", host, required=True)
        finally:
            answer.close()
        assert info is not None
        return info

    def delete_object(self, key: str) -> None:
        """Remove ``key``; an absent key is not an error."""
        try:
            self._call("DeleteObject", "DELETE", self._object_path(_check_key(key)), ()).close()
        except ObjectNotFound:
            return

    def list_pages(self, prefix: str = "", *, max_keys: int | None = None) -> Iterator[ListPage]:
        """The pages of a ListObjectsV2 listing under ``prefix`` (continuation tokens followed)."""
        ep = self._endpoint("ListObjectsV2")
        full_prefix = self._settings.prefix + prefix
        token: str | None = None
        seen: set[str] = set()
        while True:
            query = [("list-type", "2")]
            if full_prefix:
                query.append(("prefix", full_prefix))
            if token is not None:
                query.append(("continuation-token", token))
            if max_keys is not None:
                query.append(("max-keys", str(max_keys)))
            answer = self._call(
                "ListObjectsV2",
                "GET",
                self._bucket_path(),
                tuple(query),
                body_limit=xml_safe.MAX_LIST_BODY,
            )
            date = _http_date(answer.response.getheader("Date"))
            body = answer.body
            answer.close()
            page, token = self._parse_page(body, date, ep.display_host)
            yield page
            if token is None:
                return
            if token in seen:
                raise errors.unreadable_answer("ListObjectsV2", ep.display_host, 200)
            seen.add(token)

    def list_objects_v2(
        self, prefix: str = "", *, max_keys: int | None = None
    ) -> Iterator[ObjectInfo]:
        """Every object under ``prefix``, page by page."""
        for page in self.list_pages(prefix, max_keys=max_keys):
            yield from page.entries

    # -- addressing and settings ------------------------------------------------------------

    def _endpoint(self, operation: str) -> _Endpoint:
        s = self._settings
        explicit_port = False
        if s.endpoint_url:
            parts = urlsplit(s.endpoint_url)
            hostname = parts.hostname
            if parts.scheme not in ("http", "https") or not hostname:
                raise errors.unavailable(
                    "the object store endpoint is not an http(s) URL", operation, "unknown"
                )
            tls = parts.scheme == "https"
            port = parts.port or (443 if tls else 80)
            explicit_port = port != (443 if tls else 80)
            base = parts.path.rstrip("/")
        else:
            if not s.region:
                raise errors.unavailable(
                    "the object store has neither an endpoint nor a region", operation, "unknown"
                )
            hostname, tls, port, base = f"s3.{s.region}.amazonaws.com", True, 443, ""
        shown = _authority(hostname, port if explicit_port else None)
        if not tls and not s.allow_plain_http and not _is_loopback(hostname):
            raise errors.unavailable(
                f"the object store at {shown} is configured with plain http, which is allowed"
                " only for loopback hosts unless plain http is permitted explicitly",
                operation,
                shown,
            )
        virtual = s.addressing == "virtual"
        connect_host = f"{s.bucket}.{hostname}" if virtual else hostname
        header = _authority(connect_host, port if explicit_port else None)
        return _Endpoint(tls, connect_host, port, header, base, not virtual, shown)

    def _credentials(self, operation: str, host: str) -> ObjectStoreCredentials:
        credentials = self._settings.credentials
        missing: list[str] = []
        if credentials is None or not credentials.access_key_id:
            missing.append("access key id")
        if credentials is None or not credentials.secret_access_key:
            missing.append("secret access key")
        if missing or credentials is None:
            raise errors.missing_credentials(operation, host, missing)
        return credentials

    def _object_path(self, key: str) -> str:
        full = self._settings.prefix + key
        if self._settings.addressing == "virtual":
            return f"/{full}"
        return f"/{self._settings.bucket}/{full}"

    def _bucket_path(self) -> str:
        return "/" if self._settings.addressing == "virtual" else f"/{self._settings.bucket}"

    # -- one request with retries -------------------------------------------------------------

    def _ssl_context(self) -> ssl.SSLContext | None:
        if self._context is None:
            bundle = self._settings.ca_bundle
            try:
                self._context = ssl.create_default_context(
                    cafile=str(bundle) if bundle is not None else None
                )
            except (OSError, ssl.SSLError):
                return None
        return self._context

    def _call(
        self,
        operation: str,
        method: str,
        path: str,
        query: tuple[tuple[str, str], ...],
        payload_sha256: str = signing.EMPTY_PAYLOAD_SHA256,
        *,
        source: BinaryIO | None = None,
        size: int = 0,
        extra: dict[str, str] | None = None,
        body_limit: int | None = None,
    ) -> _Answer:
        """Run the request until it answers with a success status, retrying what can pass.
        Raises the mapped error otherwise. The caller closes the answer."""
        ep = self._endpoint(operation)
        credentials = self._credentials(operation, ep.display_host)
        replayable = source is None or source.seekable()
        start = source.tell() if source is not None and replayable else 0
        attempts = 0
        while True:
            attempts += 1
            if source is not None and replayable and attempts > 1:
                source.seek(start)
            outcome = self._attempt(
                ep, credentials, operation, method, path, query, payload_sha256,
                source, size, extra or {}, body_limit,
            )  # fmt: skip
            retryable: bool
            if isinstance(outcome, _Answer):
                if outcome.ok:
                    return outcome
                retryable = errors.is_retryable_response(outcome.status, outcome.s3_code)
            else:
                retryable = outcome.retryable
            if retryable and replayable and attempts <= self._retry.max_retries:
                self._sleep(self._backoff(attempts))
                continue
            break
        if isinstance(outcome, _Failed):
            if outcome.source_short:
                raise ObjectSourceError("the data to upload ended before its declared size")
            raise errors.network_error(operation, ep.display_host, outcome.reason, attempts)
        error = errors.response_error(
            operation, ep.display_host, outcome.status, outcome.s3_code, attempts
        )
        raise error

    def _backoff(self, retry_number: int) -> float:
        ceiling = min(self._retry.cap_s, self._retry.base_s * float(2 ** (retry_number - 1)))
        return float(self._rng()) * ceiling

    def _attempt(
        self,
        ep: _Endpoint,
        credentials: ObjectStoreCredentials,
        operation: str,
        method: str,
        path: str,
        query: tuple[tuple[str, str], ...],
        payload_sha256: str,
        source: BinaryIO | None,
        size: int,
        extra: dict[str, str],
        body_limit: int | None,
    ) -> _Answer | _Failed:
        """One exchange. Never raises on network trouble: it returns a classified failure so
        the error is raised outside any ``except`` block (no chained lower exception)."""
        context = self._ssl_context() if ep.tls else None
        if ep.tls and context is None:
            raise errors.unavailable(
                "the object store's CA bundle cannot be loaded", operation, ep.display_host
            )
        conn: http.client.HTTPConnection | None = None
        try:
            conn = self._connect(ep, context)
            signed = signing.sign_request(
                method=method,
                path=path,
                query=query,
                headers={"host": ep.host_header, **extra},
                payload_sha256=payload_sha256,
                credentials=credentials,
                region=self._settings.region or _SIGNING_REGION_DEFAULT,
                now=self._clock.now(),
            )
            target = signing.canonical_uri(ep.base_path + path)
            if query:
                target += "?" + signing.canonical_query_string(query)
            conn.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
            for name, value in signed.items():
                conn.putheader(name, value)
            conn.putheader("User-Agent", _USER_AGENT)
            conn.putheader("Connection", "close")
            if source is not None:
                conn.putheader("Content-Length", str(size))
            conn.endheaders()
            if source is not None:
                self._send_body(conn, source, size)
            response = conn.getresponse()
            status = response.status
            if 200 <= status < 300:
                body = response.read(body_limit + 1) if body_limit is not None else b""
                if body_limit is not None and len(body) > body_limit:
                    conn.close()
                    return _Failed("response too large", False)
                return _Answer(status, None, response, conn, body)
            raw = b"" if method == "HEAD" else response.read(xml_safe.MAX_ERROR_BODY + 1)
            code = errors.sanitize_code(xml_safe.error_code(raw)) if raw else None
            conn.close()
            return _Answer(status, code, response, conn, b"")
        except _SourceTooShort:
            if conn is not None:
                conn.close()
            return _Failed("the data to upload ended early", False, source_short=True)
        except (OSError, http.client.HTTPException) as exc:
            if conn is not None:
                conn.close()
            return _classify(exc)

    def _connect(self, ep: _Endpoint, context: ssl.SSLContext | None) -> http.client.HTTPConnection:
        conn: http.client.HTTPConnection
        if ep.tls:
            conn = http.client.HTTPSConnection(
                ep.connect_host, ep.port, timeout=self._timeouts.connect_s, context=context
            )
        else:
            conn = http.client.HTTPConnection(
                ep.connect_host, ep.port, timeout=self._timeouts.connect_s
            )
        conn.connect()
        if conn.sock is not None:
            conn.sock.settimeout(self._timeouts.read_s)
        return conn

    def _send_body(self, conn: http.client.HTTPConnection, source: BinaryIO, size: int) -> None:
        remaining = size
        while remaining:
            chunk = source.read(min(self._chunk_size, remaining))
            if not chunk:
                raise _SourceTooShort
            conn.send(chunk)
            remaining -= len(chunk)

    # -- answers ----------------------------------------------------------------------------

    def _info(
        self, key: str, answer: _Answer, operation: str, host: str, *, required: bool
    ) -> ObjectInfo | None:
        response = answer.response
        length = response.getheader("Content-Length")
        modified = _http_date(response.getheader("Last-Modified"))
        if length is None or not length.isdigit() or modified is None:
            if required:
                answer.close()
                raise errors.unreadable_answer(operation, host, answer.status)
            return None
        return ObjectInfo(key, int(length), modified, _http_date(response.getheader("Date")))

    def _parse_page(
        self, body: bytes, date: datetime | None, host: str
    ) -> tuple[ListPage, str | None]:
        root = xml_safe.parse_xml(body, limit=xml_safe.MAX_LIST_BODY)
        if root is None or xml_safe.local_name(root.tag) != "ListBucketResult":
            raise errors.unreadable_answer("ListObjectsV2", host, 200)
        strip = len(self._settings.prefix)
        entries: list[ObjectInfo] = []
        for element in root:
            if xml_safe.local_name(element.tag) != "Contents":
                continue
            key = xml_safe.child_text(element, "Key")
            size = xml_safe.child_text(element, "Size")
            stamp = xml_safe.child_text(element, "LastModified")
            modified = _iso(stamp)
            if (
                key is None
                or not key.startswith(self._settings.prefix)
                or size is None
                or not size.isdigit()
                or modified is None
            ):
                raise errors.unreadable_answer("ListObjectsV2", host, 200)
            entries.append(ObjectInfo(key[strip:], int(size), modified, date))
        token: str | None = None
        if (xml_safe.child_text(root, "IsTruncated") or "").lower() == "true":
            token = xml_safe.child_text(root, "NextContinuationToken")
            if not token:
                raise errors.unreadable_answer("ListObjectsV2", host, 200)
        return ListPage(tuple(entries), date), token


def _authority(host: str, port: int | None) -> str:
    bracketed = f"[{host}]" if ":" in host else host
    return bracketed if port is None else f"{bracketed}:{port}"


def _iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None
