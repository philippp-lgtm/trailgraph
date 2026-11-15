"""T: the storage finding codes of a graph whose stored files are in an object store
(object storage O5, O7, O8), one tamper scenario each, against the fake object store.

``blob_misplaced`` is a copy outside the location the manifest names (here a local blob
file of a graph in the object store); ``blob_store_unavailable`` stands for every blob
finding when the configured bucket is another store (its marker carries another id).
``tests/tamper/test_verify.py`` holds the scenarios of the other storage codes.
"""

import hashlib
import json
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from support.fake_s3 import FakeS3
from support.graphs import publish_ontology
from support.s3_graphs import blob_key, create_marker, object_store_manifest
from support.stores import StoreEnv

from graph_cli.kernel.ids import GraphId
from graph_cli.storage import finding_codes
from graph_cli.storage.files.maintenance import FileStoreMaintenance
from graph_cli.storage.files.store import FileStore
from graph_cli.storage.files.store_marker import MARKER_KEY, reset_identity_cache

CONTENT = b"synthetic stored bytes in an object store\n"
OTHER_ID = "0b1c2d3e-4f50-4617-8293-a4b5c6d7e8f9"


@pytest.fixture
def fake() -> Iterator[FakeS3]:
    reset_identity_cache()
    with FakeS3() as store:
        yield store
    reset_identity_cache()


def setup(tmp_path: Path, fake: FakeS3) -> tuple[StoreEnv, FileStore, FileStoreMaintenance]:
    settings = fake.settings()
    env = StoreEnv("s3", "inventory", tmp_path, seed=5, object_store=settings)
    env.random(6)
    env.write_blobs(lambda d: d.create(), [CONTENT])
    assert isinstance(env.store, FileStore)
    layout = env.store.layout
    publish_ontology(layout.root, env.graph_id, "inventory", env.syncer)
    object_store_manifest(layout, env.graph_id, create_marker(settings))
    maintenance = FileStoreMaintenance(tmp_path, syncer=env.syncer, object_store=settings)
    return env, env.store, maintenance


def verify(maintenance: FileStoreMaintenance, graph: GraphId) -> list[str]:
    """Storage ``verify`` plus the full blob check, each code once."""
    with maintenance.maintenance_session(graph, timeout_s=5) as session:
        codes = [f.code for f in session.verify()]
        extra = [f.code for f in session.verify_blobs(rehash=True).findings]
    return [*codes, *(c for c in dict.fromkeys(extra) if c not in codes)]


def copy_into_the_data_root(env: StoreEnv, fake: FakeS3) -> None:
    assert isinstance(env.store, FileStore)
    (key,) = [k for k in fake.objects if k.startswith(f"graphs/{env.graph_id}/blobs/")]
    digest = "sha256:" + key.rsplit("/", 1)[1]
    path = env.store.layout.blob_path(digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(fake.get(key) or b"")


def marker_of_another_store(env: StoreEnv, fake: FakeS3) -> None:
    document = {"schema": "graph_cli.object_store/1", "store_id": OTHER_ID, "created_at": "x"}
    fake.put(MARKER_KEY, json.dumps(document).encode())
    reset_identity_cache()


SCENARIOS: dict[str, Callable[[StoreEnv, FakeS3], None]] = {
    finding_codes.BLOB_MISPLACED: copy_into_the_data_root,
    finding_codes.BLOB_STORE_UNAVAILABLE: marker_of_another_store,
}


def test_a_graph_in_the_object_store_verifies_clean(tmp_path: Path, fake: FakeS3) -> None:
    env, _, maintenance = setup(tmp_path, fake)
    assert verify(maintenance, env.graph_id) == []
    assert fake.get(blob_key(env.graph_id, "sha256:" + hashlib.sha256(CONTENT).hexdigest()))


@pytest.mark.parametrize("code", sorted(SCENARIOS))
def test_tamper_scenario_triggers_only_its_code(tmp_path: Path, fake: FakeS3, code: str) -> None:
    env, _, maintenance = setup(tmp_path, fake)
    SCENARIOS[code](env, fake)
    assert set(verify(maintenance, env.graph_id)) == {code}
