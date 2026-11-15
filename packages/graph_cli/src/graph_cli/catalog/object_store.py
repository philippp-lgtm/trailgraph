"""Object store settings from ``config.toml`` and the environment (``[files] store``,
``[files.s3]``).

``catalog`` builds the immutable ``ObjectStoreSettings`` here and hands it to the
``FileCatalog`` and, through the admin wiring, to ``FileStoreMaintenance``; storage
never reads the environment. The environment wins over the file:
``GRAPH_CLI_S3_BUCKET`` over ``bucket``, ``AWS_ENDPOINT_URL_S3`` then
``AWS_ENDPOINT_URL`` over ``endpoint_url``, ``AWS_REGION`` then ``AWS_DEFAULT_REGION``
over ``region``. Credentials come only from ``AWS_ACCESS_KEY_ID``,
``AWS_SECRET_ACCESS_KEY`` and the optional ``AWS_SESSION_TOKEN``, the CA bundle from
``AWS_CA_BUNDLE``. An empty variable counts as unset.

The endpoint is an ``http(s)`` URL without user info, path, query or fragment;
``http://`` only to a loopback host unless ``[files.s3] allow_plain_http = true``. The
prefix is empty or ends with ``/``.

With ``[files] store = "s3"`` an invalid setting stops the command
(``config.invalid``). Otherwise the settings are built when they are valid, and the
error is kept for a graph whose manifest names an object store, so an installation
that does not use one never meets ``[files.s3]``. Messages name keys and variables,
never their values.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from graph_cli.catalog.config import Config
from graph_cli.catalog.endpoint import parse_endpoint
from graph_cli.kernel.errors import GraphCliError
from graph_cli.storage.s3.settings import ObjectStoreCredentials, ObjectStoreSettings

BUCKET_ENV: Final = "GRAPH_CLI_S3_BUCKET"
ENDPOINT_ENVS: Final = ("AWS_ENDPOINT_URL_S3", "AWS_ENDPOINT_URL")
REGION_ENVS: Final = ("AWS_REGION", "AWS_DEFAULT_REGION")
ACCESS_KEY_ENV: Final = "AWS_ACCESS_KEY_ID"
SECRET_KEY_ENV: Final = "AWS_SECRET_ACCESS_KEY"
SESSION_TOKEN_ENV: Final = "AWS_SESSION_TOKEN"
CA_BUNDLE_ENV: Final = "AWS_CA_BUNDLE"

# S3 bucket names: 3-63 lowercase letters, digits, dots and hyphens, a letter or digit
# at both ends
BUCKET_RE: Final = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
# a region is part of the signature's credential scope: no "/" or white space
REGION_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
# keys stay far below the 1024-byte S3 limit (and stores with lower ones)
MAX_PREFIX_BYTES: Final = 256
_PREFIX_SEGMENT_RE: Final = re.compile(r"^[A-Za-z0-9!_.*'()-]+$")


@dataclass(frozen=True, slots=True)
class ObjectStoreChoice:
    """The settings, or why there are none (``problem``, a ``config.invalid``)."""

    settings: ObjectStoreSettings | None
    problem: GraphCliError | None = None


def object_store(config: Config, env: Mapping[str, str]) -> ObjectStoreChoice:
    """The installation's object store; raises only with ``[files] store = "s3"``."""
    try:
        return ObjectStoreChoice(object_store_settings(config, env))
    except GraphCliError as exc:
        if config.files_store == "s3" or exc.code != "config.invalid":
            raise
        return ObjectStoreChoice(None, exc)


def object_store_settings(config: Config, env: Mapping[str, str]) -> ObjectStoreSettings:
    """``[files.s3]`` with the environment over it, fully validated (``config.invalid``)."""
    table = config.files_s3
    bucket = _Value.pick(config.path, env, (BUCKET_ENV,), "files.s3.bucket", table.bucket)
    if bucket is None:
        raise _file_invalid(config.path, "files.s3.bucket", f"is missing; set it or {BUCKET_ENV}")
    if not BUCKET_RE.match(bucket.text):
        raise bucket.invalid(
            "is not a bucket name (3 to 63 lowercase letters, digits, dots and hyphens)"
        )
    region = _Value.pick(config.path, env, REGION_ENVS, "files.s3.region", table.region)
    if region is None:
        raise _file_invalid(
            config.path, "files.s3.region", f"is missing; set it or {REGION_ENVS[0]}"
        )
    if not REGION_RE.match(region.text):
        raise region.invalid("is not a region name (letters, digits, '_', '.', '-')")
    endpoint = _Value.pick(
        config.path, env, ENDPOINT_ENVS, "files.s3.endpoint_url", table.endpoint_url
    )
    return ObjectStoreSettings(
        bucket=bucket.text,
        region=region.text,
        prefix=_prefix(config.path, table.prefix),
        endpoint_url=_endpoint(endpoint, allow_plain_http=table.allow_plain_http)
        if endpoint is not None
        else None,
        addressing=table.addressing or "path",
        allow_plain_http=table.allow_plain_http,
        credentials=_credentials(env),
        ca_bundle=_ca_bundle(env),
    )


