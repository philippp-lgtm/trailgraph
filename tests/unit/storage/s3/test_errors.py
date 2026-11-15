"""Error mapping by S3 code (O9) and redaction (O2): nothing secret in any error."""

import hashlib
import io
import json
import logging
import re
import socket
import traceback
from collections.abc import Callable
from datetime import timedelta

import pytest
from support.clock import FixedClock
from support.fake_s3 import ACCESS_KEY_ID, BUCKET, REGION, SECRET_ACCESS_KEY, SESSION_TOKEN, FakeS3
from support.s3_helpers import ClientFactory, Sleeps

from graph_cli.kernel.error_codes import ErrorCategory
from graph_cli.kernel.errors import GraphCliError
from graph_cli.storage.s3.client import ObjectStoreClient, RetryPolicy, Timeouts
from graph_cli.storage.s3.errors import ObjectNotFound, sanitize_code
from graph_cli.storage.s3.settings import ObjectStoreCredentials, ObjectStoreSettings

DATA = b"payload"
HASH = "239f59ed55e737c77147cf55ad0c1b030b6d7ee748a7426952f9b852d5a935e5"

Call = Callable[[ObjectStoreClient], object]


def _put(client: ObjectStoreClient) -> object:
    return client.put_object("k", io.BytesIO(DATA), len(DATA), HASH)


CALLS: dict[str, tuple[str, Call]] = {
    "put": ("PUT", _put),
    "get": ("GET", lambda c: c.get_object("k")),
    "head": ("HEAD", lambda c: c.head_object("k")),
    "delete": ("DELETE", lambda c: c.delete_object("k")),
    "list": ("GET", lambda c: list(c.list_objects_v2(""))),
}
OPERATIONS = {
    "put": "PutObject",
    "get": "GetObject",
    "head": "HeadObject",
    "delete": "DeleteObject",
    "list": "ListObjectsV2",
}


def fail_and_call(
    client: ObjectStoreClient, fake: FakeS3, call: str, status: int, code: str
) -> GraphCliError:
    method, run = CALLS[call]
    fake.fail(status, code, times=10, method=method)
    with pytest.raises(GraphCliError) as caught:
        run(client)
    return caught.value


# -- every mapped code --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("call", "status", "code"),
    [
        ("put", 403, "AccessDenied"),
        ("put", 403, "QuotaExceeded"),
        ("put", 403, "XMinioAdminBucketQuotaExceeded"),
        ("put", 400, "EntityTooLarge"),
        ("put", 507, "InsufficientStorage"),
        ("delete", 403, "AccessDenied"),
    ],
)
def test_refused_writes_are_files_store_refused(
    client: ObjectStoreClient, fake: FakeS3, call: str, status: int, code: str
) -> None:
    error = fail_and_call(client, fake, call, status, code)
    assert error.code == "files.store_refused"
    assert error.category is ErrorCategory.REFUSED
    assert not error.retryable
    assert "quota" in error.message and "permission" in error.message
    assert error.details["status"] == status
    assert error.details["s3_code"] == code
    assert error.details["operation"] == OPERATIONS[call]
    assert error.details["endpoint_host"] == f"127.0.0.1:{fake.port}"
    assert len(fake.calls(CALLS[call][0])) == 1  # not retried


@pytest.mark.parametrize("call", ["put", "get", "list", "delete"])
@pytest.mark.parametrize("code", ["InvalidAccessKeyId", "SignatureDoesNotMatch", "ExpiredToken"])
def test_refused_credentials_are_backend_unavailable(
    client: ObjectStoreClient, fake: FakeS3, call: str, code: str
) -> None:
    error = fail_and_call(client, fake, call, 403, code)
    assert error.code == "backend.unavailable"
    assert "the object store refused the credentials" in error.message
    assert error.details["s3_code"] == code


@pytest.mark.parametrize("call", ["get", "list"])
def test_access_denied_on_a_read_points_at_the_store_check(
    client: ObjectStoreClient, fake: FakeS3, call: str
) -> None:
    error = fail_and_call(client, fake, call, 403, "AccessDenied")
    assert error.code == "backend.unavailable"
    assert "graph-admin files store check" in error.message
    assert error.details["s3_code"] == "AccessDenied"


def test_a_denied_head_has_no_body_and_is_still_backend_unavailable(
    client: ObjectStoreClient, fake: FakeS3
) -> None:
    error = fail_and_call(client, fake, "head", 403, "AccessDenied")
    assert error.code == "backend.unavailable"
    assert "graph-admin files store check" in error.message
    assert error.details["status"] == 403
    assert "s3_code" not in error.details


