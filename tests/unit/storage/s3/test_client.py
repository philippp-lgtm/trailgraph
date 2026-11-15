"""The S3 client against the in-process fake: round trips, streaming, listing, retries,
timeouts, addressing, TLS."""

import hashlib
import io
import shutil
import socket
import ssl
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from support.clock import FixedClock
from support.fake_s3 import ACCESS_KEY_ID, BUCKET, REGION, SECRET_ACCESS_KEY, FakeS3
from support.s3_helpers import T0, ClientFactory, Sleeps

from graph_cli.kernel.errors import GraphCliError
from graph_cli.storage.s3.client import (
    ObjectInfo,
    ObjectSourceError,
    ObjectStoreClient,
    RetryPolicy,
    Timeouts,
)
from graph_cli.storage.s3.errors import ObjectContentMismatch, ObjectNotFound
from graph_cli.storage.s3.settings import ObjectStoreCredentials, ObjectStoreSettings


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def put(client: ObjectStoreClient, key: str, data: bytes) -> None:
    client.put_object(key, io.BytesIO(data), len(data), sha(data))


def read_all(client: ObjectStoreClient, key: str, **options: object) -> bytes:
    with client.get_object(key, **options) as body:  # type: ignore[arg-type]
        return b"".join(body)


# -- round trips ------------------------------------------------------------------------------


def test_put_get_head_delete_round_trip(client: ObjectStoreClient, fake: FakeS3) -> None:
    data = b"hello object store\n"
    key = "graphs/g1/blobs/sha256/ab/" + sha(data)
    put(client, key, data)
    assert fake.get(key) == data
    assert read_all(client, key) == data
    info = client.head_object(key)
    assert info == ObjectInfo(key, len(data), T0, T0)
    client.delete_object(key)
    assert fake.get(key) is None
    assert client.head_object(key) is None
    client.delete_object(key)  # absent: not an error


def test_keys_with_unusual_characters_are_signed_and_stored_once_encoded(
    client: ObjectStoreClient, fake: FakeS3
) -> None:
    key = "dir one/a+b c$d&e=é/%41"
    put(client, key, b"x")
    assert fake.keys() == [key]
    assert read_all(client, key) == b"x"
    assert client.head_object(key) is not None


def test_upload_is_signed_with_the_real_payload_hash(
    client: ObjectStoreClient, fake: FakeS3
) -> None:
    put(client, "k", b"payload")
    assert fake.calls("PUT")[0].status == 200
    # a hash that does not match the bytes is refused by the store
    with pytest.raises(GraphCliError) as caught:
        client.put_object("k2", io.BytesIO(b"payload"), 7, sha(b"other"))
    assert caught.value.code == "backend.unavailable"
    assert caught.value.details["s3_code"] == "XAmzContentSHA256Mismatch"
    assert fake.get("k2") is None


def test_put_reads_exactly_size_bytes_from_the_current_position(
    client: ObjectStoreClient, fake: FakeS3
) -> None:
    source = io.BytesIO(b"skip-THIS-and-ignore-the-rest")
    source.seek(5)
    client.put_object("k", source, 4, sha(b"THIS"))
    assert fake.get("k") == b"THIS"


def test_put_of_a_short_source_raises_a_typed_error(client: ObjectStoreClient) -> None:
    with pytest.raises(ObjectSourceError):
        client.put_object("k", io.BytesIO(b"abc"), 10, sha(b"abcdefghij"))


def test_large_object_streams_over_many_chunks(
    client: ObjectStoreClient, fake: FakeS3, tmp_path: Path
) -> None:
    data = bytes(range(256)) * (5 * 4096) + b"tail"  # about 5 MiB
    source = tmp_path / "big.bin"
    source.write_bytes(data)
    with source.open("rb") as handle:
        client.put_object("big", handle, len(data), sha(data))
    assert fake.get("big") == data
    with client.get_object("big", expected_sha256=sha(data)) as body:
        assert body.size == len(data)
        chunks = list(body)
    assert len(chunks) > 70
    assert all(len(chunk) <= 64 * 1024 for chunk in chunks)
    assert b"".join(chunks) == data


def test_empty_object_round_trip_with_verification(client: ObjectStoreClient) -> None:
    put(client, "empty", b"")
    assert read_all(client, "empty", expected_sha256=sha(b"")) == b""
    with pytest.raises(ObjectContentMismatch):
        read_all(client, "empty", expected_sha256=sha(b"x"))


