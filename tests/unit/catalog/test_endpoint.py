"""The server endpoint of the remote mode (contracts B12.7, B14.10)."""

import pytest

from graph_cli.catalog.endpoint import Endpoint, is_loopback, parse_endpoint


def test_endpoints() -> None:
    assert parse_endpoint("https://graphs.example.org") == Endpoint(
        "https", "graphs.example.org", 443, ""
    )
    found = parse_endpoint("http://127.0.0.1:8470/api/")
    assert found == Endpoint("http", "127.0.0.1", 8470, "/api")
    assert isinstance(found, Endpoint) and found.loopback and found.plain_http
    assert found.display == "http://127.0.0.1:8470/api"
    ipv6 = parse_endpoint("http://[::1]:8470")
    assert isinstance(ipv6, Endpoint) and ipv6.display == "http://[::1]:8470"


@pytest.mark.parametrize(
    "text",
    [
        "graphs.example.org",
        "ftp://x.example",
        "https://u:p@x.example",
        "https://x.example/?a=1",
        "http://x.example:99999",
    ],
)
def test_refused(text: str) -> None:
    assert isinstance(parse_endpoint(text), str)


@pytest.mark.parametrize(
    ("host", "loopback"),
    [
        ("localhost", True),
        ("127.0.0.1", True),
        ("127.8.0.1", True),
        ("::1", True),
        ("10.0.0.1", False),
        ("example.org", False),
    ],
)
def test_loopback(host: str, loopback: bool) -> None:
    assert is_loopback(host) is loopback
