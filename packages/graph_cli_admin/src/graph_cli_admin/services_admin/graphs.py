"""Graph lifecycle: create (W.f), list and show (contracts B1.11, B5.2, B12.8).

``create`` authorizes ``maint.graph_create`` on the installation, then
under the registry lock checks the name against every graph (archived ones
too, B1.11) and hands storage the manifest, the genesis event and the first
release; storage stages them, calls back for the installation event with
the anchor, and publishes the graph with one rename.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from graph_cli.audit import events
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.clock import format_timestamp
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GRAPH_NAME_RE, GraphId, TermRef
from graph_cli.kernel.langtag import normalize_language_tag
from graph_cli.kernel.ports.audit import AuditReceipt
from graph_cli.kernel.ports.catalog import GraphEntry, ManifestData, StoreInfo
from graph_cli.kernel.records import RequestMeta
from graph_cli.storage.files.layout import GraphLayout
from graph_cli.storage.files.releases import release_hashes
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
    return manifest


def alias_prefixes(compiled: Compiled) -> dict[TermRef, str]:
    """Seeded from the IR ``display_prefix`` hints of concrete classes (B1 open point)."""
    return {
        TermRef(curie): cls.display_prefix
        for curie, cls in sorted(compiled.model.classes.items())
        if cls.display_prefix is not None and not cls.abstract
    }


def merged_alias_prefixes(
    current: Mapping[TermRef, str], compiled: Compiled
) -> tuple[dict[TermRef, str], dict[TermRef, str]]:
    """The manifest's prefixes plus those of classes a release adds, and the seeds left
    out. An existing prefix never changes: a class renamed with ``renamed_from`` keeps
    the prefix of its old name (its aliases count on), and a seed that another class
    already uses is left out (the class gets no aliases)."""
    merged = dict(current)
    used = set(current.values())
    skipped: dict[TermRef, str] = {}
    for name, cls in sorted(compiled.model.classes.items()):
        curie = TermRef(name)
        if cls.abstract or curie in merged:
            continue
        if cls.renamed_from is not None:
            old = TermRef(f"{name.partition(':')[0]}:{cls.renamed_from}")
            if old in current:
                merged[curie] = current[old]
                continue
        prefix = cls.display_prefix
        if prefix is None:
            continue
        if prefix in used:
            skipped[curie] = prefix
            continue
        merged[curie] = prefix
        used.add(prefix)
    return merged, skipped


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
    return entry, data