def test_clock_skew_is_named_when_the_store_rejects_the_time(
    make_client: ClientFactory, fake: FakeS3, clock: FixedClock
) -> None:
    skewed = FixedClock(clock.now() + timedelta(hours=1))
    with pytest.raises(GraphCliError) as caught:
        make_client(clock=skewed).get_object("k")
    assert caught.value.code == "backend.unavailable"
    assert "clock skew" in caught.value.message
    assert caught.value.details["s3_code"] == "RequestTimeTooSkewed"


@pytest.mark.parametrize("call", ["put", "get", "list", "delete"])
def test_no_such_bucket_is_backend_unavailable(
    client: ObjectStoreClient, fake: FakeS3, call: str
) -> None:
    error = fail_and_call(client, fake, call, 404, "NoSuchBucket")
    assert error.code == "backend.unavailable"
    assert "bucket does not exist" in error.message
    assert error.details["s3_code"] == "NoSuchBucket"


def test_a_real_wrong_bucket_name_is_no_such_bucket(
    make_client: ClientFactory, fake: FakeS3
) -> None:
    with pytest.raises(GraphCliError) as caught:
        make_client(fake.settings(bucket="not-this-one")).get_object("k")
    assert "bucket does not exist" in caught.value.message


def test_real_credential_failures_by_the_fakes_verification(
    make_client: ClientFactory, fake: FakeS3
) -> None:
    wrong_secret = fake.settings(credentials=ObjectStoreCredentials(ACCESS_KEY_ID, "x" * 40))
    wrong_key = fake.settings(credentials=ObjectStoreCredentials("AKIAUNKNOWN", SECRET_ACCESS_KEY))
    for settings, code in (
        (wrong_secret, "SignatureDoesNotMatch"),
        (wrong_key, "InvalidAccessKeyId"),
    ):
        with pytest.raises(GraphCliError) as caught:
            make_client(settings).get_object("k")
        assert caught.value.code == "backend.unavailable"
        assert "refused the credentials" in caught.value.message
        assert caught.value.details["s3_code"] == code


def test_a_wrong_region_is_named(make_client: ClientFactory, fake: FakeS3) -> None:
    with pytest.raises(GraphCliError) as caught:
        make_client(fake.settings(region="other-region")).get_object("k")
    assert caught.value.code == "backend.unavailable"
    assert "region" in caught.value.message
    assert caught.value.details["s3_code"] == "AuthorizationHeaderMalformed"


def test_redirects_are_not_followed(client: ObjectStoreClient, fake: FakeS3) -> None:
    error = fail_and_call(client, fake, "get", 301, "PermanentRedirect")
    assert error.code == "backend.unavailable"
    assert "another endpoint or region" in error.message
    assert len(fake.calls("GET")) == 1


def test_server_errors_after_retries_are_backend_unavailable(
    client: ObjectStoreClient, fake: FakeS3
) -> None:
    error = fail_and_call(client, fake, "list", 500, "InternalError")
    assert error.code == "backend.unavailable"
    assert error.retryable
    assert error.details["attempts"] == 4
    assert "status 500 (InternalError)" in error.message


def test_a_missing_object_is_typed_not_decided(client: ObjectStoreClient, fake: FakeS3) -> None:
    fake.fail(404, "NoSuchKey", times=1, method="GET")
    with pytest.raises(ObjectNotFound) as caught:
        client.get_object("k")
    assert caught.value.operation == "GetObject"
    assert not isinstance(caught.value, GraphCliError)
    assert client.head_object("k") is None


def test_an_unknown_code_keeps_status_and_code_in_the_details(
    client: ObjectStoreClient, fake: FakeS3
) -> None:
    error = fail_and_call(client, fake, "put", 400, "SomethingNew")
    assert error.code == "backend.unavailable"
    assert dict(error.details) == {
        "operation": "PutObject",
        "endpoint_host": f"127.0.0.1:{fake.port}",
        "status": 400,
        "s3_code": "SomethingNew",
    }


# -- the body of an error is untrusted ------------------------------------------------------------

