"""``graph-admin files store check``: can this installation use its object store? (object
storage O3, O5, O8)

It checks the configured store, whether or not any graph keeps its files there, and
answers in words what is wrong; messages and details name settings keys, environment
variables, the bucket, the endpoint host, HTTP statuses and S3 error codes, never a
credential (the client's redaction, O2). Authorized like ``files verify``
(``maint.audit_verify``, on the installation); it writes no audit event: the only object
it writes is a probe below ``<prefix>graph_cli_probe/``, which it removes again.

The checks, in order (each ``ok``, ``warning``, ``error`` or ``skipped``):

- ``settings``: ``[files.s3]`` and the environment give a usable store (no store
  configured is no failure: graphs then keep their files in the data root);
- ``credentials``: both key variables are set;
- ``marker``: the store marker ``graph_cli_store.json`` (its id, and which graphs of this
  installation keep their files in it); a missing marker is a warning unless a graph names
  a store, a marker with another id than a graph names is an error;
- ``list``: ``ListObjectsV2`` (verify, gc and the commit check list the graph's prefix);
- ``write``, ``read``, ``delete``: a probe ``PUT``, ``HEAD`` plus ``GET``, ``DELETE``;
- ``missing_key``: a ``HEAD`` of a key that does not exist answers 404; a 403 means the
  key lacks ``ListBucket``, and graph_cli could not tell a missing file from a refused one;
- ``clock``: the store's ``Date`` against this machine's clock (signed requests fail
  beyond 15 minutes; grace periods use the store's clock);
- ``addressing``: no answer redirected to another endpoint, region or addressing style.

When the store cannot be reached at all, or refuses the credentials, the bucket or the
request time, the remaining requests are skipped. Any ``error`` fails the command with
``admin.store_check_failed``, whose details carry every check.
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal, TypeVar

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ports.catalog import ManifestData
from graph_cli.kernel.records import RequestMeta
from graph_cli.storage.files.s3_blobs import missing_credentials
from graph_cli.storage.files.store_marker import MARKER_KEY, StoreMarker, read_store_marker
from graph_cli.storage.s3.client import ObjectStoreClient
from graph_cli.storage.s3.errors import CREDENTIAL_CODES
from graph_cli.storage.s3.settings import ObjectStoreSettings
from graph_cli_admin.services_admin.context import AdminContext

PROBE_PREFIX: Final = "graph_cli_probe/"
PROBE_BODY: Final = b"graph_cli object store check\n"
SKEW_OK: Final = timedelta(minutes=1)
SKEW_LIMIT: Final = timedelta(minutes=15)  # S3 refuses signed requests beyond it
_REDIRECT_CODES: Final = frozenset({"PermanentRedirect", "TemporaryRedirect"})
# answers after which no further request can tell more
_FATAL_CODES: Final = CREDENTIAL_CODES | {"NoSuchBucket", "RequestTimeTooSkewed"}

Status = Literal["ok", "warning", "error", "skipped"]
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: Status
    message: str

    def to_json(self) -> dict[str, JsonValue]:
        return {"name": self.name, "status": self.status, "message": self.message}


class _Run:
    """The checks of one run, the errors the store answered and its ``Date`` readings."""

    def __init__(
        self, ctx: AdminContext, client: ObjectStoreClient, settings: ObjectStoreSettings
    ) -> None:
        self.ctx = ctx
        self.client = client
        self.prefix = settings.prefix
        self.checks: list[Check] = []
        self.errors: list[GraphCliError] = []
        self.skews: list[timedelta] = []
        self.fatal: str | None = None
        self.answered = False

    def add(self, name: str, status: Status, message: str) -> None:
        self.checks.append(Check(name, status, message))

    def call(self, name: str, request: Callable[[], T]) -> T | GraphCliError:
        """One request to the store; an error is recorded (and returned), and one after
        which nothing more can be learned skips the later requests."""
        try:
            result = request()
        except GraphCliError as exc:
            self.errors.append(exc)
            status = exc.details.get("status")
            s3_code = exc.details.get("s3_code")
            if status is None or s3_code in _FATAL_CODES:
                self.fatal = f"skipped: the {name} check failed, nothing more can be learned"
            else:
                self.answered = True
            return exc
        self.answered = True
        return result

    def date(self, value: datetime | None) -> None:
        """The store's clock as an answer's ``Date`` shows it, against this machine's (in
        whole seconds, as ``Date`` has them)."""
        if value is not None:
            self.skews.append(value - self.ctx.clock.now().replace(microsecond=0))


def check(ctx: AdminContext) -> dict[str, JsonValue]:
    meta = RequestMeta.of("files store check", {})
    ctx.authorize([("maint.audit_verify", "installation")], request=meta)
    maintenance = ctx.maintenance
    settings = maintenance.object_store
    problem = maintenance.object_store_problem
    data: dict[str, JsonValue] = {
        "configured": False,
        "files_store": ctx.config.files_store,
        "store": None,
        "store_id": None,
        "graphs": [],
        "skew_s": None,
        "checks": [],
    }
    if settings is None:
        if problem is None or _no_bucket(ctx, problem):
            message = (
                "no object store is configured ([files.s3] bucket and GRAPH_CLI_S3_BUCKET are"
                " not set): every graph keeps its stored files in the data root"
            )
            data["checks"] = [Check("settings", "skipped", message).to_json()]
            return data
        data["configured"] = True
        data["checks"] = [Check("settings", "error", problem.message).to_json()]
        return _finish(data)
    data["configured"] = True
    client = maintenance.object_store_client()
    assert client is not None
    data["store"] = _describe(settings, client)
    run = _Run(ctx, client, settings)
    store = data["store"]
    assert isinstance(store, dict)
    run.add(
        "settings",
        "ok",
        f"bucket {settings.bucket} at {store['endpoint_host']} (region {settings.region},"
        f" {_ADDRESSING[settings.addressing]} addressing,"
        f" prefix {settings.prefix or '(none)'})",
    )
    missing = missing_credentials(settings)
    if missing:
        run.add(
            "credentials",
            "error",
            f"the object store credentials are not set ({', '.join(missing)} in the environment)",
        )
        run.fatal = "skipped: no credentials"
    else:
        run.add("credentials", "ok", "the access key and the secret are set")
    manifests = ctx.manifests(include_archived=True, skip_unreadable=True)
    marker = _marker(run, manifests)
    if marker is not None:
        data["store_id"] = marker.store_id
        data["graphs"] = [
            {"id": str(m.entry.id), "name": m.entry.name}
            for m in manifests
            if m.store.blob_location == "s3" and m.store.store_id == marker.store_id
        ]
    _list(run)
    _probe(run)
    _missing_key(run)
    _clock(run, data)
    _addressing(run)
    data["checks"] = [c.to_json() for c in run.checks]
    return _finish(data)


def _finish(data: dict[str, JsonValue]) -> dict[str, JsonValue]:
    checks = data["checks"]
    assert isinstance(checks, list)
    failed = [c for c in checks if isinstance(c, dict) and c.get("status") == "error"]
    if failed:
        said = "; ".join(f"{c.get('name')}: {c.get('message')}" for c in failed)
        raise GraphCliError(
            "admin.store_check_failed", f"the object store check failed: {said}", details=data
        )
    return data


def _no_bucket(ctx: AdminContext, problem: GraphCliError) -> bool:
    """The settings are absent because no bucket is set at all (neither the file nor the
    environment names one): no object store is configured."""
    return problem.details.get("key") == "files.s3.bucket" and not ctx.config.files_s3.bucket


_ADDRESSING: Final = {"path": "path-style", "virtual": "virtual-hosted"}


def _describe(settings: ObjectStoreSettings, client: ObjectStoreClient) -> dict[str, JsonValue]:
    try:
        host = client.endpoint_host()
    except GraphCliError:
        host = "unknown"
    return {
        "bucket": settings.bucket,
        "prefix": settings.prefix,
        "region": settings.region,
        "endpoint_host": host,
        "addressing": settings.addressing,
    }


def _skip(run: _Run, *names: str) -> bool:
    if run.fatal is None:
        return False
    for name in names:
        run.add(name, "skipped", run.fatal)
    return True


def _marker(run: _Run, manifests: list[ManifestData]) -> StoreMarker | None:
    if _skip(run, "marker"):
        return None
    found = run.call("marker", lambda: read_store_marker(run.client))
    if isinstance(found, GraphCliError):
        run.add("marker", "error", found.message)
        return None
    named = {
        m.entry.name: m.store.store_id or "" for m in manifests if m.store.blob_location == "s3"
    }
    bucket = run.client.bucket
    if found is None:
        if named:
            run.add(
                "marker",
                "error",
                f"bucket {bucket} has no graph_cli store marker ({MARKER_KEY}), but graphs of"
                f" this installation keep their stored files in an object store"
                f" ({_graph_names(named)}): this is not their bucket; check the bucket, the"
                " prefix and the endpoint",
            )
        else:
            run.add(
                "marker",
                "warning",
                f"bucket {bucket} has no graph_cli store marker ({MARKER_KEY}) yet; graph"
                ' create with [files] store = "s3" or a move of a graph\'s files creates it',
            )
        return None
    foreign = {name: store_id for name, store_id in named.items() if store_id != found.store_id}
    if foreign:
        run.add(
            "marker",
            "error",
            f"bucket {bucket} is object store {found.store_id}, but graphs of this"
            f" installation keep their stored files in another one ({_graph_names(foreign)});"
            " check the bucket, the prefix and the endpoint",
        )
        return found
    count = len(named)
    run.add(
        "marker",
        "ok",
        f"object store {found.store_id} (marker created {found.created_at}); {count}"
        f" graph{'' if count == 1 else 's'} of this installation keep"
        f"{'s' if count == 1 else ''} their stored files here",
    )
    return found


def _graph_names(named: dict[str, str]) -> str:
    return ", ".join(f"{name}: {store_id}" for name, store_id in sorted(named.items()))


def _list(run: _Run) -> None:
    if _skip(run, "list"):
        return
    page = run.call("list", lambda: next(run.client.list_pages(PROBE_PREFIX, max_keys=1), None))
    if isinstance(page, GraphCliError):
        hint = ""
        if page.details.get("status") == 403:
            hint = "; the key lacks ListBucket (verify, gc and the commit check list objects)"
        run.add("list", "error", page.message + hint)
        return
    if page is not None:
        run.date(page.response_date)
    run.add("list", "ok", "listing objects (ListObjectsV2) works")


def _probe(run: _Run) -> None:
    """``PUT``, ``HEAD`` and ``GET``, ``DELETE`` of one probe object."""
    if _skip(run, "write", "read", "delete"):
        return
    key = f"{PROBE_PREFIX}{run.ctx.ids.new_id()}"
    digest = hashlib.sha256(PROBE_BODY).hexdigest()
    written = run.call(
        "write",
        lambda: run.client.put_object(key, io.BytesIO(PROBE_BODY), len(PROBE_BODY), digest),
    )
    if isinstance(written, GraphCliError):
        run.add("write", "error", written.message)
        if not _skip(run, "read", "delete"):
            run.add("read", "skipped", "skipped: nothing was written")
            run.add("delete", "skipped", "skipped: nothing was written")
        return
    run.add("write", "ok", f"writing objects (PutObject) works (probe {key})")
    _read(run, key)
    if _skip(run, "delete"):
        return
    _delete(run, key)


def _read(run: _Run, key: str) -> None:
    if _skip(run, "read"):
        return

    def read() -> bytes | None:
        info = run.client.head_object(key)
        if info is None:
            return None
        run.date(info.response_date)
        with run.client.get_object(key) as body:
            return b"".join(body)

    found = run.call("read", read)
    if isinstance(found, GraphCliError):
        run.add("read", "error", found.message)
    elif found is None:
        run.add(
            "read",
            "error",
            "the probe object written a moment ago is not found; the store does not read"
            " its own writes back",
        )
    elif found != PROBE_BODY:
        run.add("read", "error", "the probe object reads back with other bytes")
    else:
        run.add("read", "ok", "reading objects (HeadObject, GetObject) works")


def _delete(run: _Run, key: str) -> None:
    gone = run.call("delete", lambda: run.client.delete_object(key))
    if isinstance(gone, GraphCliError):
        run.add(
            "delete",
            "error",
            f"{gone.message}; files gc cannot remove files, and the probe object"
            f" {run.prefix}{key} stays: remove it by hand",
        )
    else:
        run.add("delete", "ok", "deleting objects (DeleteObject) works; the probe is removed")


def _missing_key(run: _Run) -> None:
    if _skip(run, "missing_key"):
        return
    key = f"{PROBE_PREFIX}{run.ctx.ids.new_id()}-absent"
    found = run.call("missing_key", lambda: run.client.head_object(key))
    if isinstance(found, GraphCliError):
        if found.details.get("status") == 403:
            run.add(
                "missing_key",
                "error",
                "a missing object answers 403 instead of 404: the key lacks the ListBucket"
                " permission on the bucket, so a missing file and a refused request look"
                " alike; grant ListBucket",
            )
        else:
            run.add("missing_key", "error", found.message)
    elif found is not None:
        run.add("missing_key", "error", f"an object that cannot exist is found ({key})")
    else:
        run.add("missing_key", "ok", "a missing object answers 404 (ListBucket is granted)")


def _clock(run: _Run, data: dict[str, JsonValue]) -> None:
    if not run.skews:
        skewed = [e for e in run.errors if e.details.get("s3_code") == "RequestTimeTooSkewed"]
        if skewed:
            run.add("clock", "error", skewed[0].message)
        else:
            run.add("clock", "skipped", "skipped: no answer carried the store's Date")
        return
    skew = min(run.skews, key=abs)
    seconds = round(skew.total_seconds())
    data["skew_s"] = seconds
    ahead = "ahead of" if seconds >= 0 else "behind"
    said = f"the store's clock is {abs(seconds)} s {ahead} this machine's"
    if abs(skew) <= SKEW_OK:
        run.add("clock", "ok", said)
    elif abs(skew) <= SKEW_LIMIT:
        run.add(
            "clock",
            "warning",
            f"{said}; signed requests fail beyond 15 minutes: correct this machine's clock"
            " (grace periods follow the store's clock and are not affected)",
        )
    else:
        run.add(
            "clock",
            "error",
            f"{said}; the store refuses signed requests beyond 15 minutes: correct this"
            " machine's clock",
        )


def _redirect(error: GraphCliError) -> bool:
    status = error.details.get("status")
    return isinstance(status, int) and 300 <= status < 400


def _addressing(run: _Run) -> None:
    redirected = [
        e for e in run.errors if e.details.get("s3_code") in _REDIRECT_CODES or _redirect(e)
    ]
    if redirected:
        run.add("addressing", "error", redirected[0].message)
    elif run.answered:
        run.add("addressing", "ok", "the store answers at this endpoint without a redirect")
    else:
        run.add("addressing", "skipped", "skipped: the store did not answer")