def test_an_object_body_can_be_read_once(client: ObjectStoreClient) -> None:
    put(client, "k", b"abc")
    with client.get_object("k") as body:
        assert b"".join(body) == b"abc"
        with pytest.raises(RuntimeError):
            iter(body)


def test_invalid_arguments_are_programming_errors(client: ObjectStoreClient) -> None:
    for key in ("", "/lead", "a//b", "a/../b", "a/./b", "nul\x00"):
        with pytest.raises(ValueError):
            client.head_object(key)
    with pytest.raises(ValueError):
        client.get_object("k", expected_sha256="ABC")
    with pytest.raises(ValueError):
        client.put_object("k", io.BytesIO(b""), 0, "sha256:" + sha(b""))


# -- verified streaming -----------------------------------------------------------------------


def test_verified_get_holds_back_the_last_chunk_on_a_mismatch(
    make_client: ClientFactory, fake: FakeS3
) -> None:
    client = make_client(chunk_size=1000)
    data = bytes([7]) * 3500
    put(client, "k", data)
    fake.corrupt_get("k", bytes([8]) * 3500)  # same length, other bytes
    received = bytearray()
    with (
        pytest.raises(ObjectContentMismatch) as caught,
        client.get_object("k", expected_sha256=sha(data)) as body,
    ):
        for chunk in body:
            received += chunk
    assert caught.value.expected == sha(data)
    assert caught.value.actual == sha(bytes([8]) * 3500)
    # 1000, 1000, 1000 and 500 bytes: the 500-byte tail was never handed out
    assert len(received) == 3000
    assert bytes(received) == bytes([8]) * 3000


def test_verified_get_of_one_chunk_delivers_nothing_on_a_mismatch(
    client: ObjectStoreClient, fake: FakeS3
) -> None:
    put(client, "k", b"genuine")
    fake.corrupt_get("k", b"forged!")
    delivered: list[bytes] = []
    with (
        pytest.raises(ObjectContentMismatch),
        client.get_object("k", expected_sha256=sha(b"genuine")) as body,
    ):
        delivered.extend(body)
    assert delivered == []


def test_verified_get_of_a_truncated_body_never_matches(
    client: ObjectStoreClient, fake: FakeS3
) -> None:
    put(client, "k", b"0123456789")
    fake.corrupt_get("k", b"01234")
    with pytest.raises(ObjectContentMismatch):
        read_all(client, "k", expected_sha256=sha(b"0123456789"))


def test_verified_get_passes_matching_content_chunk_by_chunk(
    make_client: ClientFactory,
) -> None:
    client = make_client(chunk_size=10)
    data = bytes(range(100))
    put(client, "k", data)
    with client.get_object("k", expected_sha256=sha(data)) as body:
        chunks = list(body)
    assert len(chunks) == 10
    assert b"".join(chunks) == data


def test_unverified_get_hands_chunks_out_as_they_arrive(
    make_client: ClientFactory, fake: FakeS3
) -> None:
    client = make_client(chunk_size=10)
    put(client, "k", bytes(25))
    fake.corrupt_get("k", bytes([1]) * 25)
    # no expected hash: nothing is checked, nothing is held back
    assert read_all(client, "k") == bytes([1]) * 25


def test_get_of_a_missing_object_is_a_typed_not_found(client: ObjectStoreClient) -> None:
    with pytest.raises(ObjectNotFound):
        client.get_object("absent")


# -- prefix and listing -----------------------------------------------------------------------


def test_keys_are_relative_to_the_settings_prefix(make_client: ClientFactory, fake: FakeS3) -> None:
    client = make_client(fake.settings(prefix="graph_cli/"))
    put(client, "graphs/g1/a", b"1")
    fake.put("other/zzz", b"2")  # outside the prefix
    fake.put("graph_cli/graphs/g2/b", b"3")
    assert "graph_cli/graphs/g1/a" in fake.objects
    assert [e.key for e in client.list_objects_v2("graphs/")] == ["graphs/g1/a", "graphs/g2/b"]
    assert [e.key for e in client.list_objects_v2("graphs/g2/")] == ["graphs/g2/b"]
    assert client.head_object("graphs/g1/a") is not None
    assert client.head_object("other/zzz") is None


