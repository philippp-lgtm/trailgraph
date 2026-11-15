"""FileService.add on both backends (files slice 1, step 4): create, dedupe, origin, refusals.

The ``s3`` parameter is a files graph whose stored files are in the fake object store
(object storage O10).
"""

import hashlib
import stat
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from support.clock import FixedClock
from support.fake_s3 import FakeS3
from support.s3_graphs import blob_key, blob_keys
from support.services import RecordingActivity, ServiceEnv
from support.trees import tree_hash

from graph_cli.catalog.file_cache import StateFileCache
from graph_cli.catalog.file_catalog import FileCatalog
from graph_cli.kernel.canonical import JsonValue, hash_value
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId
from graph_cli.kernel.records import RequestMeta
from graph_cli.services import files as files_module
from graph_cli.services.files import FileAddRequest, FileAddResult, FileService
from graph_cli.storage.files.layout import STAGING_PREFIX

PDF = b"%PDF-1.4\n% synthetic test document\n1 0 obj << >> endobj\n%%EOF\n"
DIGEST = "sha256:" + hashlib.sha256(PDF).hexdigest()


class Roots:
    """One root ``notes`` at ``base``."""

    def __init__(self, base: Path) -> None:
        self.base = base.resolve()

    def origin_of(self, path: Path) -> dict[str, JsonValue] | None:
        try:
            return {"root": "notes", "path": path.relative_to(self.base).as_posix()}
        except ValueError:
            return None


