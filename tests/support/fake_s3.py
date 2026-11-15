"""An in-process S3 server for tests (object storage stage 1, O10).

``ThreadingHTTPServer`` on 127.0.0.1 with a random port, path-style addressing, and what the
client needs: PUT, GET, HEAD, DELETE and ListObjectsV2 with paging. It behaves like a real
store where it matters for the client:

- path-style addressing, and virtual-hosted addressing when the ``Host`` header starts with
  ``<bucket>.`` (tests make that name resolve);
- every request is verified against SigV4 with a fixed key pair (the canonical request is
  rebuilt from the request as received: path, query, and the headers named in
  ``SignedHeaders``), including the credential scope (region, service) and the request time
  against the injected clock (``RequestTimeTooSkewed``);
- ``x-amz-content-sha256`` is checked against the body (``XAmzContentSHA256Mismatch``);
- error bodies carry the access key id, the string to sign and the canonical request, as the
  answers of S3 do, so tests can prove the client never echoes a body;
- ``LastModified`` and ``Date`` come from the injected clock.

Faults are injected with ``fail``, ``delay``, ``stall_get_body``, ``corrupt_get``,
``listing_lag`` and ``without_list_bucket`` (a missing object answers 403, as AWS does for a
key without ``ListBucket``); ``requests`` records every request. ``tls`` serves HTTPS with the given
certificate and key. Ranges and chunked uploads are not
supported. The signing primitives come from ``graph_cli.storage.s3.signing``, which has its
own test vectors.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import ssl
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit
from xml.sax.saxutils import escape

from graph_cli.kernel.clock import Clock, SystemClock
from graph_cli.storage.s3 import signing
from graph_cli.storage.s3.settings import ObjectStoreCredentials, ObjectStoreSettings

ACCESS_KEY_ID = "AKIAFAKES3EXAMPLE01"
SECRET_ACCESS_KEY = "fakeS3secret/KEY+Material0123456789abcdef"
SESSION_TOKEN = "fakeS3session/token+Material0123456789abcdef"
BUCKET = "fake-bucket"
REGION = "fake-region-1"

_S3_NS = "http://s3.amazonaws.com/doc/2006-03-01/"
_AUTH = re.compile(
    r"^AWS4-HMAC-SHA256 Credential=(?P<key>[^/]+)/(?P<date>\d{8})/(?P<region>[^/]+)/"
    r"(?P<service>[^/]+)/aws4_request, ?SignedHeaders=(?P<signed>[^,]+), ?"
    r"Signature=(?P<sig>[0-9a-f]+)$"
)
_MAX_SKEW = timedelta(minutes=15)


@dataclass
class StoredObject:
    data: bytes
    last_modified: datetime

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass
class RecordedRequest:
    method: str
    key: str | None
    query: dict[str, str]
    status: int
    body_bytes: int = 0
    authorization: str = ""


@dataclass
class _Fault:
    status: int
    code: str
    times: int
    method: str | None
    key: str | None
    message: str
    body: bytes | None


@dataclass
class _Delay:
    seconds: float
    times: int
    method: str | None


@dataclass
class _Request:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes
    key: str | None = None
    canonical: str = ""
    to_sign: str = ""
    key_id: str = ""
    authenticated: bool = False


class FakeS3:
    """The store. Use as a context manager; ``settings()`` builds client settings for it."""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        bucket: str = BUCKET,
        region: str = REGION,
        page_size: int = 1000,
        session_token: str | None = None,
        tls: tuple[Path, Path] | None = None,
    ) -> None:
        self.clock: Clock = clock or SystemClock()
        self.bucket = bucket
        self.region = region
        self.page_size = page_size
        self.session_token = session_token
        self.tls = tls
        self.objects: dict[str, StoredObject] = {}
        self.requests: list[RecordedRequest] = []
        self.listing_lag = timedelta(0)
        # a key without ListBucket: AWS answers 403 instead of 404 for a missing object
        self.without_list_bucket = False
        self._lock = threading.Lock()
        self._faults: list[_Fault] = []
        self._delays: list[_Delay] = []
        self._stall: tuple[int, float] | None = None
        self._truncate: int | None = None
        self._corrupt: dict[str, bytes] = {}
        self._stopped = threading.Event()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ------------------------------------------------------------------------

    def start(self) -> FakeS3:
        fake = self

        class Handler(_Handler):
            store = fake

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        if self.tls is not None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(self.tls[0]), str(self.tls[1]))
            server.socket = context.wrap_socket(server.socket, server_side=True)
        self._server = server
        self._thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stopped.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> FakeS3:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    @property
    def port(self) -> int:
        assert self._server is not None
        return int(self._server.server_address[1])

    @property
    def endpoint_url(self) -> str:
        return f"{'https' if self.tls else 'http'}://127.0.0.1:{self.port}"

    def settings(self, **overrides: object) -> ObjectStoreSettings:
        values: dict[str, object] = {
            "bucket": self.bucket,
            "region": self.region,
            "endpoint_url": self.endpoint_url,
            "credentials": ObjectStoreCredentials(
                ACCESS_KEY_ID, SECRET_ACCESS_KEY, self.session_token
            ),
        }
        values.update(overrides)
        return ObjectStoreSettings(**values)  # type: ignore[arg-type]

    # -- direct access to the content ------------------------------------------------------

    def put(self, key: str, data: bytes, *, last_modified: datetime | None = None) -> None:
        with self._lock:
            self.objects[key] = StoredObject(data, last_modified or self.clock.now())

    def get(self, key: str) -> bytes | None:
        with self._lock:
            stored = self.objects.get(key)
        return None if stored is None else stored.data

    def keys(self) -> list[str]:
        with self._lock:
            return sorted(self.objects)

    def calls(self, method: str | None = None) -> list[RecordedRequest]:
        with self._lock:
            return [r for r in self.requests if method is None or r.method == method]

    # -- fault injection --------------------------------------------------------------------

    def fail(
        self,
        status: int,
        code: str,
        *,
        times: int = 1,
        method: str | None = None,
        key: str | None = None,
        message: str = "injected failure",
        body: bytes | None = None,
    ) -> None:
        """Answer the next ``times`` matching requests with this error (after authentication).
        ``body`` replaces the generated error document (hostile or malformed bodies)."""
        with self._lock:
            self._faults.append(_Fault(status, code, times, method, key, message, body))

    def delay(self, seconds: float, *, times: int = 1, method: str | None = None) -> None:
        """Wait before answering the next ``times`` matching requests (a read timeout)."""
        with self._lock:
            self._delays.append(_Delay(seconds, times, method))

    def stall_get_body(self, after_bytes: int, seconds: float) -> None:
        """Send ``after_bytes`` of the next GET body, wait, then send the rest."""
        with self._lock:
            self._stall = (after_bytes, seconds)

    def truncate_get_body(self, after_bytes: int) -> None:
        """Declare the full length of the next GET body, send ``after_bytes`` and hang up."""
        with self._lock:
            self._truncate = after_bytes

    def corrupt_get(self, key: str, body: bytes) -> None:
        """Serve ``body`` for ``key`` instead of what is stored (the stored hash is unchanged)."""
        with self._lock:
            self._corrupt[key] = body

    # -- request handling (called from handler threads) ---------------------------------------

    def _take_delay(self, method: str) -> float:
        with self._lock:
            for rule in self._delays:
                if rule.times > 0 and rule.method in (None, method):
                    rule.times -= 1
                    return rule.seconds
        return 0.0

    def _take_fault(self, method: str, key: str | None) -> _Fault | None:
        with self._lock:
            for rule in self._faults:
                if rule.times > 0 and rule.method in (None, method) and rule.key in (None, key):
                    rule.times -= 1
                    return rule
        return None

    def authenticate(self, request: _Request) -> tuple[int, str, str] | None:
        """``None`` when the signature is good, else ``(status, code, message)``."""
        header = request.headers.get("authorization", "")
        match = _AUTH.match(header)
        if match is None:
            return 403, "AccessDenied", "Access Denied"
        request.key_id = match["key"]
        signed_names = match["signed"].split(";")
        if match["key"] != ACCESS_KEY_ID:
            return (
                403,
                "InvalidAccessKeyId",
                f"The AWS Access Key Id you provided ({match['key']}) does not exist.",
            )
        if match["service"] != "s3" or match["region"] != self.region:
            return (
                400,
                "AuthorizationHeaderMalformed",
                f"the region '{match['region']}' is wrong; expecting '{self.region}'",
            )
        stamp = request.headers.get("x-amz-date", "")
        try:
            sent = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        except ValueError:
            return 403, "AccessDenied", "invalid x-amz-date"
        if stamp[:8] != match["date"] or abs(self.clock.now() - sent) > _MAX_SKEW:
            return (
                403,
                "RequestTimeTooSkewed",
                "The difference between the request time and the current time is too large.",
            )
        if "host" not in signed_names or "x-amz-date" not in signed_names:
            return 403, "AccessDenied", "host and x-amz-date must be signed"
        if "x-amz-content-sha256" not in signed_names:
            return 403, "AccessDenied", "x-amz-content-sha256 must be signed"
        token = request.headers.get("x-amz-security-token")
        if self.session_token is not None and token != self.session_token:
            return 400, "InvalidToken", "The provided token is malformed or otherwise invalid."
        if token is not None and "x-amz-security-token" not in signed_names:
            return 403, "AccessDenied", "x-amz-security-token must be signed"
        try:
            headers = {name: request.headers[name] for name in signed_names}
        except KeyError:
            return 403, "AccessDenied", "a signed header is missing"
        payload = request.headers.get("x-amz-content-sha256", "")
        request.canonical = signing.canonical_request(
            request.method,
            signing.canonical_uri(unquote(request.path)),
            signing.canonical_query_string(request.query.items()),
            headers,
            payload,
        )
        scope = signing.credential_scope(match["date"], match["region"])
        request.to_sign = signing.string_to_sign(stamp, scope, request.canonical)
        key = signing.derive_signing_key(SECRET_ACCESS_KEY, match["date"], match["region"])
        if not hmac.compare_digest(signing.signature_of(key, request.to_sign), match["sig"]):
            return (
                403,
                "SignatureDoesNotMatch",
                "The request signature we calculated does not match the signature you provided.",
            )
        request.authenticated = True
        return None


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


class _Handler(BaseHTTPRequestHandler):
    store: FakeS3
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def date_time_string(self, timestamp: float | None = None) -> str:
        return format_datetime(self.store.clock.now(), usegmt=True)

    def version_string(self) -> str:
        return "FakeS3"

    # one entry point for every verb

    def do_PUT(self) -> None:
        self._serve()

    do_GET = do_HEAD = do_DELETE = do_POST = do_PUT

    def _serve(self) -> None:
        store = self.store
        parts = urlsplit(self.path)
        body = b""
        length = self.headers.get("Content-Length")
        if length is not None and length.isdigit():
            body = self.rfile.read(int(length))
        request = _Request(
            self.command,
            parts.path,
            dict(parse_qsl(parts.query, keep_blank_values=True)),
            {name.lower(): value for name, value in self.headers.items()},
            body,
        )
        host = request.headers.get("host", "")
        virtual = host.startswith(f"{store.bucket}.")
        if virtual:
            bucket, key = store.bucket, unquote(parts.path).lstrip("/")
        else:
            segments = unquote(parts.path).lstrip("/").split("/", 1)
            bucket, key = segments[0], segments[1] if len(segments) == 2 else ""
        request.key = key or None
        self._respond(request, bucket)

    def _respond(self, request: _Request, bucket: str) -> int:
        store = self.store
        pause = store._take_delay(request.method)
        if pause and store._stopped.wait(pause):
            return self._error(request, 503, "ServiceUnavailable", "stopping")
        failure = store.authenticate(request)
        if failure is not None:
            return self._error(request, *failure)
        if bucket != store.bucket:
            return self._error(request, 404, "NoSuchBucket", "The specified bucket does not exist")
        payload = request.headers.get("x-amz-content-sha256", "")
        if payload.startswith("STREAMING-"):
            return self._error(request, 501, "NotImplemented", "chunked signing is not supported")
        expected = hashlib.sha256(request.body).hexdigest()
        if payload != "UNSIGNED-PAYLOAD" and payload != expected:
            return self._error(
                request,
                400,
                "XAmzContentSHA256Mismatch",
                "The provided 'x-amz-content-sha256' header does not match what was computed.",
            )
        fault = store._take_fault(request.method, request.key)
        if fault is not None:
            if fault.body is not None:
                return self._send(request, fault.status, fault.body, {})
            return self._error(request, fault.status, fault.code, fault.message)
        if request.method == "PUT" and request.key:
            store.put(request.key, request.body)
            return self._send(
                request, 200, b"", {"ETag": f'"{hashlib.md5(request.body).hexdigest()}"'}
            )
        if request.method == "DELETE" and request.key:
            with store._lock:
                store.objects.pop(request.key, None)
            return self._send(request, 204, b"", {})
        if request.method in ("GET", "HEAD") and request.key:
            return self._object(request)
        if (
            request.method == "GET"
            and request.key is None
            and request.query.get("list-type") == "2"
        ):
            return self._list(request)
        return self._error(request, 400, "InvalidRequest", "unsupported request")

    def _object(self, request: _Request) -> int:
        store = self.store
        assert request.key is not None
        with store._lock:
            stored = store.objects.get(request.key)
            served = store._corrupt.get(request.key)
            stall = cut = None
            if request.method == "GET":
                stall, store._stall = store._stall, None
                cut, store._truncate = store._truncate, None
        if stored is None:
            if store.without_list_bucket:
                return self._error(request, 403, "AccessDenied", "Access Denied")
            return self._error(request, 404, "NoSuchKey", "The specified key does not exist.")
        data = served if served is not None else stored.data
        headers = {
            "Last-Modified": format_datetime(stored.last_modified, usegmt=True),
            "ETag": f'"{hashlib.md5(stored.data).hexdigest()}"',
            "Content-Type": "application/octet-stream",
        }
        return self._send(request, 200, data, headers, stall=stall, truncate=cut)

    def _list(self, request: _Request) -> int:
        store = self.store
        prefix = request.query.get("prefix", "")
        limit = min(int(request.query.get("max-keys", "1000")), store.page_size)
        after = ""
        token = request.query.get("continuation-token")
        if token:
            after = base64.urlsafe_b64decode(token.encode()).decode()
        now = store.clock.now()
        with store._lock:
            visible = sorted(
                key
                for key, stored in store.objects.items()
                if key.startswith(prefix)
                and key > after
                and now - stored.last_modified >= store.listing_lag
            )
            page = visible[:limit]
            truncated = len(visible) > limit
            contents = "".join(
                f"<Contents><Key>{escape(key)}</Key>"
                f"<LastModified>{_iso(store.objects[key].last_modified)}</LastModified>"
                f'<ETag>"x"</ETag><Size>{len(store.objects[key].data)}</Size>'
                "<StorageClass>STANDARD</StorageClass></Contents>"
                for key in page
            )
        next_token = ""
        if truncated:
            encoded = base64.urlsafe_b64encode(page[-1].encode()).decode()
            next_token = f"<NextContinuationToken>{encoded}</NextContinuationToken>"
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<ListBucketResult xmlns="{_S3_NS}"><Name>{escape(store.bucket)}</Name>'
            f"<Prefix>{escape(prefix)}</Prefix><KeyCount>{len(page)}</KeyCount>"
            f"<MaxKeys>{limit}</MaxKeys><IsTruncated>{str(truncated).lower()}</IsTruncated>"
            f"{next_token}{contents}</ListBucketResult>"
        )
        return self._send(request, 200, xml.encode(), {"Content-Type": "application/xml"})

    def _error(self, request: _Request, status: int, code: str, message: str) -> int:
        """An S3 error document. Like the real thing it names the access key id, the string to
        sign and the canonical request wherever the request got that far."""
        key_id = request.headers.get("authorization", "")
        found = _AUTH.match(key_id)
        extras = ""
        if found is not None:
            extras += f"<AWSAccessKeyId>{escape(found['key'])}</AWSAccessKeyId>"
            extras += f"<SignatureProvided>{found['sig']}</SignatureProvided>"
        if request.canonical:
            extras += f"<StringToSign>{escape(request.to_sign)}</StringToSign>"
            extras += f"<CanonicalRequest>{escape(request.canonical)}</CanonicalRequest>"
        detail = escape(f"{message} (key {found['key'] if found else '-'})")
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f"<Error><Code>{escape(code)}</Code><Message>{detail}</Message>{extras}"
            "<RequestId>FAKE0001</RequestId><HostId>fake-host</HostId></Error>"
        )
        return self._send(request, status, xml.encode(), {"Content-Type": "application/xml"})

    def _send(
        self,
        request: _Request,
        status: int,
        body: bytes,
        headers: dict[str, str],
        *,
        stall: tuple[int, float] | None = None,
        truncate: int | None = None,
    ) -> int:
        store = self.store
        with store._lock:  # recorded before the answer goes out: a test reading right after
            store.requests.append(  # the client returned must already see it
                RecordedRequest(
                    request.method,
                    request.key,
                    request.query,
                    status,
                    len(request.body),
                    request.headers.get("authorization", ""),
                )
            )
        try:
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            if request.method == "HEAD" or not body:
                return status
            if truncate is not None:
                self.wfile.write(body[:truncate])
                return status
            if stall is not None:
                head, pause = stall
                self.wfile.write(body[:head])
                self.wfile.flush()
                if self.store._stopped.wait(pause):
                    return status
                body = body[head:]
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        return status


@contextmanager
def fake_s3(**kwargs: object) -> Iterator[FakeS3]:
    store = FakeS3(**kwargs)  # type: ignore[arg-type]
    with store:
        yield store
