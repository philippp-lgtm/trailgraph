"""The read view of a caller without a marking reaches the kernel through the read service, on
both backends (field visibility plan F4, F5, step 3).

Step 3 builds the plumbing only: the read view's attribute half, the snapshot view and the kernel
mask. The model view, hints, migration surfaces and the per-command tests are step 4. The
``personnel`` series marks ``email`` and ``birth_date`` of ``ex:Person`` with ``ex:personal``;
without an installation policy no app caller holds a marking (the built-in policy is unchanged).
"""

import json
from pathlib import Path
from typing import Any

import pytest
from support.services import MemoryCatalog, ServiceEnv

from graph_cli.access_control.compiler import compile_policy
from graph_cli.access_control.policy import policy_set
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.kernel.query_ast import Cmp, OrderKey, Query
from graph_cli.kernel.read_view import UNLIMITED
from graph_cli.kernel.records import RequestMeta, revision_content_hash
from graph_cli.services import models as models_module
from graph_cli.services.read import GraphRead, ReadService
from graph_cli.services.visibility import VisibleSnapshot

REQUEST = RequestMeta("find", "sha256:" + "0" * 64)
PERSON = "ex:Person"
HOLDER = b"""\
schema: graph_cli.policy/1
id: installation
grants:
  - {entrypoint: app, graphs: [example_graph], level: write, markings: [ex:personal]}
no_project: {"*": write}
"""


class Source:
    def __init__(self, policy: PolicySet) -> None:
        self.policy = policy

    def current(self, graph: object) -> PolicySet:
        return self.policy

    def refresh(self) -> None:
        pass


