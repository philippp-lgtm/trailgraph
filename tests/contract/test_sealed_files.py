"""Stored files of sealed graphs (plan T16, step 5) through the services, on both backends.

A sealed graph stores a file encrypted under its own key and names the blob by
the hash of the ciphertext; the ``content`` value keeps the content hash, and
the revision records which blob holds it.
"""

import hashlib
import os
import stat
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from support.clock import FixedClock
from support.services import ServiceEnv

from graph_cli.catalog.file_copies import KEEP_SECONDS, FileCopyDir
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId
from graph_cli.kernel.ports.audit import graph_stream
from graph_cli.kernel.records import RequestMeta, stored_blob
from graph_cli.services.files import FileAddRequest, FileAddResult, FileService

MARKER = b"FILEMARK5b1d"
CONTENT = b"%PDF-1.4\n% synthetic " + MARKER + b"\n%%EOF\n"
DIGEST = "sha256:" + hashlib.sha256(CONTENT).hexdigest()
NAME_MARKER = "NAMEMARK0a4c"


@pytest.fixture(params=("memory-sealed", "files-sealed"))
def svc(request: pytest.FixtureRequest, tmp_path: Path) -> ServiceEnv:
    return ServiceEnv(str(request.param), "notes_and_sources", tmp_path / "root", version="1.1.0")


@pytest.fixture
def files(tmp_path: Path) -> ServiceEnv:
    return ServiceEnv("files-sealed", "notes_and_sources", tmp_path / "root", version="1.1.0")


@pytest.fixture
def scan(tmp_path: Path) -> Path:
    path = tmp_path / "input" / f"{NAME_MARKER}.pdf"
    path.parent.mkdir()
    path.write_bytes(CONTENT)
    return path


def add(service: FileService, svc: ServiceEnv, path: Path, **kw: Any) -> FileAddResult:
    request = RequestMeta.of("file add", {"args": {"path": path.name}})
    return service.add(
        FileAddRequest(actor=svc.actor, graph=svc.name, path=path, request=request, **kw)
    )


def entity_of(result: FileAddResult) -> EntityId:
    return EntityId(UUID(str(result.entity["id"])))


def test_a_file_is_stored_under_its_ciphertext_and_found_by_its_content(
    svc: ServiceEnv, scan: Path
) -> None:
    service = svc.make_files()
    result = add(service, svc, scan)
    assert result.file["hash"] == DIGEST and result.file["size"] == len(CONTENT)
    (tx,) = svc.txs()
    (revision,) = tx.revisions
    assert revision.attrs["content"] == DIGEST
    name = stored_blob(revision, DIGEST)
    assert name != DIGEST and revision.blobs == {DIGEST: name}
    assert tx.blobs == (name,)
    assert b"".join(svc.store.read_blob(name)) == CONTENT
    assert svc.store.blob(DIGEST) is None
    (intent,) = [e for e in svc.audit.read(graph_stream(str(svc.entry.id))) if "change" in e]
    assert [b["hash"] for b in intent["change"]["blobs"]] == [name]  # never the content hash
    again = add(service, svc, scan)  # known content: the stored file, no new blob
    assert again.existing and again.entity["id"] == result.entity["id"]
    assert len(svc.txs()) == 1


def test_an_update_keeps_the_blob_and_a_foreign_content_hash_is_refused(
    svc: ServiceEnv, scan: Path
) -> None:
    entity = str(add(svc.make_files(), svc, scan).entity["id"])
    svc.apply({"op": "update", "id": entity, "set": {"title": "renamed"}})
    first, second = (tx.revisions[0] for tx in svc.txs())
    assert second.blobs == first.blobs and svc.txs()[1].blobs == svc.txs()[0].blobs
    # a content hash this request did not stage names no blob a sealed graph can find
    attrs = {"title": "t", "content": DIGEST, "media_type": "text/plain"}
    error = svc.refuse({"op": "create", "type": "files:File", "attrs": attrs})
    assert error.code == "write.invalid"
    findings: Any = error.details["findings"]
    assert "data.referential.blob_missing" in {f["code"] for f in findings}


def test_no_byte_of_the_file_or_its_name_reaches_the_data_root(
    files: ServiceEnv, scan: Path
) -> None:
    add(files.make_files(), files, scan)
    for path in files.root.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert MARKER not in data and NAME_MARKER.encode() not in data, path
            assert DIGEST.removeprefix("sha256:").encode() not in data, path


def test_path_hands_out_a_private_decrypted_copy(
    files: ServiceEnv, scan: Path, tmp_path: Path
) -> None:
    add(files.make_files(), files, scan)
    clock = FixedClock()
    copies = FileCopyDir(tmp_path / "state" / "files", clock)
    reader = files.make_reader()
    service = files.make_files(reader=reader, copies=copies)
    request = RequestMeta.of("file path", {"args": {}})
    view = reader.open(files.actor, files.entry, "read.get", request)
    data, _ = service.path(view, "FILE-0001", origin=False, actor=files.actor, request=request)
    path = Path(str(data["path"]))
    assert path == tmp_path / "state" / "files" / str(files.entry.id) / DIGEST[7:]
    assert path.read_bytes() == CONTENT and data["size"] == len(CONTENT)
    assert stat.S_IMODE(path.stat().st_mode) == 0o400
    again, _ = service.path(view, "FILE-0001", origin=False, actor=files.actor, request=request)
    assert again["path"] == str(path)
    # copies not used for a day go with the next use; a use renews a copy
    stale = path.parent / ("0" * 64)
    stale.write_bytes(b"old")
    old = clock.now().timestamp() - KEEP_SECONDS - 1
    os.utime(stale, (old, old))
    clock.advance(timedelta(seconds=1))
    service.path(view, "FILE-0001", origin=False, actor=files.actor, request=request)
    assert not stale.exists() and path.exists()


def test_without_copies_path_is_null_and_the_content_streams(svc: ServiceEnv, scan: Path) -> None:
    result = add(svc.make_files(), svc, scan)
    reader = svc.make_reader()
    service = svc.make_files(reader=reader)
    request = RequestMeta.of("file path", {"args": {}})
    view = reader.open(svc.actor, svc.entry, "read.get", request)
    data, _ = service.path(view, "FILE-0001", origin=False, actor=svc.actor, request=request)
    assert data["path"] is None and data["hash"] == DIGEST
    size, chunks = service.stored_content(svc.entry.id, entity_of(result), DIGEST)
    assert (size, b"".join(chunks)) == (len(CONTENT), CONTENT)
    with pytest.raises(GraphCliError) as info:
        service.stored_content(svc.entry.id, entity_of(result), "sha256:" + "0" * 64)
    assert info.value.code == "entity.not_found"
