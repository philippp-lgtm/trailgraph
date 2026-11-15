"""SigV4 signing against the published AWS examples and an independent computation.

The vectors are the worked examples of "Authenticating Requests: Using the Authorization
Header (AWS Signature Version 4)" in the Amazon S3 API reference (example credentials
``AKIAIOSFODNN7EXAMPLE``, bucket ``examplebucket``, region ``us-east-1``, request time
2013-05-24T00:00:00Z). Each expected signature is the documented one, reproduced exactly.
"""

import hashlib
import hmac
from datetime import UTC, datetime

import pytest

from graph_cli.storage.s3 import signing
from graph_cli.storage.s3.settings import ObjectStoreCredentials

CREDENTIALS = ObjectStoreCredentials(
    "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
)
NOW = datetime(2013, 5, 24, tzinfo=UTC)
HOST = "examplebucket.s3.amazonaws.com"
EMPTY = signing.EMPTY_PAYLOAD_SHA256
SCOPE = "AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request"


def authorization(**request: object) -> str:
    headers = signing.sign_request(
        credentials=CREDENTIALS,
        region="us-east-1",
        now=NOW,
        **request,  # type: ignore[arg-type]
    )
    return headers["authorization"]


def test_empty_payload_hash_is_the_published_constant() -> None:
    assert EMPTY == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_vector_get_object_with_range() -> None:
    """GET /test.txt, ``Range: bytes=0-9``."""
    assert authorization(
        method="GET",
        path="/test.txt",
        query=[],
        headers={"host": HOST, "range": "bytes=0-9"},
        payload_sha256=EMPTY,
    ) == (
        f"AWS4-HMAC-SHA256 Credential={SCOPE},"
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date,"
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )


