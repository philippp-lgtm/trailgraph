"""Graph lifecycle: create (W.f), list and show (contracts B1.11, B5.2, B12.8).

``create`` authorizes ``maint.graph_create`` on the installation, then
under the registry lock checks the name against every graph (archived ones
too, B1.11) and hands storage the manifest, the genesis event and the first
release; storage stages them, calls back for the installation event with
the anchor, and publishes the graph with one rename.
"""

from __future__ import annotations

from pathlib import Path

from graph_cli.audit import events
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.clock import format_timestamp
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GRAPH_NAME_RE, GraphId, TermRef
from graph_cli.kernel.ports.audit import AuditReceipt
from graph_cli.kernel.ports.catalog import GraphEntry, ManifestData, StoreInfo
from graph_cli.storage.files.layout import GraphLayout
from graph_cli.storage.files.releases import release_hashes
from graph_cli_admin.services_admin.context import AdminContext, request
from graph_cli_admin.services_admin.ontology import Compiled, compile_source, release_files

STORE = StoreInfo("files", 1)


def create(
    ctx: AdminContext, *, name: str, source: Path, module: str | None, title: str | None
) -> ManifestData:
    if not GRAPH_NAME_RE.match(name):
        raise GraphCliError(
            "usage.invalid_argument",
            f"graph name {name!r} must match {GRAPH_NAME_RE.pattern}",
            details={"argument": "name"},
        )
    compiled = compile_source(source, module)
    pin = compiled.pin
    meta = request(
        "graph create",
        {"name": name, "title": title, "ontology": pin.to_json()},
    )
    decisions = ctx.authorize([("maint.graph_create", "installation")], request=meta)
    now = ctx.clock.now()
    entry = GraphEntry(GraphId(ctx.ids.new_id()), name, "active", pin, alias_prefixes(compiled), ())
    manifest = ManifestData(entry, title, now, STORE)
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
    return manifest


def alias_prefixes(compiled: Compiled) -> dict[TermRef, str]:
    """Seeded from the IR ``display_prefix`` hints of concrete classes (B1 open point)."""
    return {
        TermRef(curie): cls.display_prefix
        for curie, cls in sorted(compiled.model.classes.items())
        if cls.display_prefix is not None and not cls.abstract
    }


def describe(manifest: ManifestData) -> dict[str, JsonValue]:
    entry = manifest.entry
    return {
        "id": str(entry.id),
        "name": entry.name,
        "title": manifest.title,
        "state": entry.state,
        "created_at": format_timestamp(manifest.created_at),
        "ontology": entry.pin.to_json(),
        "alias_prefixes": {str(curie): prefix for curie, prefix in entry.alias_prefixes.items()},
        "extensions": list(entry.extensions),
        "store": {
            "backend": manifest.store.backend,
            "format_version": manifest.store.format_version,
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
            "state": m.entry.state,
            "ontology": {"bundle": m.entry.pin.bundle, "version": m.entry.pin.version},
        }
        for m in ctx.catalog.manifests(include_archived=include_archived)
    ]


def show(ctx: AdminContext, ref: str) -> tuple[GraphEntry, dict[str, JsonValue]]:
    entry = ctx.catalog.resolve(ref)
    manifest = ctx.catalog.manifest(entry.id)
    head = ctx.catalog.file_store(entry.id).head()
    layout = GraphLayout(ctx.catalog.graphs_dir / str(entry.id))
    data = describe(manifest)
    data["seq"] = head.seq
    data["releases"] = sorted(release_hashes(layout))
    return entry, data