def test_listing_follows_continuation_tokens(
    make_client: ClientFactory, fake: FakeS3, clock: FixedClock
) -> None:
    fake.page_size = 3
    client = make_client()
    for index in range(8):
        clock.advance(timedelta(seconds=1))
        put(client, f"p/{index:02d}", b"x" * index)
    fake.put("q/unrelated", b"")
    fake.requests.clear()
    entries = list(client.list_objects_v2("p/"))
    assert [e.key for e in entries] == [f"p/{i:02d}" for i in range(8)]
    assert [e.size for e in entries] == list(range(8))
    assert entries[0].last_modified == T0 + timedelta(seconds=1)
    assert all(e.response_date == clock.now() for e in entries)
    lists = fake.calls("GET")
    assert len(lists) == 3
    assert [r.query.get("continuation-token") is not None for r in lists] == [False, True, True]


def test_list_pages_expose_each_pages_response_date(
    make_client: ClientFactory, fake: FakeS3, clock: FixedClock
) -> None:
    fake.page_size = 2
    client = make_client()
    for index in range(5):
        put(client, f"p/{index}", b"")
    dates = []
    for page in client.list_pages("p/"):
        dates.append(page.response_date)
        clock.advance(timedelta(minutes=1))
    assert dates == [T0, T0 + timedelta(minutes=1), T0 + timedelta(minutes=2)]


def test_max_keys_limits_each_page(make_client: ClientFactory, fake: FakeS3) -> None:
    client = make_client()
    for index in range(5):
        put(client, f"p/{index}", b"")
    pages = list(client.list_pages("p/", max_keys=2))
    assert [len(p.entries) for p in pages] == [2, 2, 1]


def test_empty_listing(client: ObjectStoreClient) -> None:
    assert list(client.list_objects_v2("nothing/")) == []


def test_lagging_listing_hides_new_objects_but_head_sees_them(
    client: ObjectStoreClient, fake: FakeS3, clock: FixedClock
) -> None:
    fake.listing_lag = timedelta(seconds=30)
    put(client, "p/new", b"x")
    assert list(client.list_objects_v2("p/")) == []
    assert client.head_object("p/new") is not None
    clock.advance(timedelta(seconds=31))
    assert [e.key for e in client.list_objects_v2("p/")] == ["p/new"]


def test_a_repeating_continuation_token_is_refused(client: ObjectStoreClient, fake: FakeS3) -> None:
    body = (
        b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        b"<IsTruncated>true</IsTruncated><NextContinuationToken>same</NextContinuationToken>"
        b"</ListBucketResult>"
    )
    fake.fail(200, "x", times=5, body=body)
    with pytest.raises(GraphCliError) as caught:
        list(client.list_objects_v2("p/"))
    assert caught.value.code == "backend.unavailable"


def test_a_truncated_listing_without_a_token_is_refused(
    client: ObjectStoreClient, fake: FakeS3
) -> None:
    body = b"<ListBucketResult><IsTruncated>true</IsTruncated></ListBucketResult>"
    fake.fail(200, "x", body=body)
    with pytest.raises(GraphCliError):
        list(client.list_objects_v2("p/"))


def test_a_listing_with_a_doctype_is_refused(client: ObjectStoreClient, fake: FakeS3) -> None:
    body = (
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]>'
        b"<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>"
    )
    fake.fail(200, "x", body=body)
    with pytest.raises(GraphCliError) as caught:
        list(client.list_objects_v2(""))
    assert caught.value.code == "backend.unavailable"


# -- retries ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code"),
    [(500, "InternalError"), (503, "SlowDown"), (502, "BadGateway")],
)
def test_transient_failures_are_retried_with_backoff_and_jitter(
    client: ObjectStoreClient, fake: FakeS3, sleeps: Sleeps, status: int, code: str
) -> None:
    put(client, "k", b"data")
    fake.requests.clear()
    fake.fail(status, code, times=2, method="HEAD")
    assert client.head_object("k") is not None
    assert len(fake.calls("HEAD")) == 3
    # jitter 0.5 of ceilings 0.5 and 1.0
    assert sleeps.waits == [0.25, 0.5]


def test_a_request_timeout_answer_is_retried(
    client: ObjectStoreClient, fake: FakeS3, sleeps: Sleeps
) -> None:
    fake.fail(400, "RequestTimeout", times=1, method="PUT")
    put(client, "k", b"data")
    assert fake.get("k") == b"data"
    assert len(fake.calls("PUT")) == 2
    assert len(sleeps.waits) == 1