HOSTILE = {
    "entity bomb": (
        b'<?xml version="1.0"?><!DOCTYPE Error [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;">]>'
        b"<Error><Code>&b;</Code></Error>"
    ),
    "doctype with a code": b"<!DOCTYPE x><Error><Code>AccessDenied</Code></Error>",
    "oversized": b"<Error><Code>AccessDenied</Code><Message>"
    + b"a" * 100_000
    + b"</Message></Error>",
    "utf-16": "<Error><Code>AccessDenied</Code></Error>".encode("utf-16"),
    "not xml": b"<html><body>502 Bad Gateway</body></html>",
    "wrong root": b"<Other><Code>AccessDenied</Code></Other>",
    "truncated": b"<Error><Code>AccessDeni",
    "empty": b"",
}


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_hostile_bodies_yield_no_code_and_are_never_echoed(
    client: ObjectStoreClient, fake: FakeS3, name: str
) -> None:
    fake.fail(403, "unused", times=1, method="GET", body=HOSTILE[name])
    with pytest.raises(GraphCliError) as caught:
        client.get_object("k")
    error = caught.value
    assert error.code == "backend.unavailable"
    assert "s3_code" not in error.details
    assert error.details["status"] == 403
    for fragment in ("aaaa", "Bad Gateway", "html", "Message"):
        assert fragment not in error.message + json.dumps(dict(error.details))


@pytest.mark.parametrize(
    "code",
    ["Access Denied", "<script>", "A" * 65, "x\ny", "SECRET=" + SECRET_ACCESS_KEY, "", "1Code"],
)
def test_odd_codes_are_dropped_not_echoed(
    client: ObjectStoreClient, fake: FakeS3, code: str
) -> None:
    body = f"<Error><Code>{code.replace('<', '&lt;')}</Code></Error>".encode()
    fake.fail(400, "unused", times=1, method="PUT", body=body)
    with pytest.raises(GraphCliError) as caught:
        _put(client)
    assert "s3_code" not in caught.value.details
    assert code not in caught.value.message or code == ""
    assert SECRET_ACCESS_KEY not in str(dict(caught.value.details)) + caught.value.message


def test_sanitize_code() -> None:
    assert sanitize_code("SlowDown") == "SlowDown"
    assert sanitize_code("XMinioAdminBucketQuotaExceeded") == "XMinioAdminBucketQuotaExceeded"
    assert sanitize_code(None) is None
    assert sanitize_code("with space") is None


# -- redaction ----------------------------------------------------------------------------------

NEEDLES = (
    SECRET_ACCESS_KEY,
    ACCESS_KEY_ID,
    SESSION_TOKEN,
    "AKIAWRONGKEY",
    "wrongsecretwrongsecret",
    "AWS4-HMAC-SHA256",
    "Credential=",
    "SignedHeaders=",
    "Signature=",
    "<StringToSign",
    "<CanonicalRequest",
    "<AWSAccessKeyId",
    "<SignatureProvided",
    "x-amz-security-token",
    "x-amz-content-sha256",
    "Authorization:",
    "FAKE0001",
    "injected failure",
    "does not match the signature",
    "you provided",
)


def chain(error: BaseException) -> list[BaseException]:
    found, todo = [], [error]
    while todo:
        current = todo.pop()
        if current in found:
            continue
        found.append(current)
        todo += [e for e in (current.__cause__, current.__context__) if e is not None]
    return found


def assert_redacted(error: Exception, fake: FakeS3, extra: tuple[str, ...] = ()) -> None:
    signatures = [
        match.group(1)
        for request in fake.requests
        for match in re.finditer(r"Signature=([0-9a-f]+)", request.authorization)
    ]
    authorizations = [r.authorization for r in fake.requests if r.authorization]
    forbidden = (*NEEDLES, *signatures, *authorizations, *extra)
    texts = [str(error), repr(error), "".join(traceback.format_exception(error))]
    if isinstance(error, GraphCliError):
        texts += [error.message, json.dumps(dict(error.details)), repr(dict(error.details))]
    assert error.__cause__ is None and error.__context__ is None
    for item in chain(error):
        texts.append(str(item))
        texts.append(repr(item))
    for text in texts:
        for needle in forbidden:
            assert needle not in text, f"{needle!r} leaked into {text!r}"


def scenario_wrong_secret(fake: FakeS3, make: ClientFactory) -> Callable[[], object]:
    creds = ObjectStoreCredentials(ACCESS_KEY_ID, "wrongsecretwrongsecret" * 2)
    return lambda: make(fake.settings(credentials=creds)).get_object("k")


def scenario_wrong_key(fake: FakeS3, make: ClientFactory) -> Callable[[], object]:
    creds = ObjectStoreCredentials("AKIAWRONGKEY", SECRET_ACCESS_KEY)
    return lambda: make(fake.settings(credentials=creds)).get_object("k")


def scenario_skew(fake: FakeS3, make: ClientFactory) -> Callable[[], object]:
    later = FixedClock(fake.clock.now() + timedelta(hours=2))
    return lambda: make(clock=later).get_object("k")


def scenario_wrong_region(fake: FakeS3, make: ClientFactory) -> Callable[[], object]:
    return lambda: make(fake.settings(region="elsewhere-1")).get_object("k")