@dataclass(frozen=True, slots=True)
class _Value:
    """A setting and where it came from: a config key or an environment variable."""

    file: Path
    key: str  # files.s3.<key>, or the variable's name when ``variable``
    text: str
    variable: bool = False

    @classmethod
    def pick(
        cls,
        file: Path,
        env: Mapping[str, str],
        variables: Sequence[str],
        key: str,
        written: str | None,
    ) -> _Value | None:
        for variable in variables:
            if env.get(variable):
                return cls(file, variable, env[variable], variable=True)
        return cls(file, key, written) if written else None

    def invalid(self, message: str) -> GraphCliError:
        if self.variable:
            return _env_invalid(self.key, message)
        return _file_invalid(self.file, self.key, message)


def _file_invalid(file: Path, key: str, message: str) -> GraphCliError:
    return GraphCliError(
        "config.invalid", f"{file}: {key} {message}", details={"file": str(file), "key": key}
    )


def _env_invalid(variable: str, message: str) -> GraphCliError:
    return GraphCliError("config.invalid", f"{variable} {message}", details={"variable": variable})


def _endpoint(value: _Value, *, allow_plain_http: bool) -> str:
    """``scheme://host[:port]`` as written, without a trailing slash."""
    found = parse_endpoint(value.text)
    if isinstance(found, str):
        # the parser's own reason may quote the text, which may hold a secret
        raise value.invalid("is not a URL" if found.startswith("is not a URL") else found)
    if found.path:
        raise value.invalid("must not carry a path")
    if found.plain_http and not found.loopback and not allow_plain_http:
        raise value.invalid(
            "is plain http:// to a host that is not loopback: requests and file bytes would"
            " travel in clear; use https:// (or set [files.s3] allow_plain_http = true)"
        )
    parts = urlsplit(value.text)
    return f"{parts.scheme}://{parts.netloc}"


def _prefix(file: Path, written: str | None) -> str:
    """Empty, or ``/``-separated segments ending with ``/``."""
    if not written:
        return ""
    where = _Value(file, "files.s3.prefix", written)
    segments = written.removesuffix("/").split("/")
    if any(s in ("", ".", "..") or not _PREFIX_SEGMENT_RE.match(s) for s in segments):
        raise where.invalid(
            "must be segments of letters, digits and !_.*'()- separated by '/',"
            " without a leading '/', empty, '.' or '..' segments"
        )
    prefix = "/".join(segments) + "/"
    if len(prefix.encode()) > MAX_PREFIX_BYTES:
        raise where.invalid(f"is longer than {MAX_PREFIX_BYTES} bytes")
    return prefix


def _credentials(env: Mapping[str, str]) -> ObjectStoreCredentials | None:
    """Both keys or neither; a session token only with both."""
    key_id = env.get(ACCESS_KEY_ENV) or None
    secret = env.get(SECRET_KEY_ENV) or None
    token = env.get(SESSION_TOKEN_ENV) or None
    if key_id is None and secret is None:
        if token is not None:
            raise _env_invalid(
                SESSION_TOKEN_ENV, f"needs {ACCESS_KEY_ENV} and {SECRET_KEY_ENV} as well"
            )
        return None
    if key_id is None or secret is None:
        missing = ACCESS_KEY_ENV if key_id is None else SECRET_KEY_ENV
        present = SECRET_KEY_ENV if key_id is None else ACCESS_KEY_ENV
        raise _env_invalid(missing, f"is missing while {present} is set")
    return ObjectStoreCredentials(key_id, secret, token)


def _ca_bundle(env: Mapping[str, str]) -> Path | None:
    value = env.get(CA_BUNDLE_ENV) or None
    if value is None:
        return None
    path = Path(value)
    if not path.is_absolute():
        raise _env_invalid(CA_BUNDLE_ENV, "must be an absolute path")
    return path