def test_backoff_grows_exponentially_up_to_the_cap_and_scales_with_jitter(
    make_client: ClientFactory, fake: FakeS3, sleeps: Sleeps
) -> None:
    client = make_client(retry=RetryPolicy(max_retries=5, base_s=1.0, cap_s=4.0))
    fake.fail(503, "SlowDown", times=5, method="HEAD")
    sleeps.jitter = 1.0
    assert client.head_object("k") is None
    assert sleeps.waits == [1.0, 2.0, 4.0, 4.0, 4.0]


def test_retries_are_exhausted_after_three_retries(
    client: ObjectStoreClient, fake: FakeS3, sleeps: Sleeps
) -> None:
    fake.fail(503, "SlowDown", times=10)
    with pytest.raises(GraphCliError) as caught:
        client.get_object("k")
    error = caught.value
    assert error.code == "backend.unavailable"
    assert dict(error.details) == {
        "operation": "GetObject",
        "endpoint_host": f"127.0.0.1:{fake.port}",
        "status": 503,
        "s3_code": "SlowDown",
        "attempts": 4,
    }
    assert len(fake.calls("GET")) == 4
    assert len(sleeps.waits) == 3


def test_permanent_errors_are_not_retried(
    client: ObjectStoreClient, fake: FakeS3, sleeps: Sleeps
) -> None:
    fake.fail(403, "AccessDenied", times=5, method="GET")
    with pytest.raises(GraphCliError):
        client.get_object("k")
    assert len(fake.calls("GET")) == 1
    assert sleeps.waits == []


def test_put_is_replayed_in_full_on_retry(
    client: ObjectStoreClient, fake: FakeS3, sleeps: Sleeps
) -> None:
    data = bytes(range(256)) * 1000
    fake.fail(500, "InternalError", times=2, method="PUT")
    client.put_object("k", io.BytesIO(data), len(data), sha(data))
    assert fake.get("k") == data
    assert [r.body_bytes for r in fake.calls("PUT")] == [len(data)] * 3
    assert len(sleeps.waits) == 2


def test_a_source_that_cannot_rewind_gets_one_attempt(
    client: ObjectStoreClient, fake: FakeS3, sleeps: Sleeps
) -> None:
    class OneWay(io.BytesIO):
        def seekable(self) -> bool:
            return False

    fake.fail(500, "InternalError", times=1, method="PUT")
    with pytest.raises(GraphCliError):
        client.put_object("k", OneWay(b"abc"), 3, sha(b"abc"))
    assert len(fake.calls("PUT")) == 1
    assert sleeps.waits == []


def test_a_refused_connection_is_retried_then_unavailable(
    make_client: ClientFactory, sleeps: Sleeps
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]  # nothing listens here once the socket is closed
    settings = ObjectStoreSettings(
        bucket=BUCKET,
        region=REGION,
        endpoint_url=f"http://127.0.0.1:{port}",
        credentials=ObjectStoreCredentials(ACCESS_KEY_ID, SECRET_ACCESS_KEY),
    )
    client = make_client(settings)
    with pytest.raises(GraphCliError) as caught:
        client.head_object("k")
    assert caught.value.code == "backend.unavailable"
    assert caught.value.details["reason"] == "connection refused"
    assert caught.value.details["attempts"] == 4
    assert len(sleeps.waits) == 3


# -- timeouts ---------------------------------------------------------------------------------


def test_a_slow_answer_times_out_and_the_retry_succeeds(
    make_client: ClientFactory, fake: FakeS3, sleeps: Sleeps
) -> None:
    client = make_client(timeouts=Timeouts(connect_s=2.0, read_s=0.3))
    put(client, "k", b"x")
    fake.delay(1.5, times=1, method="HEAD")
    assert client.head_object("k") is not None
    assert len(sleeps.waits) == 1


def test_timeouts_that_persist_end_as_unavailable(make_client: ClientFactory, fake: FakeS3) -> None:
    client = make_client(
        timeouts=Timeouts(connect_s=2.0, read_s=0.2), retry=RetryPolicy(max_retries=1)
    )
    fake.delay(1.0, times=5)
    with pytest.raises(GraphCliError) as caught:
        client.head_object("k")
    assert caught.value.code == "backend.unavailable"
    assert caught.value.details["reason"] == "timeout"
    assert caught.value.details["attempts"] == 2


