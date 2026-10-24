"""The server endpoint of the remote mode (``[runtime] endpoint``, contracts B12.7, B14.10).

An ``http://`` or ``https://`` URL without user info, query or fragment; a
path is a prefix for the API (a proxy that serves it below a path). Plain
HTTP is refused for a host that is not loopback unless the configuration
allows it: the bearer token would travel in clear.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

LOOPBACK_NAMES: Final = frozenset({"localhost"})


@dataclass(frozen=True, slots=True)
class Endpoint:
    scheme: str  # http | https
    host: str
    port: int
    path: str  # "" or "/prefix", without a trailing slash; "/v1/..." follows it

    @property
    def display(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}{self.path}"

    @property
    def plain_http(self) -> bool:
        return self.scheme == "http"

    @property
    def loopback(self) -> bool:
        return is_loopback(self.host)


def is_loopback(host: str) -> bool:
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_endpoint(text: str) -> Endpoint | str:
    """The endpoint, or why the text is none."""
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as exc:
        return f"is not a URL: {exc}"
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "must be an http:// or https:// URL with a host"
    if parts.username or parts.password or parts.query or parts.fragment:
        return "must not carry user info, a query or a fragment"
    default = 443 if parts.scheme == "https" else 80
    return Endpoint(parts.scheme, parts.hostname, port or default, parts.path.rstrip("/"))
