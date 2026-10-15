"""The write service over both backends, with test clock and seeded ids (plan W10).

``MemoryCatalog`` serves graphs from ``InMemoryStore`` with release files in
memory; the files backend creates its graph through graph-admin's service
and reads it with ``FileCatalog``, as the app will. ``ServiceEnv`` gives a
C test one graph, a ``WriteService`` and request builders. ``OpsFactory``
writes valid create ops for any class from the compiled model alone
(required attributes and required relations), so core tests name no type.
"""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from graph_cli.access_control.policy import BuiltinPolicySource
from graph_cli.audit import events
from graph_cli.audit.verify import StreamReport, verify_graph
from graph_cli.catalog.file_catalog import FileCatalog
from graph_cli.catalog.identity import IdentityInputs, resolve_actor
from graph_cli.kernel.actor import ActorContext, ClientInfo, Entrypoint
from graph_cli.kernel.canonical import JsonValue, hash_value
from graph_cli.kernel.changeset import ChangeSet, parse_changeset, read_documents
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ids import EntityId, EventId, GraphId, TermRef
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM, AuditSink, graph_stream
from graph_cli.kernel.ports.catalog import GraphEntry, SkillSlot
from graph_cli.kernel.ports.storage import GraphStoreRead, GraphStoreWrite, Snapshot
from graph_cli.kernel.query_ast import Query
from graph_cli.kernel.records import EnvInfo, OntologyPin, RequestMeta, TxRecord
from graph_cli.ontology_layer.model import AttributeDef, CompiledModel
from graph_cli.services.read import ReadService
from graph_cli.services.write import (
    BatchResult,
    WriteConfig,
    WriteRequest,
    WriteResult,
    WriteService,
)
from graph_cli.storage.files.audit_log import FileAuditLog
from graph_cli.storage.memory.audit_log import InMemoryAuditLog
from graph_cli.storage.memory.store import InMemoryStore
from support.admin_context import admin_context
from support.audit_events import allow
from support.clock import StepClock
from support.fixtures import FIXTURES, ONTOLOGIES, compiled_model
from support.graphs import pin_of, release_files
from support.ids import SeededIdProvider
from support.stores import ALL_STATUSES, FaultSwitch

ENV = EnvInfo(data_root="<data root>", root_source="default")
SEEDS = FIXTURES / "seeds"
SEED_REQUEST = RequestMeta("apply", "sha256:" + "0" * 64)


def app_actor(uid: int = 501, user: str = "tester") -> ActorContext:
    actor, _ = resolve_actor(
        IdentityInputs(env={}, uid=uid, user_name=user, pid=4242),
        client=ClientInfo("graph", "0.1.0", Entrypoint.APP),
    )
    return actor


def alias_prefixes(model: CompiledModel) -> dict[TermRef, str]:
    """As graph-admin seeds them: ``display_prefix`` of concrete classes."""
    return {
        TermRef(curie): cls.display_prefix
        for curie, cls in sorted(model.classes.items())
        if cls.display_prefix is not None and not cls.abstract
    }


class MemoryCatalog:
    """The ``Catalog`` port over in-memory stores; one audit log for every stream."""

    def __init__(self, faults: FaultSwitch | None = None) -> None:
        self.faults = faults
        self.audit = InMemoryAuditLog(faults=faults)
        self.entries: dict[GraphId, GraphEntry] = {}
        self.stores: dict[GraphId, InMemoryStore] = {}
        self.files: dict[tuple[GraphId, str, str], bytes] = {}
        self.skills: dict[GraphId, SkillSlot] = {}

    def add(self, entry: GraphEntry, model: CompiledModel, event_id: EventId) -> None:
        self.entries[entry.id] = entry
        self.stores[entry.id] = InMemoryStore(entry.id, audit=self.audit, faults=self.faults)
        for name, data in release_files(model).items():
            self.files[(entry.id, model.bundle_version, name)] = data
        genesis = events.graph_genesis(
            actor=app_actor(),
            decisions=[allow("maint.graph_create", "installation")],
            request=RequestMeta("graph create", hash_value({"name": entry.name})),
            ts=StepClock().now(),
            event_id=event_id,
            env=ENV,
            graph=entry,
        )
        self.audit.append(graph_stream(str(entry.id)), genesis, session_token=object())

    @property
    def warnings(self) -> tuple[WarningRecord, ...]:
        return ()

    def list_graphs(
        self, *, include_archived: bool = False, skip_unreadable: bool = False
    ) -> Sequence[GraphEntry]:
        found = sorted(self.entries.values(), key=lambda e: (e.name, e.id))
        return [e for e in found if include_archived or e.state != "archived"]

    def resolve(self, ref: str) -> GraphEntry:
        for entry in self.entries.values():
            if ref in (entry.name, str(entry.id)):
                return entry
        raise GraphCliError("graph.not_found", f"no graph {ref!r}", details={"ref": ref})

    def read_release_file(self, graph: GraphId, version: str, name: str) -> bytes:
        try:
            return self.files[(graph, version, name)]
        except KeyError:
            raise GraphCliError("ontology.artifact_missing", f"no {version}/{name}") from None

    def read_skill(self, graph: GraphId) -> SkillSlot | None:
        return self.skills.get(graph)

    def store_for(self, graph: GraphId) -> tuple[GraphStoreRead, GraphStoreWrite]:
        store = self.stores[graph]
        return store, store