def test_a_stalled_body_times_out_per_chunk_without_retry(
    make_client: ClientFactory, fake: FakeS3, sleeps: Sleeps
) -> None:
    client = make_client(timeouts=Timeouts(connect_s=2.0, read_s=0.3), chunk_size=50)
    put(client, "k", bytes(1000))
    fake.stall_get_body(after_bytes=120, seconds=2.0)
    received = bytearray()
    with pytest.raises(GraphCliError) as caught, client.get_object("k") as body:
        for chunk in body:
            received += chunk
    assert caught.value.code == "backend.unavailable"
    assert caught.value.details["reason"] == "timeout"
    # a partial read of 20 bytes is lost with the timeout: two whole chunks arrived
    assert len(received) == 100
    assert sleeps.waits == []


def test_a_body_cut_short_is_an_error_even_without_a_hash(
    client: ObjectStoreClient, fake: FakeS3, sleeps: Sleeps
) -> None:
    put(client, "k", bytes(100))
    fake.truncate_get_body(after_bytes=40)
    with pytest.raises(GraphCliError) as caught, client.get_object("k") as body:
        for _ in body:
            pass
    assert caught.value.code == "backend.unavailable"
    assert sleeps.waits == []


# -- settings ---------------------------------------------------------------------------------


def test_missing_credentials_fail_before_any_request(
    make_client: ClientFactory, fake: FakeS3
) -> None:
    client = make_client(fake.settings(credentials=None))
    for call in (
        lambda: client.head_object("k"),
        lambda: client.get_object("k"),
        lambda: client.delete_object("k"),
        lambda: client.put_object("k", io.BytesIO(b""), 0, sha(b"")),
        lambda: list(client.list_objects_v2("")),
    ):
        with pytest.raises(GraphCliError) as caught:
            call()
        assert caught.value.code == "backend.unavailable"
        assert "access key id and secret access key" in caught.value.message
        assert caught.value.details["missing"] == ["access key id", "secret access key"]
    assert fake.requests == []


def test_an_empty_secret_is_named_as_missing(make_client: ClientFactory, fake: FakeS3) -> None:
    settings = fake.settings(credentials=ObjectStoreCredentials(ACCESS_KEY_ID, ""))
    with pytest.raises(GraphCliError) as caught:
        make_client(settings).head_object("k")
    assert caught.value.details["missing"] == ["secret access key"]
    assert fake.requests == []


def test_plain_http_to_a_remote_host_needs_explicit_permission(
    make_client: ClientFactory, fake: FakeS3
) -> None:
    settings = fake.settings(endpoint_url="http://store.example.invalid:9000")
    with pytest.raises(GraphCliError) as caught:
        make_client(settings).head_object("k")
    assert caught.value.code == "backend.unavailable"
    assert "plain http" in caught.value.message
    assert caught.value.details["endpoint_host"] == "store.example.invalid:9000"


def test_plain_http_permission_lets_the_request_through(
    make_client: ClientFactory, fake: FakeS3
) -> None:
    settings = fake.settings(
        endpoint_url="http://store.example.invalid:9000", allow_plain_http=True
    )
    with pytest.raises(GraphCliError) as caught:
        make_client(settings, retry=RetryPolicy(max_retries=0)).head_object("k")
    assert caught.value.details["reason"] == "name resolution failed"


def test_neither_endpoint_nor_region_is_refused(make_client: ClientFactory, fake: FakeS3) -> None:
    settings = fake.settings(endpoint_url=None, region="")
    with pytest.raises(GraphCliError) as caught:
        make_client(settings).head_object("k")
    assert caught.value.code == "backend.unavailable"


# -- addressing -------------------------------------------------------------------------------


def test_aws_endpoint_follows_the_region() -> None:
    settings = ObjectStoreSettings(
        bucket="my-bucket",
        region="eu-central-1",
        credentials=ObjectStoreCredentials("a", "b"),
    )
    path_style = ObjectStoreClient(settings)._endpoint("HeadObject")
    assert path_style.tls and path_style.port == 443
    assert path_style.connect_host == "s3.eu-central-1.amazonaws.com"
    assert path_style.host_header == "s3.eu-central-1.amazonaws.com"
    assert path_style.display_host == "s3.eu-central-1.amazonaws.com"
    virtual = ObjectStoreClient(
        ObjectStoreSettings(
            bucket="my-bucket",
            region="eu-central-1",
            addressing="virtual",
            credentials=ObjectStoreCredentials("a", "b"),
        )
    )
    endpoint = virtual._endpoint("HeadObject")
    assert endpoint.connect_host == "my-bucket.s3.eu-central-1.amazonaws.com"
    assert endpoint.host_header == "my-bucket.s3.eu-central-1.amazonaws.com"
    assert endpoint.display_host == "s3.eu-central-1.amazonaws.com"
    assert virtual._object_path("a/b") == "/a/b"
    assert virtual._bucket_path() == "/"
    assert ObjectStoreClient(settings)._object_path("a/b") == "/my-bucket/a/b"
    assert ObjectStoreClient(settings)._bucket_path() == "/my-bucket"