@pytest.fixture(params=["memory", "files", "s3"])
def svc(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[ServiceEnv]:
    """Both backends, and a files graph whose stored files are in the fake object store."""
    backend = str(request.param)
    root = tmp_path / "root"
    if backend != "s3":
        yield ServiceEnv(backend, "notes_and_sources", root, version="1.1.0")
        return
    with FakeS3() as fake:
        env = ServiceEnv(
            backend, "notes_and_sources", root, version="1.1.0", object_store=fake.settings()
        )
        env.fake = fake  # type: ignore[attr-defined]
        yield env


@pytest.fixture
def notes(tmp_path: Path) -> Path:
    base = tmp_path / "notes"
    (base / "scans").mkdir(parents=True)
    path = base / "scans" / "report 2024.pdf"
    path.write_bytes(PDF)
    return path


def files_of(svc: ServiceEnv, notes: Path | None = None, **config: Any) -> FileService:
    roots = Roots(notes.parent.parent) if notes is not None else None
    return svc.make_files(origins=roots, **config)


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


def refuse(service: FileService, svc: ServiceEnv, path: Path, **kw: Any) -> GraphCliError:
    with pytest.raises(GraphCliError) as info:
        add(service, svc, path, **kw)
    return info.value


def staging(svc: ServiceEnv) -> list[str]:
    if svc.backend == "memory":
        return []
    blobs = svc.root / "graphs" / str(svc.entry.id) / "blobs"
    return (
        [p.name for p in blobs.iterdir() if p.name.startswith(STAGING_PREFIX)]
        if blobs.is_dir()
        else []
    )


def test_add_creates_a_file_with_its_blob(svc: ServiceEnv, notes: Path) -> None:
    result = add(files_of(svc, notes), svc, notes)
    assert result.existing is False
    assert result.entity["alias"] == "FILE-0001"
    assert result.entity["type"] == "files:File"
    assert result.file == {
        "hash": DIGEST,
        "size": len(PDF),
        "media_type": "application/pdf",
        "file_name": "report 2024.pdf",
        "origin": {"root": "notes", "path": "scans/report 2024.pdf"},
    }
    assert result.warnings == ()
    revision = svc.snapshot().get(EntityId(UUID(str(result.entity["id"]))))
    assert revision is not None
    assert revision.attrs == {
        "title": "report 2024",
        "content": DIGEST,
        "media_type": "application/pdf",
        "file_name": "report 2024.pdf",
        "origin": {"root": "notes", "path": "scans/report 2024.pdf"},
    }
    assert b"".join(svc.store.read_blob(DIGEST)) == PDF
    (tx,) = svc.txs()
    assert tx.blobs == (DIGEST,)
    intent = svc.graph_events()[-1]
    assert intent["change"]["blobs"] == [{"hash": DIGEST, "size": len(PDF)}]  # type: ignore[call-overload,index]
    assert staging(svc) == []
    assert blob_keys(svc.object_store) == (
        [blob_key(svc.entry.id, DIGEST)] if svc.object_store else []
    )
    assert svc.verify().findings == ()


def test_known_content_returns_the_stored_file(
    svc: ServiceEnv, notes: Path, tmp_path: Path
) -> None:
    service = files_of(svc, notes)
    first = add(service, svc, notes)
    copy = tmp_path / "copy.pdf"
    copy.write_bytes(PDF)
    second = add(service, svc, copy, title="Another name")
    assert second.existing is True
    assert second.entity["id"] == first.entity["id"]
    assert [w.code for w in second.warnings] == ["file.already_present"]
    assert second.to_json()["tx_id"] is None
    assert svc.head_seq() == 1
    assert staging(svc) == []


def test_a_concurrent_add_of_the_same_content_returns_the_winner(
    svc: ServiceEnv, notes: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = files_of(svc, notes)
    first = add(service, svc, notes)
    real = files_module.find_by_content
    calls: list[str] = []

    def late(*args: Any) -> Any:  # the other writer committed after our lookup
        calls.append("lookup")
        return None if len(calls) == 1 else real(*args)

    monkeypatch.setattr(files_module, "find_by_content", late)
    second = add(service, svc, notes)
    assert calls == ["lookup", "lookup"]
    assert (second.existing, second.entity["id"]) == (True, first.entity["id"])
    assert svc.head_seq() == 1


def test_subclass_with_its_own_attributes(svc: ServiceEnv, notes: Path) -> None:
    result = add(
        files_of(svc, notes),
        svc,
        notes,
        type="nas:Scan",
        title="Quarterly report",
        attrs_text={"scanned_on": "2026-03-01"},
        source_url="https://example.org/report.pdf",
        retrieved_at="2026-03-02",
    )
    assert (result.entity["type"], result.entity["alias"]) == ("nas:Scan", "SCAN-0001")
    revision = svc.snapshot().get(EntityId(UUID(str(result.entity["id"]))))
    assert revision is not None
    assert revision.attrs["title"] == "Quarterly report"
    assert revision.attrs["scanned_on"] == "2026-03-01"
    assert revision.attrs["source_url"] == "https://example.org/report.pdf"
    assert revision.attrs["retrieved_at"] == "2026-03-02"


@pytest.mark.parametrize("type_ref", ["nas:Note", "nas:Missing"])
def test_other_types_are_refused(svc: ServiceEnv, notes: Path, type_ref: str) -> None:
    error = refuse(files_of(svc, notes), svc, notes, type=type_ref)
    assert (error.code, error.details["argument"]) == ("usage.invalid_argument", "--type")


def test_a_model_without_the_module(tmp_path: Path, notes: Path) -> None:
    svc = ServiceEnv("memory", "notes_and_sources", tmp_path / "root")
    assert refuse(files_of(svc, notes), svc, notes).code == "file.module_missing"


def test_dry_run_writes_nothing(svc: ServiceEnv, notes: Path) -> None:
    before = tree_hash(svc.root) if svc.backend == "files" else None
    result = add(files_of(svc, notes), svc, notes, dry_run=True)
    assert result.entity["id"] is None and result.entity["alias"] is None
    assert result.file["hash"] == DIGEST
    assert result.to_json()["dry_run"] is True
    assert svc.store.blob(DIGEST) is None
    assert svc.head_seq() == 0
    if before is not None:
        assert tree_hash(svc.root) == before
    assert blob_keys(svc.object_store) == []


def test_a_refused_write_leaves_no_blob(svc: ServiceEnv, notes: Path) -> None:
    error = refuse(files_of(svc, notes), svc, notes, attrs={"pages": 3})
    assert error.code == "write.invalid"
    # Validation runs under the lock, after the promote: an object store keeps the
    # authorized but invalid add's object as an orphan until gc (object storage O6).
    assert (svc.store.blob(DIGEST) is None) is (svc.backend != "s3")
    assert staging(svc) == []
    assert svc.head_seq() == 0


def test_too_large(svc: ServiceEnv, notes: Path) -> None:
    error = refuse(files_of(svc, notes, max_bytes=10), svc, notes)
    assert (error.code, error.details) == ("file.too_large", {"max_bytes": 10})
    assert staging(svc) == []
    dry = refuse(files_of(svc, notes, max_bytes=10), svc, notes, dry_run=True)
    assert dry.code == "file.too_large"


def test_unreadable_inputs(svc: ServiceEnv, notes: Path, tmp_path: Path) -> None:
    service = files_of(svc, notes)
    for path in (notes.parent, tmp_path / "absent.pdf"):
        error = refuse(service, svc, path)
        assert (error.code, error.details) == ("file.unreadable", {"argument": "path"})


def test_media_type_override_is_checked(svc: ServiceEnv, notes: Path) -> None:
    service = files_of(svc, notes)
    error = refuse(service, svc, notes, media_type="PDF")
    assert (error.code, error.details["argument"]) == ("usage.invalid_argument", "--media-type")
    result = add(service, svc, notes, media_type="application/x-scan")
    assert result.file["media_type"] == "application/x-scan"


def test_origin_needs_a_root(svc: ServiceEnv, notes: Path, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("plain text\n")
    result = add(files_of(svc, notes), svc, outside)
    assert result.file["origin"] is None
    assert result.file["media_type"] == "text/plain"
    assert [w.code for w in result.warnings] == ["file.no_root"]
    skipped = add(files_of(svc, notes), svc, notes, no_origin=True)
    assert skipped.file["origin"] is None and skipped.warnings == ()


def test_path_and_check_signal_the_files_they_read(svc: ServiceEnv, notes: Path) -> None:
    file_id = str(add(files_of(svc, notes), svc, notes).entity["id"])
    activity = RecordingActivity()
    reader = svc.make_reader()
    service = svc.make_files(reader=reader, activity=activity)
    request = RequestMeta("file check", hash_value({}))
    view = reader.open(svc.actor, svc.entry, "read.get", request)
    service.check(view, [], limit=50, offset=0, actor=svc.actor, request=request)
    service.check(view, ["FILE-0001"], limit=50, offset=0, actor=svc.actor, request=request)
    graph = str(svc.entry.id)
    assert activity.signals == [(graph, [file_id]), (graph, [file_id])]
    if svc.backend == "files":
        service.path(view, "FILE-0001", origin=False, actor=svc.actor, request=request)
        assert activity.signals[-1] == (graph, [file_id])
    else:  # no local path and no file cache: file.no_local_path, nothing was read
        with pytest.raises(GraphCliError) as caught:
            service.path(view, "FILE-0001", origin=False, actor=svc.actor, request=request)
        assert caught.value.code == "file.no_local_path"
        assert len(activity.signals) == 2
    service.open(view, "FILE-0001", actor=svc.actor, request=request)
    assert activity.signals[-1] == (graph, [file_id])


def test_open_streams_the_stored_bytes(svc: ServiceEnv, notes: Path) -> None:
    """One way to read a stored file (O8): metadata, the local path when there is one, and
    the bytes, started before the first one is handed out."""
    add(files_of(svc, notes), svc, notes)
    reader = svc.make_reader()
    service = svc.make_files(reader=reader)
    request = RequestMeta("file path", hash_value({}))
    view = reader.open(svc.actor, svc.entry, "read.get", request)
    opened, warnings = service.open(view, "FILE-0001", actor=svc.actor, request=request)
    assert warnings == []
    assert (opened.hash, opened.size, opened.media_type, opened.file_name) == (
        DIGEST,
        len(PDF),
        "application/pdf",
        "report 2024.pdf",
    )
    assert (opened.local_path is not None) is (svc.backend == "files")
    assert b"".join(opened.start()) == PDF
    answer = service.answer(opened)
    assert answer["path"] is None and answer["hash"] == DIGEST and answer["size"] == len(PDF)


def test_path_answers_a_cached_copy_without_a_local_path(
    svc: ServiceEnv, notes: Path, tmp_path: Path
) -> None:
    """``file path`` in local mode with an object store: a read-only copy in this machine's
    file cache, reused while it has its hash; the backend's own file where there is one."""
    add(files_of(svc, notes), svc, notes)
    reader = svc.make_reader()
    clock = FixedClock()
    cache = StateFileCache(tmp_path / "state", clock)
    service = svc.make_files(reader=reader, cache=cache)
    request = RequestMeta("file path", hash_value({}))
    view = reader.open(svc.actor, svc.entry, "read.get", request)
    data, _ = service.path(view, "FILE-0001", origin=False, actor=svc.actor, request=request)
    path = Path(str(data["path"]))
    if svc.backend == "files":
        assert path.is_relative_to(svc.root) and not cache.root.exists()
        return
    assert path == cache.path(str(svc.entry.id), DIGEST)
    assert path.read_bytes() == PDF
    assert stat.S_IMODE(path.stat().st_mode) == 0o400
    gets = fetches(svc)
    clock.advance(timedelta(hours=1))
    again, _ = service.path(view, "FILE-0001", origin=False, actor=svc.actor, request=request)
    assert again["path"] == str(path) and fetches(svc) == gets  # reused, not fetched
    assert path.stat().st_mtime == clock.now().timestamp()


def fetches(svc: ServiceEnv) -> int:
    """GET requests of stored files the fake object store answered (0 without one)."""
    fake: FakeS3 | None = getattr(svc, "fake", None)
    if fake is None:
        return 0
    return len([c for c in fake.calls("GET") if "/blobs/" in (c.key or "")])


def test_the_server_answers_no_path(svc: ServiceEnv, notes: Path) -> None:
    """On the server (``paths=False``) the stored copy's path is null and no bytes are read."""
    add(files_of(svc, notes), svc, notes)
    reader = svc.make_reader()
    service = svc.make_files(reader=reader, paths=False)
    request = RequestMeta("file path", hash_value({}))
    view = reader.open(svc.actor, svc.entry, "read.get", request)
    data, _ = service.path(view, "FILE-0001", origin=False, actor=svc.actor, request=request)
    assert data["path"] is None and data["hash"] == DIGEST


def test_writes_to_file_entities_need_the_object_store(
    svc: ServiceEnv, notes: Path, tmp_path: Path
) -> None:
    """Every revision of a file lists its blob, so the commit check asks the object store
    even for a title change (O4); writes that touch no file and file-free reads do not."""
    if svc.backend != "s3":
        pytest.skip("an object store only")
    add(files_of(svc, notes), svc, notes)
    svc.catalog = FileCatalog(svc.root)  # the installation lost its [files.s3]
    svc.service = svc.make_service()
    with pytest.raises(GraphCliError) as caught:
        svc.apply({"op": "update", "id": "FILE-0001", "set": {"title": "renamed"}})
    assert caught.value.code == "backend.unavailable"
    assert str(svc.entry.id) in caught.value.message
    assert svc.head_seq() == 1
    svc.create("nas:Note")
    assert svc.head_seq() == 2
    assert svc.snapshot().seq == 2
