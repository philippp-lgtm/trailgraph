"""The copies of a move of a graph's stored files to its other location (object storage O7).

``graph-admin files move`` copies every blob a move covers to the location the manifest
does not name, then switches the manifest. A copy pass here:

- covers every hash a tx names and every blob stored at the source (orphans too, so an
  add between its promote and its commit, or an old misplaced copy, is not left behind),
  in hash order; ``.tmp-`` staging files are never blobs;
- lists both locations once and copies what the target lacks or holds with another size
  (``BlobBackend.receive``: verified, durable; a present copy with the same size is
  trusted, its name being its hash); a referenced hash a listing lacks is confirmed by a
  ``HEAD`` or ``stat`` first, since object store listings may lag;
- refuses (``backend.corrupt``) when a referenced blob is in neither location: such a
  graph has lost a file, which ``files verify`` names, and is not moved;
- skips an orphan that vanished while the pass ran (``files gc`` removed it);
- asks ``stop`` before every copy after the first, so a pass makes progress however
  short its time, and fires ``blob.after_copy`` after each copy;
- never removes anything: old copies stay until ``files gc`` judges them misplaced.

Passes run without the graph lock and are idempotent; the switch runs one more pass under
the lock, so whatever a pass missed (a file added meanwhile, a copy a concurrent gc
removed) is copied before the manifest names the target.
"""

from __future__ import annotations

import sys
from collections.abc import Set

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ports.storage import BlobCopies, CopyBudget
from graph_cli.kernel.records import BlobInfo
from graph_cli.storage.common.faults import CrashPoint, FaultHook, fire
from graph_cli.storage.files.blob_backend import BlobBackend
from graph_cli.storage.files.store import FileStore

_SHOWN = 5  # missing hashes a refusal names


def lock_free_referenced(store: FileStore) -> frozenset[str]:
    """The hashes the committed txs name, read without the lock (a pass)."""
    return frozenset(h for tx in store.changes_since(0, limit=sys.maxsize) for h in tx.blobs)


def copy_pass(
    source: BlobBackend,
    target: BlobBackend | None,
    referenced: Set[str],
    *,
    stop: CopyBudget | None,
    dry_run: bool,
    faults: FaultHook | None,
) -> BlobCopies:
    """One pass from ``source`` (the location the manifest names) to ``target``; ``None``
    stands for an object store without a marker yet, which holds nothing of the graph (dry
    runs only)."""
    if target is not None and target.location == source.location:
        raise GraphCliError("internal.error", "a move needs two locations")
    held = {blob.hash: blob.size for blob in source.listing()}
    there = {blob.hash: blob.size for blob in target.listing()} if target is not None else {}
    covered: list[BlobInfo] = []
    todo: list[BlobInfo] = []
    missing: list[str] = []
    for digest in sorted(held.keys() | referenced):
        size = held.get(digest)
        if size is None:  # a referenced hash the listing lacks: listings may lag
            found = source.info(digest)
            size = found.size if found is not None else None
        if size is not None:
            blob = BlobInfo(digest, size)
            covered.append(blob)
            if there.get(digest) != size:
                todo.append(blob)
            continue
        size = there.get(digest)
        if size is None and target is not None:
            found = target.info(digest)
            size = found.size if found is not None else None
        if size is None:
            missing.append(digest)
        else:
            covered.append(BlobInfo(digest, size))
    if missing:
        shown = ", ".join(missing[:_SHOWN]) + (", ..." if len(missing) > _SHOWN else "")
        raise GraphCliError(
            "backend.corrupt",
            f"{len(missing)} stored file(s) a transaction names are in neither location"
            f" ({shown}); the graph is not moved: run graph-admin files verify",
            details={"missing": len(missing), "blobs": missing[:_SHOWN]},
        )
    if dry_run or target is None:
        return BlobCopies(
            source.location,
            target.location if target is not None else "s3",
            tuple(covered),
            (),
            tuple(todo),
        )
    copied: list[BlobInfo] = []
    gone: set[str] = set()
    done = 0
    for blob in todo:
        if done and stop is not None and stop():
            break
        done += 1
        try:
            target.receive(blob, source)
        except GraphCliError as exc:
            if (
                exc.code != "backend.corrupt"
                or blob.hash in referenced
                or source.info(blob.hash) is not None
            ):
                raise
            gone.add(blob.hash)  # an orphan files gc removed meanwhile
            continue
        copied.append(blob)
        try:
            fire(faults, CrashPoint.BLOB_AFTER_COPY)
        except OSError as exc:
            raise GraphCliError(
                "backend.unavailable", f"cannot copy the stored files: {exc.strerror or exc}"
            ) from exc
    remaining = tuple(todo[done:])
    return BlobCopies(
        source.location,
        target.location,
        tuple(blob for blob in covered if blob.hash not in gone),
        tuple(copied),
        remaining,
    )
