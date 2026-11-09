"""Seeds of both toy ontologies through the write pipeline (contracts B3.8, B3.9; plan W8).

The seeds are the positive control of the whole pipeline: they apply as
JSONL with cross-document locals on both backends, ``check_graph`` finds
nothing (batch rules included), audit and storage verify are clean, and a
re-run replays every document from its idempotency key.
"""

from pathlib import Path

import pytest
from support.admin_context import key_rings
from support.fixtures import FIXTURES
from support.services import ServiceEnv

from graph_cli.kernel.changeset import read_documents
from graph_cli.kernel.records import RequestMeta
from graph_cli.ontology_layer.data_validator import check_graph
from graph_cli.services.write import BatchResult, WriteRequest
from graph_cli.storage.files.maintenance import FileStoreMaintenance

SEEDS = FIXTURES / "seeds"
REQUEST = RequestMeta.of("apply", {})


@pytest.fixture
def svc(backend: str, ontology: str, tmp_path: Path) -> ServiceEnv:
    return ServiceEnv(backend, ontology, tmp_path / "root")


def apply_seed(svc: ServiceEnv) -> BatchResult:
    documents = read_documents((SEEDS / svc.ontology / "changes.jsonl").read_bytes())
    requests = [
        WriteRequest(svc.actor, svc.name, svc.service.parse(doc), False, REQUEST)
        for doc in documents
    ]
    return svc.service.apply_batch(requests)


def test_seed_applies_and_checks_clean(svc: ServiceEnv) -> None:
    outcome = apply_seed(svc)
    assert outcome.error is None, outcome.error.details if outcome.error else None
    assert [r.seq for r in outcome.results] == list(range(1, len(outcome.results) + 1))
    assert all(not r.findings and not r.warnings for r in outcome.results)
    assert check_graph(svc.model, svc.snapshot()) == []
    assert svc.verify().findings == ()
    revisions = svc.all_revisions()
    assert 20 <= len(revisions) <= 50
    assert {r.record_status for r in revisions} == {"active", "superseded", "retracted"}
    for name, lifecycle in svc.model.lifecycles.items():
        attrs = {
            attr
            for cls in svc.model.classes.values()
            for attr, definition in cls.attributes.items()
            if definition.type == name
        }
        seen = {r.attrs.get(a) for r in revisions for a in attrs}
        assert set(lifecycle.state_names()) <= seen, name
    rels = {e.rel for r in revisions for e in r.edges}
    assert "core:supersedes" in rels
    if svc.backend.startswith("files"):
        maintenance = FileStoreMaintenance(svc.root, rings=key_rings(svc.root))
        with maintenance.maintenance_session(svc.entry.id, timeout_s=5) as session:
            assert list(session.verify()) == []


def test_seed_rerun_replays_every_document(svc: ServiceEnv) -> None:
    first = apply_seed(svc)
    head = svc.head_seq()
    again = apply_seed(svc)
    assert again.error is None
    assert all(r.idempotent_replay for r in again.results)
    assert [r.tx_id for r in again.results] == [r.tx_id for r in first.results]
    assert [r.entities for r in again.results] == [r.entities for r in first.results]
    assert svc.head_seq() == head


def test_seed_cross_references(svc: ServiceEnv) -> None:
    outcome = apply_seed(svc)
    bound: dict[str, object] = {}
    for result in outcome.results:
        for entity in result.entities:
            if entity.local is not None:
                assert bound.setdefault(entity.local, entity.id) == entity.id
    later = [e for r in outcome.results[1:] for e in r.entities if e.rev > 1 and e.local]
    assert later, "later documents touch entities bound by earlier ones"
