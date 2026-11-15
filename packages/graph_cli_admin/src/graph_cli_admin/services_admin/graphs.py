"""Graph lifecycle: create (W.f), list and show (contracts B1.11, B5.2, B12.8).

``create`` authorizes ``maint.graph_create`` on the installation, then
under the registry lock checks the name against every graph (archived ones
too, B1.11) and hands storage the manifest, the genesis event and the first
release; storage stages them, calls back for the installation event with
the anchor, and publishes the graph with one rename.

With ``[files] store = "s3"`` a new graph keeps its stored files in the object store
(object storage O7, "genesis means local"): the graph is created as above, local, and
the same command then switches it, as a ``maint.graph_configure`` operation in the W.e
shape (an intent with ``change.blob_store{from: local, to: s3, store_id, blobs: 0,
bytes: 0}``, the manifest replaced by one rename as format 2, the installation anchor).
Everything that can fail on the object store's side is done before the graph exists:
the settings and credentials are checked, ``maint.graph_configure`` on the new graph is
authorized, and the store marker is read or created (``ensure_store_marker``; creating
it is part of this audited step). Should the switch still fail (the graph lock, the
disk), the graph stays local, holding no file, and the command fails naming the graph
and its location.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from graph_cli.audit import events
from graph_cli.kernel.authz import Decision
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.clock import format_timestamp
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GRAPH_NAME_RE, GraphId, TermRef
from graph_cli.kernel.langtag import normalize_language_tag
from graph_cli.kernel.ports.audit import AuditReceipt
from graph_cli.kernel.ports.catalog import GraphEntry, ManifestData, StoreInfo
from graph_cli.kernel.records import RequestMeta
from graph_cli.ontology_layer.diff import Renames
from graph_cli.ontology_layer.migration import Retype
from graph_cli.storage.files.layout import GraphLayout
from graph_cli.storage.files.releases import release_hashes
from graph_cli.storage.files.s3_blobs import missing_credentials
from graph_cli.storage.files.store_marker import ensure_store_marker
from graph_cli.storage.s3.client import ObjectStoreClient
from graph_cli.storage.s3.settings import ObjectStoreSettings
from graph_cli_admin.services_admin.context import AdminContext
from graph_cli_admin.services_admin.ontology import Compiled, compile_source, release_files

STORE = StoreInfo("files", 1)


def create(
    ctx: AdminContext,
    *,
    name: str,
    source: Path,
    module: str | None,
    title: str | None,
    language: str | None,
) -> ManifestData:
    """``language`` is stored in canonical case and fixed until P2 ``graph configure``."""
    if not GRAPH_NAME_RE.match(name):
        raise GraphCliError(
            "usage.invalid_argument",
            f"graph name {name!r} must match {GRAPH_NAME_RE.pattern}",
            details={"argument": "name"},
        )
    if language is not None:
        canonical = normalize_language_tag(language)
        if canonical is None:
            raise GraphCliError(
                "usage.invalid_argument",
                f"language {language!r} is not a well-formed BCP 47 tag (e.g. en, de-CH)",
                details={"argument": "language"},
            )
        language = canonical
    compiled = compile_source(source, module)
    pin = compiled.pin
    meta = RequestMeta.of(
        "graph create",
        {"name": name, "title": title, "language": language, "ontology": pin.to_json()},
    )
    decisions = ctx.authorize([("maint.graph_create", "installation")], request=meta)
    object_store = _object_store(ctx)
    now = ctx.clock.now()
    entry = GraphEntry(
        GraphId(ctx.ids.new_id()),
        name,
        "active",
        pin,
        alias_prefixes(compiled),
        (),
        title,
        language,
    )
    configure: tuple[Decision, ...] = ()
    store_id: str | None = None
    if object_store is not None:
        configure = ctx.authorize(
            [("maint.graph_configure", f"graph:{entry.id}")], request=meta, graph=entry.id
        )
        client, settings = object_store
        store_id = ensure_store_marker(client, settings, clock=ctx.clock, ids=ctx.ids)
    manifest = ManifestData(entry, now, STORE)
    genesis = events.graph_genesis(
        actor=ctx.actor,
        decisions=decisions,
        request=meta,
        ts=now,
        event_id=ctx.event_id(),
        env=ctx.env,
        graph=entry,
    )

    def announce(receipt: AuditReceipt) -> AuditReceipt:
        event = events.maintenance(
            actor=ctx.actor,
            decisions=decisions,
            request=meta,
            ts=now,
            event_id=ctx.event_id(),
            env=ctx.env,
            graph=entry,
            anchors=[receipt],
        )
        return ctx.audit.append_installation(event)

    files = release_files(compiled, previous=None, release_id=ctx.ids.new_id(), released_at=now)
    with ctx.maintenance.registry(timeout_s=ctx.lock_timeout_s) as registry:
        taken = [m.entry for m in registry.manifests() if m.entry.name == name]
        if taken:
            raise GraphCliError(
                "admin.graph_name_taken",
                f"a graph named {name!r} exists ({taken[0].state})",
                details={"name": name, "id": str(taken[0].id)},
            )
        registry.create_graph(
            manifest, genesis, version=pin.version, files=files, announce=announce
        )
    if store_id is None:
        return manifest
    try:
        return _into_object_store(ctx, entry, configure, meta, store_id)
    except GraphCliError as exc:
        raise GraphCliError(
            exc.code,
            f"graph {name} was created, but it keeps its stored files in the data root:"
            f" switching it to the object store failed ({exc.message}). It holds no file"
            " yet; move it to the object store with graph-admin files move once the cause"
            " is fixed",
            details={**exc.details, "graph_id": str(entry.id), "blob_location": "local"},
        ) from exc


def _object_store(ctx: AdminContext) -> tuple[ObjectStoreClient, ObjectStoreSettings] | None:
    """The object store new graphs go to (``[files] store = "s3"``), checked before the
    graph is created; ``None`` for the data root."""
    if ctx.config.files_store != "s3":
        return None
    settings = ctx.catalog.object_store
    if settings is None:  # catalog.object_store stops every command first; a guard
        raise ctx.catalog.object_store_problem or GraphCliError(
            "config.invalid",
            '[files] store = "s3" needs [files.s3]',
            details={"key": "files.s3.bucket"},
        )
    missing = missing_credentials(settings)
    if missing:
        raise GraphCliError(
            "backend.unavailable",
            '[files] store = "s3" puts new graphs into the object store, but its credentials'
            f" are not set ({', '.join(missing)})",
            details={"missing": list(missing)},
        )
    return ObjectStoreClient(settings), settings


def _into_object_store(
    ctx: AdminContext,
    entry: GraphEntry,
    decisions: tuple[Decision, ...],
    meta: RequestMeta,
    store_id: str,
) -> ManifestData:
    """Switch the new, empty graph to the object store ``store_id`` (O7) in the W.e shape:
    intent, manifest replaced by one rename, anchor."""
    with ctx.maintenance.maintenance_session(entry.id, timeout_s=ctx.lock_timeout_s) as session:
        current = session.manifest()
        if current.store.blob_location != "local":
            raise GraphCliError("internal.error", "the new graph is not local")
        change = events.blob_store_change("local", "s3", store_id, blobs=0, size=0)
        receipt = ctx.intent(session, current.entry, decisions, meta, change)
        switched = replace(current, store=replace(STORE, blob_location="s3", store_id=store_id))
        session.replace_manifest(switched, receipt)
        ctx.anchor(current.entry, decisions, meta, receipt)
    return switched


def alias_prefixes(compiled: Compiled) -> dict[TermRef, str]:
    """Seeded from the IR ``display_prefix`` hints of concrete classes (B1 open point)."""
    return {
        TermRef(curie): cls.display_prefix
        for curie, cls in sorted(compiled.model.classes.items())
        if cls.display_prefix is not None and not cls.abstract
    }


@dataclass(frozen=True, slots=True)
class PrefixChanges:
    """What a release does to the manifest's alias prefixes.

    ``merged`` is the new map. ``added`` holds the prefixes given to classes that had none
    (seeds from ``display_prefix``); ``moved`` the prefixes that follow a class to its new
    CURIE (new CURIE to ``{"from": old CURIE, "prefix": prefix}``), where the old CURIE's entry
    is replaced. ``skipped`` are seeds another class already uses."""

    merged: dict[TermRef, str]
    skipped: dict[TermRef, str]
    added: dict[TermRef, str]
    moved: dict[TermRef, dict[str, str]]

    def to_json(self) -> dict[str, JsonValue]:
        """The ``change.alias_prefixes`` shape of the pin intent."""
        return {
            "added": {str(curie): prefix for curie, prefix in sorted(self.added.items())},
            "moved": {str(curie): dict(move) for curie, move in sorted(self.moved.items())},
        }


def merged_alias_prefixes(
    current: Mapping[TermRef, str],
    compiled: Compiled,
    *,
    renames: Renames,
    retypes: Sequence[Retype] = (),
) -> PrefixChanges:
    """The manifest's prefixes plus those of classes a release adds, and the seeds left out.

    A prefix stays with its entities and is never freed. A class renamed (``renames``, local
    or CURIE ``renamed_from``) takes the prefix of its old CURIE and **replaces** that entry,
    so the old CURIE no longer holds it. So does the ``to`` class of a ``retype`` whose
    ``from`` class is gone from the new model, unless ``to`` holds a prefix or declares another
    ``display_prefix``; then ``to`` keeps or seeds its own and the moved entities keep their
    old aliases. A class removed without a move keeps its entry (the prefix stays reserved),
    and a seed that another class already uses is left out (the class gets no aliases)."""
    classes = compiled.model.classes
    merged = dict(current)
    moved: dict[TermRef, dict[str, str]] = {}

    def move(old: TermRef, new: TermRef) -> None:
        prefix = merged.pop(old)
        merged[new] = prefix
        moved[new] = {"from": str(old), "prefix": prefix}

    sources = {n: o for o, n in renames.classes.items()}
    for name, cls in sorted(classes.items()):
        old = TermRef(sources.get(name, ""))
        if not cls.abstract and TermRef(name) not in merged and old in merged:
            move(old, TermRef(name))
    for step in retypes:
        old, new = TermRef(step.from_), TermRef(step.to)
        target = classes.get(step.to)
        if step.from_ in classes or old not in merged or target is None or target.abstract:
            continue
        if new in merged or target.display_prefix not in (None, merged[old]):
            continue
        move(old, new)
    used = set(merged.values())
    skipped: dict[TermRef, str] = {}
    added: dict[TermRef, str] = {}
    for name, cls in sorted(classes.items()):
        curie = TermRef(name)
        prefix = cls.display_prefix
        if cls.abstract or curie in merged or prefix is None:
            continue
        if prefix in used:
            skipped[curie] = prefix
            continue
        merged[curie] = added[curie] = prefix
        used.add(prefix)
    return PrefixChanges(merged, skipped, added, moved)


def describe(manifest: ManifestData) -> dict[str, JsonValue]:
    entry = manifest.entry
    return {
        "id": str(entry.id),
        "name": entry.name,
        "title": entry.title,
        "language": entry.language,
        "state": entry.state,
        "created_at": format_timestamp(manifest.created_at),
        "ontology": entry.pin.to_json(),
        "alias_prefixes": {str(curie): prefix for curie, prefix in entry.alias_prefixes.items()},
        "extensions": list(entry.extensions),
        "store": {
            "backend": manifest.store.backend,
            "format_version": manifest.store.format_version,
            "blob_location": manifest.store.blob_location,
            "store_id": manifest.store.store_id,
        },
    }


def envelope_graph(entry: GraphEntry, seq: int | None = None) -> dict[str, JsonValue]:
    """The B9 ``graph`` key of an envelope."""
    return {
        "id": str(entry.id),
        "name": entry.name,
        "state": entry.state,
        "ontology": {"bundle": entry.pin.bundle, "version": entry.pin.version},
        "seq": seq,
    }


def list_graphs(ctx: AdminContext, *, include_archived: bool) -> list[JsonValue]:
    return [
        {
            "id": str(m.entry.id),
            "name": m.entry.name,
            "title": m.entry.title,
            "language": m.entry.language,
            "state": m.entry.state,
            "ontology": {"bundle": m.entry.pin.bundle, "version": m.entry.pin.version},
        }
        for m in ctx.manifests(include_archived=include_archived)
    ]


def show(ctx: AdminContext, ref: str) -> tuple[GraphEntry, dict[str, JsonValue]]:
    entry = ctx.resolve(ref, "graph show")
    ctx.authorize(
        [("maint.graph_configure", f"graph:{entry.id}")],
        request=RequestMeta.of("graph show", {"graph": ref}),
        graph=entry.id,
    )
    manifest = ctx.catalog.manifest(entry.id)
    head = ctx.catalog.file_store(entry.id).head()
    layout = GraphLayout(ctx.catalog.graphs_dir / str(entry.id))
    data = describe(manifest)
    data["seq"] = head.seq
    data["releases"] = sorted(release_hashes(layout))
    skill = ctx.catalog.read_skill(entry.id)
    data["skill"] = skill.summary() if skill is not None else None
    published = ctx.catalog.read_functions(entry.id)
    data["functions"] = (
        {"count": published.count, "hash": published.hash} if published is not None else None
    )
    actions = ctx.catalog.read_actions(entry.id)
    data["actions"] = (
        {"count": actions.count, "hash": actions.hash} if actions is not None else None
    )
    return entry, data