def test_custom_endpoint_keeps_a_non_default_port_in_the_host(fake: FakeS3) -> None:
    settings = fake.settings(addressing="virtual")
    endpoint = ObjectStoreClient(settings)._endpoint("HeadObject")
    assert endpoint.host_header == f"{BUCKET}.127.0.0.1:{fake.port}"
    assert endpoint.display_host == f"127.0.0.1:{fake.port}"
    assert not endpoint.bucket_in_path


def test_virtual_hosted_addressing_round_trip(
    make_client: ClientFactory,
    fake: FakeS3,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bucket name goes into the host (and the signature); the fake resolves it the same."""
    real = socket.getaddrinfo

    def resolve(host: str, *args: object, **kwargs: object) -> object:
        return real("127.0.0.1" if host == f"{BUCKET}.127.0.0.1" else host, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    client = make_client(fake.settings(addressing="virtual", prefix="p/"))
    put(client, "a b/c", b"virtual")
    assert fake.keys() == ["p/a b/c"]
    assert read_all(client, "a b/c") == b"virtual"
    assert client.head_object("a b/c") is not None
    assert [e.key for e in client.list_objects_v2("a")] == ["a b/c"]
    client.delete_object("a b/c")
    assert fake.keys() == []


def test_session_tokens_are_sent_and_checked(
    clock: FixedClock, make_client: ClientFactory, sleeps: Sleeps
) -> None:
    with FakeS3(clock=clock, session_token="session-token-value") as store:
        with_token = store.settings()
        assert with_token.credentials is not None
        assert with_token.credentials.session_token == "session-token-value"
        client = make_client(with_token)
        put(client, "k", b"x")
        assert client.head_object("k") is not None
        without = store.settings(
            credentials=ObjectStoreCredentials(ACCESS_KEY_ID, SECRET_ACCESS_KEY)
        )
        with pytest.raises(GraphCliError) as caught:
            make_client(without).get_object("k")
        assert "refused the credentials" in caught.value.message


# -- TLS --------------------------------------------------------------------------------------


@pytest.fixture
def certificate(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    if shutil.which("openssl") is None:
        pytest.skip("openssl is not installed")
    directory = tmp_path_factory.mktemp("tls")
    cert, key = directory / "cert.pem", directory / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "2",
            "-subj", "/CN=localhost",
            "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost",
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    return cert, key


def test_tls_with_a_ca_bundle_and_without(
    clock: FixedClock,
    make_client: ClientFactory,
    certificate: tuple[Path, Path],
) -> None:
    with FakeS3(clock=clock, tls=certificate) as store:
        trusted = make_client(store.settings(ca_bundle=certificate[0]))
        put(trusted, "k", b"secure")
        assert read_all(trusted, "k") == b"secure"
        untrusted = make_client(store.settings(), retry=RetryPolicy(max_retries=0))
        with pytest.raises(GraphCliError) as caught:
            untrusted.head_object("k")
        assert caught.value.details["reason"] == "TLS certificate verification failed"


def test_an_unreadable_ca_bundle_is_reported_without_its_path(
    make_client: ClientFactory, fake: FakeS3, tmp_path: Path
) -> None:
    settings = fake.settings(
        endpoint_url="https://127.0.0.1:1", ca_bundle=tmp_path / "missing_bundle.pem"
    )
    with pytest.raises(GraphCliError) as caught:
        make_client(settings).head_object("k")
    assert "CA bundle" in caught.value.message
    assert "missing_bundle" not in caught.value.message + repr(dict(caught.value.details))


def test_https_to_a_plain_http_server_fails_the_handshake(
    make_client: ClientFactory, fake: FakeS3
) -> None:
    settings = fake.settings(endpoint_url=f"https://127.0.0.1:{fake.port}")
    with pytest.raises(GraphCliError) as caught:
        make_client(settings, retry=RetryPolicy(max_retries=0)).head_object("k")
    assert caught.value.code == "backend.unavailable"
    assert isinstance(caught.value.details["reason"], str)
    assert ssl.OPENSSL_VERSION  # the TLS stack is the standard library's
