"""A data root with a file and derived metadata, written the way an extraction run writes it.

The graph is ``example_graph`` on ``notes_and_sources`` 1.3.0 (two kinds of metadata imported).
The CLI adds the file; the records are written in process through the real write service by a
request whose source has the reserved kind ``extractor``, which no document can name.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from harness import CliRunner

from graph_cli.access_control.policy import BuiltinPolicySource
from graph_cli.catalog.file_catalog import FileCatalog
from graph_cli.kernel.canonical import hash_value
from graph_cli.kernel.clock import SystemClock
from graph_cli.kernel.ids import Uuid7Provider
from graph_cli.kernel.records import EnvInfo, RequestMeta, SourceRef
from graph_cli.services.write import WriteConfig, WriteRequest, WriteResult, WriteService
from graph_cli.storage.files.audit_log import FileAuditLog
from support.app import ENV, app, create_graph
from support.files import write_pdf
from support.services import app_actor

GRAPH = "example_graph"
MANAGED_CODE = "data.semantic.managed_class"


@dataclass(frozen=True)
class Scene:
    root: Path
    file: str
    pdf: str  # id of the pdf record
    sqlite: str  # id of the sqlite record
    part: str  # id of its part


def extractor_write(root: Path, ops: list[dict[str, Any]], name: str) -> WriteResult:
    """``ops`` as one write of the extraction run ``name``."""
    clock = SystemClock()
    service = WriteService(
        catalog=FileCatalog(root),
        audit=FileAuditLog(root, durability="fsync", lock_timeout_s=60),
        policy=BuiltinPolicySource(),
        clock=clock,
        ids=Uuid7Provider(clock),
        env=EnvInfo("<data root>", "flag"),
        config=WriteConfig(lock_timeout_s=60),
    )
    document = {"ops": ops, "reason": f"extract {name}@1"}
    changeset = service.parse(document)
    changeset = replace(changeset, source=SourceRef(name, "1", None, "extractor"))
    return service.apply(
        WriteRequest(
            app_actor(), GRAPH, changeset, False, RequestMeta("files extract", hash_value(document))
        )
    )


def record_ops(extractor: str, file: str, **attrs: Any) -> dict[str, Any]:
    return {
        "op": "create",
        "attrs": {
            "key": f"{extractor}/{file}",
            "extractor": extractor,
            "extractor_version": 1,
            "status": "ok",
            **attrs,
        },
        "edges": [{"rel": "files:metadata_of", "target": file}],
    }


def build(run_cli: CliRunner, root: Path, work: Path) -> Scene:
    create_graph(run_cli, GRAPH, "notes_and_sources", version="1.3.0")
    added = app(run_cli, "file", "add", str(write_pdf(work / "report.pdf")), "--no-origin", env=ENV)
    assert added.code == 0, added.envelope
    file = str(added.data["entity"]["id"])
    pdf = extractor_write(
        root, [{**record_ops("pdf", file, page_count=1), "type": "files_pdf:PdfMetadata"}], "pdf"
    )
    part = {
        "op": "create",
        "type": "files_sqlite:SqliteObject",
        "local": "table",
        "attrs": {"key": f"sqlite/{file}/orders", "name": "orders", "kind": "table"},
    }
    record = {
        **record_ops("sqlite", file, table_count=1),
        "type": "files_sqlite:SqliteMetadata",
        "edges": [
            {"rel": "files:metadata_of", "target": file},
            {"rel": "files_sqlite:has_object", "target": "@table"},
        ],
    }
    sqlite = extractor_write(root, [part, record], "sqlite")
    by_type = {e.type: str(e.id) for e in (*pdf.entities, *sqlite.entities)}
    return Scene(
        root,
        file,
        by_type["files_pdf:PdfMetadata"],
        by_type["files_sqlite:SqliteMetadata"],
        by_type["files_sqlite:SqliteObject"],
    )
