"""FileService.add on both backends (files slice 1, step 4): create, dedupe, origin, refusals."""

import hashlib
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from support.services import RecordingActivity, ServiceEnv
from support.trees import tree_hash

from graph_cli.kernel.canonical import JsonValue
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


@pytest.fixture(params=["memory", "files"])
def svc(request: pytest.FixtureRequest, tmp_path: Path) -> ServiceEnv:
    return ServiceEnv(str(request.param), "notes_and_sources", tmp_path / "root", version="1.1.0")


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
            request=RequestMeta.of("file add", {"args": {"path": path.name}}),
            **kw,
        )
    )


def refuse(service: FileService, svc: ServiceEnv, path: Path, **kw: Any) -> GraphCliError:
    with pytest.raises(GraphCliError) as info:
        add(service, svc, path, **kw)
    return info.value


def staging(svc: ServiceEnv) -> list[str]:
    if not svc.backend.startswith("files"):
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
    before = tree_hash(svc.root) if svc.backend.startswith("files") else None
    result = add(files_of(svc, notes), svc, notes, dry_run=True)
    assert result.entity["id"] is None and result.entity["alias"] is None
    assert result.file["hash"] == DIGEST
    assert result.to_json()["dry_run"] is True
    assert svc.store.blob(DIGEST) is None
    assert svc.head_seq() == 0
    if before is not None:
        assert tree_hash(svc.root) == before


def test_a_refused_write_leaves_no_blob(svc: ServiceEnv, notes: Path) -> None:
    error = refuse(files_of(svc, notes), svc, notes, attrs={"pages": 3})
    assert error.code == "write.invalid"
    assert svc.store.blob(DIGEST) is None
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
    request = RequestMeta.of("file check", {"args": {}})
    view = reader.open(svc.actor, svc.entry, "read.get", request)
    service.check(view, [], limit=50, offset=0, actor=svc.actor, request=request)
    service.check(view, ["FILE-0001"], limit=50, offset=0, actor=svc.actor, request=request)
    graph = str(svc.entry.id)
    assert activity.signals == [(graph, [file_id]), (graph, [file_id])]
    if svc.backend.startswith("files"):
        service.path(view, "FILE-0001", origin=False, actor=svc.actor, request=request)
        assert activity.signals[-1] == (graph, [file_id])
    else:
        with pytest.raises(GraphCliError):  # file.no_local_path: nothing was read
            service.path(view, "FILE-0001", origin=False, actor=svc.actor, request=request)
        assert len(activity.signals) == 2
