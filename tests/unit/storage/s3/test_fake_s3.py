"""The fake store checks what a real one checks: tests of the test infrastructure.

Raw requests (signed with the signing module, then altered) prove that the fake rejects a
signature that does not cover what was sent, a body that differs from its declared hash,
a stale request time and a wrong credential scope. Without these the client tests would pass
against a fake that accepts anything.
"""

import hashlib
import http.client
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

import pytest
from support.clock import FixedClock
from support.fake_s3 import (
    ACCESS_KEY_ID,
    BUCKET,
    REGION,
    SECRET_ACCESS_KEY,
    FakeS3,
)
from support.s3_helpers import T0

from graph_cli.storage.s3 import signing
from graph_cli.storage.s3.settings import ObjectStoreCredentials


@dataclass
class Reply:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def code(self) -> str | None:
        marker = b"<Code>"
        if marker not in self.body:
            return None
        return self.body.split(marker)[1].split(b"</Code>")[0].decode()


def raw(
    fake: FakeS3,
    method: str,
    path: str,
    *,
    query: tuple[tuple[str, str], ...] = (),
    body: bytes = b"",
    declared_hash: str | None = None,
    sign_path: str | None = None,
    sign_query: tuple[tuple[str, str], ...] | None = None,
    wire_body: bytes | None = None,
    wire_headers: dict[str, str] | None = None,
    now_offset: timedelta = timedelta(0),
    region: str = REGION,
    secret: str = SECRET_ACCESS_KEY,
    key_id: str = ACCESS_KEY_ID,
) -> Reply:
    """Sign ``method path`` for ``body`` and send it, optionally with something changed after
    signing (``wire_*``) or something else signed (``sign_*``)."""
    host = f"127.0.0.1:{fake.port}"
    payload = declared_hash or hashlib.sha256(body).hexdigest()
    headers = signing.sign_request(
        method=method,
        path=sign_path or path,
        query=sign_query if sign_query is not None else query,
        headers={"host": host, "content-type": "application/octet-stream"},
        payload_sha256=payload,
        credentials=ObjectStoreCredentials(key_id, secret),
        region=region,
        now=fake.clock.now() + now_offset,
    )
    headers.update(wire_headers or {})
    sent = body if wire_body is None else wire_body
    target = signing.canonical_uri(path)
    if query:
        target += "?" + signing.canonical_query_string(query)
    conn = http.client.HTTPConnection("127.0.0.1", fake.port, timeout=5)
    try:
        conn.request(method, target, body=sent if method == "PUT" else None, headers=headers)
        response = conn.getresponse()
        return Reply(response.status, dict(response.getheaders()), response.read())
    finally:
        conn.close()


def test_a_valid_request_is_accepted_for_every_verb(fake: FakeS3) -> None:
    path = f"/{BUCKET}/dir/a b"
    assert raw(fake, "PUT", path, body=b"data").status == 200
    got = raw(fake, "GET", path)
    assert (got.status, got.body) == (200, b"data")
    assert raw(fake, "HEAD", path).headers["Content-Length"] == "4"
    assert raw(fake, "DELETE", path).status == 204
    assert raw(fake, "GET", path).code == "NoSuchKey"


def test_the_signature_must_cover_the_path_that_was_sent(fake: FakeS3) -> None:
    reply = raw(fake, "GET", f"/{BUCKET}/a", sign_path=f"/{BUCKET}/b")
    assert (reply.status, reply.code) == (403, "SignatureDoesNotMatch")


def test_the_signature_must_cover_the_query_that_was_sent(fake: FakeS3) -> None:
    reply = raw(
        fake,
        "GET",
        f"/{BUCKET}",
        query=(("list-type", "2"), ("prefix", "a")),
        sign_query=(("list-type", "2"), ("prefix", "b")),
    )
    assert (reply.status, reply.code) == (403, "SignatureDoesNotMatch")


def test_a_signed_header_changed_after_signing_is_detected(fake: FakeS3) -> None:
    reply = raw(fake, "GET", f"/{BUCKET}/a", wire_headers={"content-type": "text/plain"})
    assert (reply.status, reply.code) == (403, "SignatureDoesNotMatch")


def test_a_wrong_secret_or_key_id_is_detected(fake: FakeS3) -> None:
    assert raw(fake, "GET", f"/{BUCKET}/a", secret="x" * 40).code == "SignatureDoesNotMatch"
    assert raw(fake, "GET", f"/{BUCKET}/a", key_id="AKIAOTHER").code == "InvalidAccessKeyId"


def test_the_body_must_match_the_declared_payload_hash(fake: FakeS3) -> None:
    reply = raw(fake, "PUT", f"/{BUCKET}/a", body=b"declared", wire_body=b"tampered")
    assert (reply.status, reply.code) == (400, "XAmzContentSHA256Mismatch")
    assert fake.get("a") is None


