"""Object store settings: the immutable value ``catalog`` builds (O1, O3).

Storage never reads the environment or ``config.toml``; it receives this value. The
credentials are kept out of ``repr`` so a settings value in a traceback, log line or
error detail never shows a secret (O2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Addressing = Literal["path", "virtual"]


@dataclass(frozen=True, slots=True)
class ObjectStoreCredentials:
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    session_token: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ObjectStoreSettings:
    """Where and how to reach one S3-compatible bucket.

    ``endpoint_url`` is ``None`` for AWS itself (the endpoint follows from ``region``).
    ``prefix`` is empty or ends with ``/``. ``credentials`` is ``None`` when the
    environment carries none; every call then fails with ``backend.unavailable``.
    """

    bucket: str
    region: str
    prefix: str = ""
    endpoint_url: str | None = None
    addressing: Addressing = "path"
    allow_plain_http: bool = False
    credentials: ObjectStoreCredentials | None = None
    ca_bundle: Path | None = None
