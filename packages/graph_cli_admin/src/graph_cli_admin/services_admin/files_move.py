"""``graph-admin files move``: a graph's stored files to the other location (object storage O7).

A move copies, then switches:

- **Copy passes** (``FileStoreMaintenance.copy_blobs``) run without the graph lock and copy
  every blob the move covers (each hash a tx names, and every blob stored at the source,
  orphans too) that the target lacks, verified (an upload signed with the hash and then
  ``HEAD``; a local copy rehashed while written, synced, mode 0o444). They are idempotent
  and repeatable; an interrupted pass only leaves extra copies.
- **The switch** takes the graph lock, copies what the passes missed (files added since,
  copies a concurrent ``files gc`` removed), then records the change in the W.e shape of
  ``graph create``'s switch (``switch_blob_store``): a ``maint.graph_configure`` intent with
  ``change.blob_store{from, to, store_id, blobs, bytes}``, the manifest replaced by one
  rename (format 1 <-> 2) through ``replace_manifest`` (so the session rebinds), and the
  installation anchor. ``store_id`` is the target store for ``s3``; for ``local`` it is
  the store the graph leaves, so ``files gc`` may clean the bucket afterwards.
- **Never deletes.** The old copies stay until ``files gc`` removes them as misplaced
  (hash-confirmed against the new location).

A file add racing the move commits where the manifest names: its write session binds the
location under the lock, and the publish uploads from (or renames) its local staging file
when the bound location lacks the blob.

Without ``--step`` the passes run back to back in this process until nothing remains, then
the switch, graph by graph. ``--step pass`` runs one pass and ``--step switch`` only the
switch, each with ``PASS_BUDGET_S`` of copying: the remote admin mode sends these short
requests (``maintenance_cli.remote_move``), so each fits the client's timeout. A switch
whose copies under the lock run out of time switches nothing and answers ``copying``.

Refused before any copy: an object store that is not configured, lacks credentials or
whose marker does not match (``--to s3`` creates the marker when the bucket has none: the
move is one of the two audited steps that may), a graph in the middle of a data migration
(``admin.migration_incomplete``). A graph already at the target is answered as such with no
audit event. A dry run copies and writes nothing. Authorized like ``graph create``'s switch:
``maint.graph_configure`` on the graph, for passes and dry runs too (a pass writes into the
bucket). ``--all`` takes every graph of the installation, archived ones too; the first
failure stops the command with what was done so far.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Final

from graph_cli.audit import events
from graph_cli.kernel.authz import Decision
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.migration_marker import MigrationMarker, marker_committed
from graph_cli.kernel.ports.catalog import BlobLocation, GraphEntry, ManifestData
from graph_cli.kernel.ports.storage import BlobCopies, CopyBudget, MaintenanceSession, Snapshot
from graph_cli.kernel.records import BlobInfo, RequestMeta
from graph_cli.storage.files.s3_blobs import ClientFactory, missing_credentials
from graph_cli.storage.files.store_marker import ensure_store_marker, read_store_marker
from graph_cli.storage.s3.client import ObjectStoreClient
from graph_cli.storage.s3.settings import ObjectStoreSettings
from graph_cli_admin.services_admin.context import AdminContext

# Seconds of copying one request with --step may spend (the remote admin mode: each request
# fits the client's 60 s timeout). Tests lower it.
PASS_BUDGET_S = 30.0
LOCATIONS: Final = ("local", "s3")
STEPS: Final = ("pass", "switch")
COMMAND: Final = "files move"


@dataclass(frozen=True, slots=True)
class MoveOutcome:
    data: dict[str, JsonValue]
    error: GraphCliError | None = None


def object_store_client(
    settings: ObjectStoreSettings | None,
    problem: GraphCliError | None,
    client_factory: ClientFactory | None,
    *,
    what: str,
    purpose: str,
) -> tuple[ObjectStoreClient, ObjectStoreSettings]:
    """A client of the configured object store for a step that establishes a location
    (``graph create`` with ``[files] store = "s3"``, ``files move --to s3``), refused
    before anything is written: no settings (``config.invalid``, the reason when known),
    no credentials (``backend.unavailable``)."""
    if settings is None:
        raise problem or GraphCliError(
            "config.invalid", f"{what} needs [files.s3]", details={"key": "files.s3.bucket"}
        )
    missing = missing_credentials(settings)
    if missing:
        raise GraphCliError(
            "backend.unavailable",
            f"{purpose}, but its credentials are not set ({', '.join(missing)})",
            details={"missing": list(missing)},
        )
    return (client_factory or ObjectStoreClient)(settings), settings


def switch_blob_store(
    ctx: AdminContext,
    session: MaintenanceSession,
    current: ManifestData,
    decisions: tuple[Decision, ...],
    meta: RequestMeta,
    *,
    to: BlobLocation,
    store_id: str,
    blobs: Sequence[BlobInfo],
    reason: str | None = None,
) -> ManifestData:
    """Record and apply a switch of the graph's blob location under its lock (W.e): the
    ``change.blob_store`` intent, the manifest replaced by one rename (format 2 naming the
    store for ``s3``, format 1 for ``local``), the installation anchor. ``store_id`` is the
    object store the switch goes to or leaves; ``blobs`` the stored files it covers."""
    source = current.store.blob_location
    change = events.blob_store_change(
        source, to, store_id, blobs=len(blobs), size=sum(blob.size for blob in blobs)
    )
    receipt = ctx.intent(session, current.entry, decisions, meta, change, reason=reason)
    store = replace(current.store, blob_location=to, store_id=store_id if to == "s3" else None)
    switched = replace(current, store=store)
    session.replace_manifest(switched, receipt)
    ctx.anchor(current.entry, decisions, meta, receipt, reason=reason)
    return switched


def move(
    ctx: AdminContext,
    *,
    refs: Sequence[str],
    all_graphs: bool,
    to: str,
    dry_run: bool,
    step: str | None,
    reason: str | None,
) -> MoveOutcome:
    if to not in LOCATIONS:
        raise _usage("--to", f"--to takes {' or '.join(LOCATIONS)}")
    if step is not None and step not in STEPS:
        raise _usage("--step", f"--step takes {' or '.join(STEPS)}")
    if bool(refs) == all_graphs:
        raise _usage("ref", "name the graphs to move, or pass --all for every graph")
    if dry_run and step is not None:
        raise _usage("--step", "a dry run copies nothing; it takes no --step")
    location: BlobLocation = "s3" if to == "s3" else "local"
    entries = _entries(ctx, refs, all_graphs=all_graphs)
    target = _Target(ctx, location, dry_run=dry_run)
    rows: list[JsonValue] = []

    def data() -> dict[str, JsonValue]:
        return {"to": to, "dry_run": dry_run, "step": step, "graphs": list(rows)}

    for entry in entries:
        try:
            rows.append(_move_graph(ctx, entry, target, dry_run=dry_run, step=step, reason=reason))
        except GraphCliError as exc:
            return MoveOutcome(data(), exc)
    return MoveOutcome(data())


def _entries(ctx: AdminContext, refs: Sequence[str], *, all_graphs: bool) -> list[GraphEntry]:
    if all_graphs:
        found = ctx.manifests(include_archived=True)
        return sorted((m.entry for m in found), key=lambda entry: entry.name)
    entries: dict[str, GraphEntry] = {}
    for ref in refs:
        entry = ctx.resolve(ref, COMMAND)
        entries.setdefault(str(entry.id), entry)
    return list(entries.values())


class _Target:
    """Where the move goes, checked once per command and before any copy: the object store's
    settings, credentials and marker (created when absent, except in a dry run)."""

    def __init__(self, ctx: AdminContext, location: BlobLocation, *, dry_run: bool) -> None:
        self.ctx = ctx
        self.location = location
        self.dry_run = dry_run
        self._store_id: str | None = None
        self._checked = False

    def store_id(self, manifest: ManifestData) -> str | None:
        """The object store the switch names: the target's marker id for ``s3`` (``None``
        in a dry run when the bucket has no marker yet), the store left for ``local``."""
        if self.location == "local":
            return manifest.store.store_id
        if not self._checked:
            ctx = self.ctx
            maintenance = ctx.maintenance
            client, settings = object_store_client(
                maintenance.object_store,
                maintenance.object_store_problem,
                maintenance.client_factory,
                what="files move --to s3",
                purpose="files move --to s3 copies the stored files into the object store",
            )
            if self.dry_run:
                marker = read_store_marker(client)
                self._store_id = marker.store_id if marker is not None else None
            else:
                self._store_id = ensure_store_marker(client, settings, clock=ctx.clock, ids=ctx.ids)
            self._checked = True
        return self._store_id


def _move_graph(
    ctx: AdminContext,
    entry: GraphEntry,
    target: _Target,
    *,
    dry_run: bool,
    step: str | None,
    reason: str | None,
) -> dict[str, JsonValue]:
    to = target.location
    meta = RequestMeta.of(
        COMMAND, {"graph": str(entry.id), "to": to, "dry_run": dry_run, "step": step}
    )
    decisions = ctx.authorize(
        [("maint.graph_configure", f"graph:{entry.id}")],
        request=meta,
        graph=entry.id,
        reason=reason,
    )
    manifest = ctx.catalog.manifest(entry.id)
    row = _Row(entry, manifest.store.blob_location)
    if manifest.store.blob_location == to:
        return row.at_target(manifest)
    store = ctx.maintenance.store(entry.id)
    _refuse_migrating(entry, store.migration_marker(), lambda: store.snapshot(), manifest)
    store_id = target.store_id(manifest)
    if dry_run:
        copies = ctx.maintenance.copy_blobs(entry.id, to=to, store_id=store_id, dry_run=True)
        return row.add(copies).render("planned", store_id)
    assert store_id is not None
    budget = _budget(ctx) if step is not None else None
    if step != "switch":
        while True:
            copies = ctx.maintenance.copy_blobs(entry.id, to=to, store_id=store_id, stop=budget)
            row.add(copies, passes=1)
            if step == "pass":
                return row.render("copying", store_id)
            if not copies.remaining:
                break
    with ctx.maintenance.maintenance_session(entry.id, timeout_s=ctx.lock_timeout_s) as session:
        current = session.manifest()  # W.6: re-read under the lock
        if current.store.blob_location == to:  # another move switched it meanwhile
            row.source = to
            return row.at_target(current)
        _refuse_migrating(entry, session.migration_marker(), session.snapshot, current)
        copies = session.copy_blobs(to=to, store_id=store_id, stop=budget)
        row.add(copies)
        if copies.remaining:  # the time ran out under the lock: nothing is switched
            return row.render("copying", store_id)
        switch_blob_store(
            ctx,
            session,
            current,
            decisions,
            meta,
            to=to,
            store_id=store_id,
            blobs=copies.blobs,
            reason=reason,
        )
    return row.render("moved", store_id)


def _budget(ctx: AdminContext) -> CopyBudget:
    """``PASS_BUDGET_S`` of copying from now (the request's pass or switch)."""
    monotonic = ctx.maintenance.monotonic
    deadline = monotonic() + PASS_BUDGET_S
    return lambda: monotonic() >= deadline


def _refuse_migrating(
    entry: GraphEntry,
    marker: MigrationMarker | None,
    snapshot: Callable[[], Snapshot],
    manifest: ManifestData,
) -> None:
    """``admin.migration_incomplete`` while a data migration is committed but not pinned (a
    stale marker, whose tx never committed or whose pin switched, does not count); the pin
    is the manifest's."""
    if marker is None:
        return
    if marker.reached_by(manifest.entry.pin) or not marker_committed(snapshot(), marker):
        return
    raise GraphCliError(
        "admin.migration_incomplete",
        f"graph {entry.name} is in the middle of a data migration ({marker.from_version} ->"
        f" {marker.to_version}); finish it by releasing {marker.to_version} again before"
        " moving its stored files",
        details={
            "graph": entry.name,
            "from_version": marker.from_version,
            "to_version": marker.to_version,
        },
    )


class _Row:
    """One graph's line of the answer, summed over the passes of this command."""

    def __init__(self, entry: GraphEntry, source: BlobLocation) -> None:
        self.entry = entry
        self.source = source
        self.blobs: tuple[BlobInfo, ...] = ()
        self.remaining: tuple[BlobInfo, ...] = ()
        self.copied: list[BlobInfo] = []
        self.passes = 0

    def add(self, copies: BlobCopies, *, passes: int = 0) -> _Row:
        self.blobs = copies.blobs
        self.remaining = copies.remaining
        self.copied += copies.copied
        self.passes += passes
        return self

    def at_target(self, manifest: ManifestData) -> dict[str, JsonValue]:
        self.blobs = self.remaining = ()
        return self.render("at_target", manifest.store.store_id)

    def render(self, status: str, store_id: str | None) -> dict[str, JsonValue]:
        return {
            "graph_id": str(self.entry.id),
            "name": self.entry.name,
            "from": self.source,
            "status": status,
            "store_id": store_id,
            "stored": counts(self.blobs),
            "copied": counts(self.copied),
            "remaining": counts(self.remaining),
            "passes": self.passes,
        }


def counts(blobs: Sequence[BlobInfo]) -> dict[str, JsonValue]:
    return {"blobs": len(blobs), "bytes": sum(blob.size for blob in blobs)}


def _usage(argument: str, message: str) -> GraphCliError:
    return GraphCliError("usage.invalid_argument", message, details={"argument": argument})
