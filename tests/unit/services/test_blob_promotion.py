"""W.5b: staged blobs are promoted after W.1-W.5 and before the lock (object storage O6).

A store that keeps stored files elsewhere uploads them in ``promote_blobs``, so a request
that is denied, names a missing entity or is a dry-run must never reach it, and an
allowed one reaches it once, before its session takes the graph lock. Validation (W.11)
runs under the lock: an authorized request found invalid there has promoted its blobs.
The ``s3`` parameter checks the fake object store's bucket as well: a denied add leaves
no object there.
"""

import hashlib
from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from support.fake_s3 import FakeS3
from support.s3_graphs import blob_keys
from support.services import ServiceEnv

from graph_cli.access_control.policy import PolicySet
from graph_cli.kernel.canonical import JsonValue, hash_value
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ports.storage import StagedBlob
from graph_cli.kernel.records import RequestMeta
from graph_cli.services.files import FileAddRequest, FileAddResult, FileService
from graph_cli.storage.files.store import FileStore
from graph_cli.storage.memory.store import InMemoryStore

TEXT = b"plain text for the promotion tests\n"
DIGEST = "sha256:" + hashlib.sha256(TEXT).hexdigest()


@pytest.fixture(params=["memory", "files", "s3"])
def svc(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[ServiceEnv]:
    """Both backends, and a files graph whose stored files are in the fake object store."""
    backend = str(request.param)
    root = tmp_path / "root"
    if backend != "s3":
        yield ServiceEnv(backend, "notes_and_sources", root, version="1.1.0")
        return
    with FakeS3() as fake:
        yield ServiceEnv(
            backend, "notes_and_sources", root, version="1.1.0", object_store=fake.settings()
        )


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "note.txt"
    path.write_bytes(TEXT)
    return path


Promotion = tuple[list[str], bool, int]  # hashes, lock held, head seq at the time


@pytest.fixture
def promoted(monkeypatch: pytest.MonkeyPatch) -> list[Promotion]:
    calls: list[Promotion] = []
    for cls in (InMemoryStore, FileStore):
        original = cls.promote_blobs

        def spy(self: Any, staged: Sequence[StagedBlob], original: Any = original) -> None:
            calls.append(([b.hash for b in staged], self.writer_active(), self.head().seq))
            original(self, staged)

        monkeypatch.setattr(cls, "promote_blobs", spy)
    return calls


def add(service: FileService, svc: ServiceEnv, path: Path, **kw: Any) -> FileAddResult:
    return service.add(
        FileAddRequest(
            actor=svc.actor,
            graph=svc.name,
            path=path,
            request=RequestMeta("file add", hash_value({"path": path.name})),
            **kw,
        )
    )


def only_listing() -> object:
    """A policy that lets the caller see the graph and nothing else (a denied write)."""
    document: dict[str, JsonValue] = {
        "schema": "graph_cli.policy/1",
        "id": "test",
        "rules": [
            {"id": "list", "effect": "allow", "action": ["read.list"], "resource": "graph:*"}
        ],
        "obligations": [],
    }
    policy = PolicySet("sha256:" + "1" * 64, (document,))

    class Policy:
        def current(self, graph: object) -> PolicySet:
            return policy

        def refresh(self) -> None:
            pass

    return Policy()


def test_an_allowed_add_promotes_once_before_the_lock(
    svc: ServiceEnv, source: Path, promoted: list[Promotion]
) -> None:
    result = add(svc.make_files(), svc, source)
    assert promoted == [([DIGEST], False, 0)]  # no lock held, nothing committed yet
    assert result.file["hash"] == DIGEST
    assert svc.store.blob(DIGEST) is not None


def test_a_denied_add_never_reaches_promotion(
    svc: ServiceEnv, source: Path, promoted: list[Promotion]
) -> None:
    denied = FileService(catalog=svc.catalog, writer=svc.make_service(only_listing()))
    with pytest.raises(GraphCliError) as caught:
        add(denied, svc, source)
    assert caught.value.code == "access.denied"
    assert promoted == []
    assert svc.store.blob(DIGEST) is None
    assert blob_keys(svc.object_store) == []  # a denied add leaves no object in the bucket


def test_a_dry_run_never_reaches_promotion(
    svc: ServiceEnv, source: Path, promoted: list[Promotion]
) -> None:
    add(svc.make_files(), svc, source, dry_run=True)
    assert promoted == []
    assert blob_keys(svc.object_store) == []


def test_a_request_with_a_missing_reference_never_reaches_promotion(
    svc: ServiceEnv, promoted: list[Promotion]
) -> None:
    write = svc.catalog.store_for(svc.entry.id)[1]
    staged = write.stage_blob([TEXT], max_bytes=len(TEXT))
    try:
        request = replace(
            svc.request({"op": "update", "id": "FILE-0099", "set": {"title": "x"}}),
            blobs=(staged,),
        )
        with pytest.raises(GraphCliError) as caught:
            svc.service.apply(request)
        assert caught.value.code == "entity.not_found"
    finally:
        staged.discard()
    assert promoted == []
    assert blob_keys(svc.object_store) == []


def test_a_request_over_a_limit_never_reaches_promotion(
    svc: ServiceEnv, source: Path, promoted: list[Promotion]
) -> None:
    write = svc.catalog.store_for(svc.entry.id)[1]
    staged = write.stage_blob([TEXT], max_bytes=len(TEXT))
    try:
        ops = [{"op": "update", "id": "FILE-0099", "set": {"title": "x"}}] * 2
        request = replace(svc.request(*ops), blobs=(staged,))
        with pytest.raises(GraphCliError) as caught:
            svc.make_service(max_ops=1).apply(request)
        assert caught.value.code == "changeset.too_large"
    finally:
        staged.discard()
    error = None
    try:
        add(svc.make_files(max_bytes=10), svc, source)  # file.too_large while staging
    except GraphCliError as exc:
        error = exc
    assert error is not None and error.code == "file.too_large"
    assert promoted == []
    assert blob_keys(svc.object_store) == []


def test_an_add_found_invalid_under_the_lock_has_promoted(
    svc: ServiceEnv, source: Path, promoted: list[Promotion]
) -> None:
    """W.11 runs under the lock; the promoted blob is referenced by no tx."""
    with pytest.raises(GraphCliError) as caught:
        add(svc.make_files(), svc, source, attrs={"pages": 3})
    assert caught.value.code == "write.invalid"
    assert promoted == [([DIGEST], False, 0)]
    assert svc.head_seq() == 0
    assert len(blob_keys(svc.object_store)) == (svc.backend == "s3")