def test_a_get_must_declare_the_hash_of_an_empty_body(fake: FakeS3) -> None:
    reply = raw(fake, "GET", f"/{BUCKET}/a", declared_hash="0" * 64)
    assert (reply.status, reply.code) == (400, "XAmzContentSHA256Mismatch")


def test_an_unsigned_request_is_denied(fake: FakeS3) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", fake.port, timeout=5)
    conn.request("GET", f"/{BUCKET}/a")
    response = conn.getresponse()
    assert response.status == 403
    assert b"AccessDenied" in response.read()


def test_the_request_time_is_checked_against_the_stores_clock(fake: FakeS3) -> None:
    assert raw(fake, "GET", f"/{BUCKET}/a", now_offset=timedelta(minutes=10)).code == "NoSuchKey"
    late = raw(fake, "GET", f"/{BUCKET}/a", now_offset=timedelta(minutes=20))
    assert (late.status, late.code) == (403, "RequestTimeTooSkewed")
    early = raw(fake, "GET", f"/{BUCKET}/a", now_offset=-timedelta(minutes=20))
    assert early.code == "RequestTimeTooSkewed"


def test_the_credential_scope_must_name_the_stores_region(fake: FakeS3) -> None:
    reply = raw(fake, "GET", f"/{BUCKET}/a", region="us-east-1")
    assert (reply.status, reply.code) == (400, "AuthorizationHeaderMalformed")


def test_an_unknown_bucket(fake: FakeS3) -> None:
    assert raw(fake, "GET", "/other-bucket/a").code == "NoSuchBucket"


def test_error_bodies_carry_what_a_real_stores_do(fake: FakeS3) -> None:
    """The premise of the redaction tests: a failed signature draws a body that names the
    access key id, the string to sign and the canonical request."""
    reply = raw(fake, "GET", f"/{BUCKET}/a", secret="x" * 40)
    text = reply.body.decode()
    assert f"<AWSAccessKeyId>{ACCESS_KEY_ID}</AWSAccessKeyId>" in text
    assert "<StringToSign>AWS4-HMAC-SHA256" in text
    assert "<CanonicalRequest>GET" in text
    assert "<SignatureProvided>" in text


def test_dates_come_from_the_injected_clock(clock: FixedClock, fake: FakeS3) -> None:
    fake.put("a", b"x", last_modified=T0 - timedelta(days=1))
    reply = raw(fake, "HEAD", f"/{BUCKET}/a")
    assert reply.headers["Date"] == "Sun, 01 Mar 2026 12:00:00 GMT"
    assert reply.headers["Last-Modified"] == "Sat, 28 Feb 2026 12:00:00 GMT"
    clock.advance(timedelta(hours=3))
    assert raw(fake, "HEAD", f"/{BUCKET}/a").headers["Date"] == "Sun, 01 Mar 2026 15:00:00 GMT"


def fresh_requests(fake: FakeS3, count: int) -> list[int]:
    return [raw(fake, "HEAD", f"/{BUCKET}/missing").status for _ in range(count)]


def test_injected_failures_apply_in_order_and_expire(fake: FakeS3) -> None:
    fake.fail(500, "InternalError", times=2)
    assert fresh_requests(fake, 4) == [500, 500, 404, 404]


def test_injected_failures_can_target_a_method_and_a_key(fake: FakeS3) -> None:
    fake.fail(503, "SlowDown", times=5, method="PUT", key="only/this")
    assert raw(fake, "PUT", f"/{BUCKET}/other", body=b"x").status == 200
    assert raw(fake, "GET", f"/{BUCKET}/only/this").status == 404
    assert raw(fake, "PUT", f"/{BUCKET}/only/this", body=b"x").status == 503


def test_injected_failures_still_need_valid_authentication(fake: FakeS3) -> None:
    fake.fail(500, "InternalError", times=1)
    assert raw(fake, "GET", f"/{BUCKET}/a", secret="x" * 40).code == "SignatureDoesNotMatch"
    assert raw(fake, "GET", f"/{BUCKET}/a").status == 500


def test_the_listing_is_sorted_filtered_and_paged(fake: FakeS3) -> None:
    fake.page_size = 2
    for key in ("b/2", "a/1", "b/1", "b/3"):
        fake.put(key, b"")
    first = raw(fake, "GET", f"/{BUCKET}", query=(("list-type", "2"), ("prefix", "b/")))
    text = first.body.decode()
    assert text.index("b/1") < text.index("b/2") and "b/3" not in text
    assert "<IsTruncated>true</IsTruncated>" in text and "<NextContinuationToken>" in text


@pytest.fixture(autouse=True)
def _stop_check() -> Callable[[], None]:
    return lambda: None