@dataclass
class OpsFactory:
    """Valid create ops for any concrete class: required values and required relations."""

    model: CompiledModel
    counter: int = 0

    def number(self) -> int:
        self.counter += 1
        return self.counter

    def value(self, attribute: AttributeDef) -> JsonValue:
        n = self.number()
        if attribute.kind == "enum":
            value: JsonValue = self.model.enums[attribute.type].values[0].name
        else:
            base = self.model.value_base(attribute.kind, attribute.type)
            value = {
                "integer": n,
                "decimal": f"{n}.50",
                "boolean": True,
                "date": f"2026-01-{n % 28 + 1:02d}",
                "datetime": "2026-01-01T00:00:00.000Z",
                "duration": f"P{n}D",
                "iri": f"https://example.org/{n}",
                "json": {"n": n},
            }.get(base, f"value {n}")
        return [value] if attribute.many else value

    def attrs(self, cls: str) -> dict[str, JsonValue]:
        return {
            name: self.value(attribute)
            for name, attribute in sorted(self.model.classes[cls].attributes.items())
            if attribute.required and attribute.default is None and attribute.kind != "lifecycle"
        }

    def concrete(self, cls: str) -> str:
        return next(c for c in self.model.subtypes(cls) if not self.model.classes[c].abstract)

    def create(self, cls: str, local: str = "x", *, depth: int = 0) -> list[dict[str, Any]]:
        """Ops creating ``cls`` as ``@local`` plus a target per required relation."""
        assert depth < 5, "required relations form a cycle"
        ops: list[dict[str, Any]] = []
        edges = []
        for index, (name, relation) in enumerate(sorted(self.model.relations.items())):
            if name.startswith("core:") or not self.model.is_a_any(cls, relation.domain):
                continue
            for k in range(relation.out.min):
                target = f"{local}_r{index}_{k}"
                ops += self.create(self.concrete(relation.range[0]), target, depth=depth + 1)
                edges.append({"rel": name, "target": f"@{target}"})
        op: dict[str, Any] = {"op": "create", "type": cls, "local": local, "attrs": self.attrs(cls)}
        if edges:
            op["edges"] = edges
        return [*ops, op]