def scenario_denied_put(fake: FakeS3, make: ClientFactory) -> Callable[[], object]:
    fake.fail(403, "AccessDenied", times=3, method="PUT")
    return lambda: _put(make())


def scenario_quota_put(fake: FakeS3, make: ClientFactory) -> Callable[[], object]:
    fake.fail(403, "QuotaExceeded", times=3, method="PUT")
    return lambda: _put(make())


def scenario_exhausted_503(fake: FakeS3, make: ClientFactory) -> Callable[[], object]:
    fake.fail(503, "SlowDown", times=10, method="GET")
    return lambda: make().get_object("k")


def scenario_expired_token(fake: FakeS3, make: ClientFactory) -> Callable[[], object]:
    fake.fail(400, "ExpiredToken", times=3, method="GET")
    return lambda: make().get_object("k")


def scenario_no_bucket(fake: FakeS3, make: ClientFactory) -> Callable[[], object]:
    return lambda: list(make(fake.settings(bucket="no-such-bucket")).list_objects_v2(""))


def scenario_timeout(fake: FakeS3, make: ClientFactory) -> Callable[[], object]:
    fake.delay(1.0, times=10, method="GET")
    client = make(timeouts=Timeouts(2.0, 0.1), retry=RetryPolicy(max_retries=1))
    return lambda: client.get_object("k")


def scenario_refused(fake: FakeS3, make: ClientFactory) -> Callable[[], object]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    settings = fake.settings(endpoint_url=f"http://127.0.0.1:{port}")
    return lambda: make(settings).get_object("k")


def scenario_missing_secret(fake: FakeS3, make: ClientFactory) -> Callable[[], object]:
    settings = fake.settings(credentials=ObjectStoreCredentials(ACCESS_KEY_ID, ""))
    return lambda: make(settings).get_object("k")


def scenario_hostile_body(fake: FakeS3, make: ClientFactory) -> Callable[[], object]:
    body = f"<Error><Code>AccessDenied</Code><Message>{SECRET_ACCESS_KEY}</Message></Error>"
    fake.fail(403, "x", times=1, method="GET", body=b"<!DOCTYPE a>" + body.encode())
    return lambda: make().get_object("k")


def scenario_wrong_hash_put(fake: FakeS3, make: ClientFactory) -> Callable[[], object]:
    return lambda: make().put_object("k", io.BytesIO(DATA), len(DATA), "0" * 64)


SCENARIOS = {
    "wrong secret": scenario_wrong_secret,
    "wrong key id": scenario_wrong_key,
    "clock skew": scenario_skew,
    "wrong region": scenario_wrong_region,
    "denied put": scenario_denied_put,
    "quota put": scenario_quota_put,
    "exhausted 503": scenario_exhausted_503,
    "expired token": scenario_expired_token,
    "no bucket": scenario_no_bucket,
    "timeout": scenario_timeout,
    "connection refused": scenario_refused,
    "missing secret": scenario_missing_secret,
    "hostile body": scenario_hostile_body,
    "wrong payload hash": scenario_wrong_hash_put,
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_no_secret_in_any_error_log_or_chain(
    name: str,
    fake: FakeS3,
    make_client: ClientFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    run = SCENARIOS[name](fake, make_client)
    with pytest.raises(GraphCliError) as caught:
        run()
    assert_redacted(caught.value, fake)
    for record in caplog.records:
        text = record.getMessage()
        assert not any(needle in text for needle in NEEDLES)
    assert [r for r in caplog.records if r.name.startswith("graph_cli")] == []


def test_settings_and_client_reprs_hide_the_credentials(
    fake: FakeS3, client: ObjectStoreClient
) -> None:
    settings = ObjectStoreSettings(
        bucket=BUCKET,
        region=REGION,
        credentials=ObjectStoreCredentials(ACCESS_KEY_ID, SECRET_ACCESS_KEY, SESSION_TOKEN),
    )
    for text in (repr(settings), repr(client), str(settings), repr(settings.credentials)):
        assert SECRET_ACCESS_KEY not in text
        assert ACCESS_KEY_ID not in text
        assert SESSION_TOKEN not in text


def test_a_failed_stream_leaks_nothing_either(
    fake: FakeS3, make_client: ClientFactory, sleeps: Sleeps
) -> None:
    client = make_client(timeouts=Timeouts(2.0, 0.2), chunk_size=10)
    data = bytes(100)
    client.put_object("s", io.BytesIO(data), 100, hashlib.sha256(data).hexdigest())
    fake.stall_get_body(after_bytes=30, seconds=1.0)
    with pytest.raises(GraphCliError) as caught, client.get_object("s") as body:
        list(body)
    assert_redacted(caught.value, fake)