def test_vector_get_object_intermediate_values() -> None:
    """The canonical request and string to sign of the same example, step by step."""
    headers = {
        "host": HOST,
        "range": "bytes=0-9",
        "x-amz-content-sha256": EMPTY,
        "x-amz-date": "20130524T000000Z",
    }
    canonical = signing.canonical_request("GET", "/test.txt", "", headers, EMPTY)
    assert canonical == (
        "GET\n/test.txt\n\n"
        f"host:{HOST}\nrange:bytes=0-9\nx-amz-content-sha256:{EMPTY}\n"
        "x-amz-date:20130524T000000Z\n\n"
        f"host;range;x-amz-content-sha256;x-amz-date\n{EMPTY}"
    )
    to_sign = signing.string_to_sign(
        "20130524T000000Z", signing.credential_scope("20130524", "us-east-1"), canonical
    )
    assert to_sign == (
        "AWS4-HMAC-SHA256\n20130524T000000Z\n20130524/us-east-1/s3/aws4_request\n"
        "7344ae5b7ee6c3e7e6b0fe0640412a37625d1fbfff95c48bbb2dc43964946972"
    )
    key = signing.derive_signing_key(CREDENTIALS.secret_access_key, "20130524", "us-east-1")
    assert (
        signing.signature_of(key, to_sign)
        == "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )


def test_vector_put_object() -> None:
    """PUT /test$file.text with the body ``Welcome to Amazon S3.``: the ``$`` is encoded in
    the canonical URI, and extra signed headers (``date``, a storage class) sort in."""
    body_hash = hashlib.sha256(b"Welcome to Amazon S3.").hexdigest()
    assert body_hash == "44ce7dd67c959e0d3524ffac1771dfbba87d2b6b4b4e99e42034a8b803f8b072"
    assert authorization(
        method="PUT",
        path="/test$file.text",
        query=[],
        headers={
            "host": HOST,
            "date": "Fri, 24 May 2013 00:00:00 GMT",
            "x-amz-storage-class": "REDUCED_REDUNDANCY",
        },
        payload_sha256=body_hash,
    ) == (
        f"AWS4-HMAC-SHA256 Credential={SCOPE},"
        "SignedHeaders=date;host;x-amz-content-sha256;x-amz-date;x-amz-storage-class,"
        "Signature=98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd"
    )


def test_vector_get_bucket_lifecycle() -> None:
    """GET /?lifecycle: a query parameter without a value is ``lifecycle=``."""
    assert authorization(
        method="GET",
        path="/",
        query=[("lifecycle", "")],
        headers={"host": HOST},
        payload_sha256=EMPTY,
    ).endswith("Signature=fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543")


def test_vector_list_objects() -> None:
    """GET /?max-keys=2&prefix=J: parameters are sorted by name."""
    assert authorization(
        method="GET",
        path="/",
        query=[("prefix", "J"), ("max-keys", "2")],
        headers={"host": HOST},
        payload_sha256=EMPTY,
    ).endswith("Signature=34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7")


def test_signing_key_matches_an_independent_derivation() -> None:
    """Not a published value: the four-step HMAC chain written out in the test."""

    def step(key: bytes, message: str) -> bytes:
        return hmac.new(key, message.encode(), hashlib.sha256).digest()

    expected = step(
        step(
            step(step(b"AWS4" + CREDENTIALS.secret_access_key.encode(), "20130524"), "us-east-1"),
            "s3",
        ),
        "aws4_request",
    )
    assert (
        signing.derive_signing_key(CREDENTIALS.secret_access_key, "20130524", "us-east-1")
        == expected
    )


def test_session_token_is_signed_and_sent() -> None:
    credentials = ObjectStoreCredentials("AKID", "SECRET", session_token="TOKEN123")
    headers = signing.sign_request(
        method="GET",
        path="/key",
        query=[],
        headers={"host": HOST},
        payload_sha256=EMPTY,
        credentials=credentials,
        region="eu-west-1",
        now=NOW,
    )
    assert headers["x-amz-security-token"] == "TOKEN123"
    assert headers["x-amz-date"] == "20130524T000000Z"
    assert headers["x-amz-content-sha256"] == EMPTY
    assert (
        "SignedHeaders=host;x-amz-content-sha256;x-amz-date;x-amz-security-token,"
        in (headers["authorization"])
    )
    # Independent recomputation of the signature with the token in the signed headers.
    canonical = "\n".join(
        [
            "GET",
            "/key",
            "",
            f"host:{HOST}",
            f"x-amz-content-sha256:{EMPTY}",
            "x-amz-date:20130524T000000Z",
            "x-amz-security-token:TOKEN123",
            "",
            "host;x-amz-content-sha256;x-amz-date;x-amz-security-token",
            EMPTY,
        ]
    )
    to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            "20130524T000000Z",
            "20130524/eu-west-1/s3/aws4_request",
            hashlib.sha256(canonical.encode()).hexdigest(),
        ]
    )
    key = b"AWS4SECRET"
    for part in ("20130524", "eu-west-1", "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    expected = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    assert headers["authorization"].endswith(f"Signature={expected}")


def test_no_token_means_no_token_header() -> None:
    headers = signing.sign_request(
        method="GET",
        path="/",
        query=[],
        headers={"host": HOST},
        payload_sha256=EMPTY,
        credentials=CREDENTIALS,
        region="us-east-1",
        now=NOW,
    )
    assert "x-amz-security-token" not in headers


def test_timestamp_is_utc_whatever_the_clock_offset() -> None:
    from datetime import timedelta, timezone

    local = datetime(2013, 5, 24, 2, 0, tzinfo=timezone(timedelta(hours=2)))
    assert signing.amz_timestamp(local) == "20130524T000000Z"


@pytest.mark.parametrize(
    ("raw", "encoded"),
    [
        ("AZaz09-._~", "AZaz09-._~"),
        ("a b", "a%20b"),
        ("a+b", "a%2Bb"),
        ("a/b", "a%2Fb"),
        ("$&,:;=?@", "%24%26%2C%3A%3B%3D%3F%40"),
        ("é", "%C3%A9"),
        ("%", "%25"),
        ("*", "%2A"),
    ],
)
def test_uri_encode(raw: str, encoded: str) -> None:
    assert signing.uri_encode(raw) == encoded


def test_object_keys_keep_slashes_and_are_encoded_once() -> None:
    assert signing.canonical_uri("/bucket/graphs/ab/a b+c%d/é") == (
        "/bucket/graphs/ab/a%20b%2Bc%25d/%C3%A9"
    )


def test_canonical_query_sorts_by_encoded_name_then_value() -> None:
    query = [("prefix", "a b"), ("list-type", "2"), ("prefix", "A"), ("continuation-token", "x/y=")]
    assert signing.canonical_query_string(query) == (
        "continuation-token=x%2Fy%3D&list-type=2&prefix=A&prefix=a%20b"
    )
    assert signing.canonical_query_string([]) == ""


def test_canonical_headers_lowercase_trim_and_collapse() -> None:
    block, signed = signing.canonical_headers({"X-Amz-Meta": "  a   b ", "Host": "h"})
    assert block == "host:h\nx-amz-meta:a b\n"
    assert signed == "host;x-amz-meta"