@dataclass
class ServiceEnv:
    backend: str
    ontology: str
    root: Path
    faults: FaultSwitch = field(default_factory=FaultSwitch)
    name: str = "example_graph"
    model_override: CompiledModel | None = None  # memory backend only

    def __post_init__(self) -> None:
        self.model = self.model_override or compiled_model(self.ontology)
        self.clock = StepClock(start="2030-01-01T00:00:00Z")
        self.ids = SeededIdProvider(self.clock, 5)
        self.factory = OpsFactory(self.model)
        self.actor = app_actor()
        if self.backend == "memory":
            memory = MemoryCatalog(self.faults)
            entry = GraphEntry(
                GraphId(self.ids.new_id()),
                self.name,
                "active",
                pin_of(self.model),
                alias_prefixes(self.model),
                (),
            )
            memory.add(entry, self.model, EventId(self.ids.new_id()))
            self.catalog: MemoryCatalog | FileCatalog = memory
            self.audit: AuditSink = memory.audit
        else:
            from graph_cli_admin.services_admin.graphs import create

            source = ONTOLOGIES / self.ontology / self.model.bundle_version
            create(
                admin_context(self.root),
                name=self.name,
                source=source,
                module=None,
                title=None,
                language=None,
            )
            self.catalog = FileCatalog(self.root, faults=self.faults)
            self.audit = FileAuditLog(self.root, faults=self.faults)
        self.entry = self.catalog.resolve(self.name)
        self.service = self.make_service()

    def make_service(self, **config: Any) -> WriteService:
        return WriteService(
            catalog=self.catalog,
            audit=self.audit,
            policy=BuiltinPolicySource(),
            clock=self.clock,
            ids=self.ids,
            env=ENV,
            config=WriteConfig(lock_timeout_s=5.0, **config),
        )

    def make_reader(self, policy: Any = None) -> ReadService:
        return ReadService(
            catalog=self.catalog,
            audit=self.audit,
            policy=policy or BuiltinPolicySource(),
            clock=self.clock,
            ids=self.ids,
            env=ENV,
        )

    def apply_seed(self) -> BatchResult:
        documents = read_documents((SEEDS / self.ontology / "changes.jsonl").read_bytes())
        requests = [
            WriteRequest(self.actor, self.name, self.service.parse(doc), False, SEED_REQUEST)
            for doc in documents
        ]
        return self.service.apply_batch(requests)

    # ---- requests ------------------------------------------------------

    def changeset(self, *ops: Mapping[str, Any], **top: Any) -> ChangeSet:
        return parse_changeset({"ops": list(ops), **top})

    def request(
        self,
        *ops: Mapping[str, Any],
        dry_run: bool = False,
        actor: ActorContext | None = None,
        bindings: Mapping[str, EntityId] | None = None,
        **top: Any,
    ) -> WriteRequest:
        return WriteRequest(
            actor=actor or self.actor,
            graph=self.name,
            changeset=self.changeset(*ops, **top),
            dry_run=dry_run,
            request=RequestMeta("apply", hash_value({"ops": len(ops)})),
            bindings=bindings or {},
        )

    def apply(self, *ops: Mapping[str, Any], **kw: Any) -> WriteResult:
        return self.service.apply(self.request(*ops, **kw))

    def refuse(self, *ops: Mapping[str, Any], **kw: Any) -> GraphCliError:
        try:
            self.service.apply(self.request(*ops, **kw))
        except GraphCliError as exc:
            return exc
        raise AssertionError("the write was not refused")

    def batch(self, documents: Sequence[Sequence[Mapping[str, Any]]], **kw: Any) -> BatchResult:
        return self.service.apply_batch([self.request(*doc, **kw) for doc in documents])

    def create(self, cls: str, local: str = "x", **kw: Any) -> WriteResult:
        return self.apply(*self.factory.create(cls, local), **kw)

    # ---- inspection ----------------------------------------------------

    @property
    def store(self) -> GraphStoreRead:
        return self.catalog.store_for(self.entry.id)[0]

    def snapshot(self) -> Snapshot:
        return self.store.snapshot()

    def head_seq(self) -> int:
        return self.store.head().seq

    def txs(self) -> list[TxRecord]:
        return list(self.store.changes_since(0, limit=10**6))

    def all_revisions(self) -> list[Any]:
        return list(self.snapshot().query(Query(record_status=ALL_STATUSES, limit=10**6)).items)

    def graph_events(self) -> list[Mapping[str, JsonValue]]:
        return list(self.audit.read(graph_stream(str(self.entry.id))))

    def installation_events(self) -> list[Mapping[str, JsonValue]]:
        return list(self.audit.read(INSTALLATION_STREAM))

    def verify(self) -> StreamReport:
        lines = self.audit.read_lines(graph_stream(str(self.entry.id)))
        return verify_graph(lines, graph_id=self.entry.id, txs=self.txs())

    def set_entry(self, **changes: Any) -> None:
        """Change the manifest (state, pin) the way graph-admin would, test-only."""
        entry = replace(self.catalog.resolve(self.name), **changes)
        if isinstance(self.catalog, MemoryCatalog):
            self.catalog.entries[entry.id] = entry
            return
        from graph_cli.storage.files.manifest import format_manifest

        manifest = self.catalog.manifest(entry.id)
        path = self.root / "graphs" / str(entry.id) / "manifest.toml"
        path.write_bytes(format_manifest(replace(manifest, entry=entry)))

    def tamper_artifact(self) -> None:
        """Change one summary in the pinned ``compiled.json``: its C.2 hash no longer matches."""
        marker = b'"summary": "'
        version = self.entry.pin.version
        if isinstance(self.catalog, MemoryCatalog):
            key = (self.entry.id, version, "compiled.json")
            self.catalog.files[key] = self.catalog.files[key].replace(marker, marker + b"X", 1)
            return
        path = self.root / "graphs" / str(self.entry.id) / "ontology" / version / "compiled.json"
        path.write_bytes(path.read_bytes().replace(marker, marker + b"X", 1))


def iter_revisions(snapshot: Snapshot) -> Iterator[Any]:
    yield from snapshot.query(Query(record_status=ALL_STATUSES, limit=10**6)).items


def pin_for(ontology: str, version: str | None = None) -> OntologyPin:
    return pin_of(compiled_model(ontology, version))