@pytest.fixture(params=("personnel",))
def ontology(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def svc(backend: str, ontology: str, tmp_path: Path) -> ServiceEnv:
    env = ServiceEnv(backend, ontology, tmp_path / "root")
    # the marked values are written by a holder (a caller without the marking cannot write them)
    writer = env.make_service(Source(holding(env)))
    result = writer.apply(
        env.request(
            {
                "op": "create",
                "type": PERSON,
                "local": "ada",
                "attrs": {
                    "name": "Ada",
                    "email": "ada@example.org",
                    "birth_date": "1990-01-01",
                    "team": "core",
                },
            },
            {"op": "create", "type": PERSON, "local": "bo", "attrs": {"name": "Bo", "team": "ops"}},
            {"op": "create", "type": "ex:Team", "local": "core", "attrs": {"name": "core"}},
        )
    )
    assert len(result.entities) == 3
    return env


def holding(svc: ServiceEnv) -> PolicySet:
    graphs = {"example_graph": svc.entry.id}
    return policy_set(compile_policy(HOLDER, file="installation.yaml", graphs=graphs).document)


def holder(svc: ServiceEnv) -> ReadService:
    return svc.make_reader(Source(holding(svc)))


def open_view(svc: ServiceEnv, reader: ReadService) -> GraphRead:
    return reader.open(svc.actor, svc.entry, "read.query", REQUEST)


def ids(svc: ServiceEnv) -> dict[str, EntityId]:
    found = svc.store.snapshot().query(Query(types=(PERSON,))).items  # type: ignore[arg-type]
    return {str(r.attrs["name"]): r.id for r in found}


def test_a_caller_without_the_marking_reads_through_the_attribute_mask(svc: ServiceEnv) -> None:
    view = open_view(svc, svc.make_reader())
    assert view.read_view.attributes_limited and not view.read_view.types_limited
    assert isinstance(view.snapshot, VisibleSnapshot)
    assert view.read_view.keeps(PERSON) == {"name", "team"}
    snapshot = view.snapshot
    assert snapshot.query(Query(where=Cmp("email", "exists"))).total == 0
    assert snapshot.query(Query(where=Cmp("birth_date", "eq", "1990-01-01"))).items == ()
    everyone = snapshot.query(Query(types=(PERSON,), order=(OrderKey("email"),)))  # type: ignore[arg-type]
    assert sorted(str(r.attrs["name"]) for r in everyone.items) == ["Ada", "Bo"]
    for revision in everyone.items:
        assert set(revision.attrs) <= {"name", "team"}
        assert revision.content_hash == revision_content_hash(revision)
    ada = snapshot.get(ids(svc)["Ada"])
    assert ada is not None and ada.attrs == {"name": "Ada", "team": "core"}
    assert ada.content_hash == revision_content_hash(ada)


def test_a_holder_of_every_marking_reads_the_raw_graph(svc: ServiceEnv) -> None:
    view = open_view(svc, holder(svc))
    assert view.read_view == UNLIMITED
    assert not isinstance(view.snapshot, VisibleSnapshot)
    found = view.snapshot.query(Query(where=Cmp("email", "exists")))
    assert [r.attrs["name"] for r in found.items] == ["Ada"]
    raw = svc.store.snapshot().get(ids(svc)["Ada"])
    assert view.snapshot.get(ids(svc)["Ada"]) == raw


def test_the_skill_digest_follows_the_read_view(svc: ServiceEnv) -> None:
    reader: Any = svc.make_reader()
    digest = reader.skill_digest(svc.actor, svc.entry)
    assert digest is not None and digest == reader.read_view(svc.actor, svc.entry).digest
    assert holder(svc).skill_digest(svc.actor, svc.entry) is None


def test_the_type_half_stays_unlimited_for_whoami_and_files(svc: ServiceEnv) -> None:
    reader = svc.make_reader()
    assert reader.visible_types(svc.actor, svc.entry) is None
    assert reader.read_view(svc.actor, svc.entry).types is None


# ---- an artifact that does not match its pin (review fix of stage 4) ----------------------


def artifact(svc: ServiceEnv) -> bytes:
    version = svc.entry.pin.version
    if isinstance(svc.catalog, MemoryCatalog):
        return svc.catalog.files[(svc.entry.id, version, "compiled.json")]
    return (
        svc.root / "graphs" / str(svc.entry.id) / "ontology" / version / "compiled.json"
    ).read_bytes()


def set_artifact(svc: ServiceEnv, data: bytes) -> None:
    version = svc.entry.pin.version
    if isinstance(svc.catalog, MemoryCatalog):
        svc.catalog.files[(svc.entry.id, version, "compiled.json")] = data
        return
    path = svc.root / "graphs" / str(svc.entry.id) / "ontology" / version / "compiled.json"
    path.write_bytes(data)


def without_markings(data: bytes) -> bytes:
    """The compiled document as if nothing were marked: schema ``/1``, no trace of markings
    (and no longer the pinned artifact)."""
    document = json.loads(data)
    document["schema"] = "graph_cli.compiled_ontology/1"
    document.pop("markings", None)
    document.pop("requires", None)
    for cls in document["classes"].values():
        for attribute in cls["attributes"].values():
            attribute.pop("markings", None)
    return json.dumps(document).encode()


def graph_item(reader: ReadService, svc: ServiceEnv) -> Any:
    graphs: Any = reader.whoami(svc.actor)["permissions"]
    (item,) = [g for g in graphs["graphs"] if g["graph"] == svc.entry.name]
    return item


def test_a_marked_artifact_that_does_not_match_the_pin_refuses_the_read(svc: ServiceEnv) -> None:
    """Fail closed: only the pin vouches for which attributes an artifact marks, so a marked
    artifact that does not match it is not read (the error of an unreadable marked model),
    for a holder too; ``whoami`` still answers, with no marking held."""
    assert "markings" not in graph_item(holder(svc), svc)
    svc.tamper_artifact()
    for reader in (svc.make_reader(), holder(svc)):
        with pytest.raises(GraphCliError) as info:
            open_view(svc, reader)
        assert info.value.code == "ontology.pin_mismatch"
        with pytest.raises(GraphCliError):
            reader.read_view(svc.actor, svc.entry)
        assert graph_item(reader, svc)["markings"] == []


def test_markings_are_never_remembered_from_a_mismatching_artifact(svc: ServiceEnv) -> None:
    """An artifact without any trace of markings that does not match the pin is read as
    before (with ``ontology.pin_mismatch_read``); once the pinned artifact is back, the same
    service hides the marked attributes again."""
    reader = svc.make_reader()
    pinned = artifact(svc)
    set_artifact(svc, without_markings(pinned))
    view = open_view(svc, reader)
    assert view.read_view == UNLIMITED
    assert "ontology.pin_mismatch_read" in [w.code for w in view.warnings]
    assert reader.read_view(svc.actor, svc.entry) == UNLIMITED
    set_artifact(svc, pinned)
    assert reader.read_view(svc.actor, svc.entry).keeps(PERSON) == {"name", "team"}
    view = open_view(svc, reader)
    assert view.read_view.keeps(PERSON) == {"name", "team"} and view.warnings == ()
    ada = view.snapshot.get(ids(svc)["Ada"])
    assert ada is not None and "email" not in ada.attrs


def test_a_graph_without_markings_parses_no_model_for_the_read_view(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``whoami`` and the skill check of every command ask for the read view of a graph; one
    whose pinned artifact shows no trace of markings is answered from its raw bytes."""
    plain = ServiceEnv(backend, "inventory", tmp_path / "plain")
    parsed: list[int] = []
    real = models_module.load_compiled

    def counting(*args: Any, **kwargs: Any) -> Any:
        parsed.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(models_module, "load_compiled", counting)
    reader = plain.make_reader()
    assert reader.read_view(plain.actor, plain.entry) == UNLIMITED
    assert reader.skill_digest(plain.actor, plain.entry) is None
    assert "markings" not in graph_item(reader, plain)
    assert reader.visible_types(plain.actor, plain.entry) is None
    assert parsed == []
