"""C: derived file metadata is written by its extractor only, over the API (file metadata plan
M9, step 3): the same refusal as the CLI gives, as a 422 with the finding
``data.semantic.managed_class``, for a single-op command and for ``apply``; a document that names
the reserved source kind is ``changeset.malformed`` there too; a link from an ordinary entity to
a record is allowed."""

import io
import json
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import init
from support.managed_scene import GRAPH, MANAGED_CODE, Scene, build
from support.openapi import answer_validator, app_document
from support.server import call, grant_server, new_token

from graph_cli import __version__
from graph_cli.kernel.canonical import hash_bytes
from graph_cli_server.app import ServerApp, Settings

pytestmark = pytest.mark.slow


@pytest.fixture
def world(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path
) -> tuple[ServerApp, str, Scene]:
    root = init(run_cli, isolated_env)
    scene = build(run_cli, root, tmp_path / "work")
    grant_server(run_cli, tmp_path)
    token = new_token(run_cli, "pipeline")
    return ServerApp(Settings.load(isolated_env, tmp_path), log=io.StringIO()), token, scene


def command(world: tuple[ServerApp, str, Scene], name: str, **args: Any) -> Any:
    app, token, _ = world
    return call(
        app, "POST", f"/v1/commands/{name}", token=token, body={"args": {"--graph": GRAPH, **args}}
    )


def apply(world: tuple[ServerApp, str, Scene], document: dict[str, Any], *flags: str) -> Any:
    app, token, _ = world
    data = json.dumps(document).encode()
    uploaded = call(app, "PUT", "/v1/uploads", token=token, raw=data)
    assert uploaded.status == 200, uploaded.json
    assert uploaded.json["upload"] == hash_bytes(data)
    args: dict[str, Any] = {
        "file": {"upload": uploaded.json["upload"], "name": "doc.json"},
        "--graph": GRAPH,
    }
    args.update({flag: True for flag in flags})
    return call(app, "POST", "/v1/commands/apply", token=token, body={"args": args})


def finding_codes(reply: Any) -> list[str]:
    assert reply.status == 422, reply.json
    assert reply.error["code"] == "write.invalid", reply.error
    return [f["code"] for f in reply.error["details"]["findings"]]


def test_a_single_op_on_a_record_is_a_422(world: tuple[ServerApp, str, Scene]) -> None:
    scene = world[2]
    update = command(world, "update", ref=scene.pdf, **{"--set": ["page_count=9"]})
    assert finding_codes(update) == [MANAGED_CODE]
    retract = command(world, "retract", ref=scene.part, **{"--reason": "tidy up"})
    assert finding_codes(retract) == [MANAGED_CODE]
    spec = app_document(__version__, dist=False, enroll=False)
    validator = answer_validator(spec, "/v1/commands/update", "post", "422")
    assert not [e.message for e in validator.iter_errors(update.json)], update.json


def test_apply_refuses_record_ops_and_dry_runs_alike(world: tuple[ServerApp, str, Scene]) -> None:
    scene = world[2]
    document = {"ops": [{"op": "retract", "id": scene.pdf}], "reason": "tidy up"}
    assert finding_codes(apply(world, document)) == [MANAGED_CODE]
    assert finding_codes(apply(world, document, "--dry-run")) == [MANAGED_CODE]


def test_a_reconcile_of_a_record_type_by_a_plain_source_is_refused(
    world: tuple[ServerApp, str, Scene],
) -> None:
    scene = world[2]
    document = {
        "reason": "sync",
        "source": {"name": "format", "version": "1"},
        "reconcile": [
            {
                "type": "files:FormatMetadata",
                "key": "key",
                "under": scene.file,
                "fields": ["extractor"],
                "items": [],
            }
        ],
    }
    assert finding_codes(apply(world, document)) == [MANAGED_CODE]


@pytest.mark.doc_claim("security_model.records_by_extraction_only")
def test_a_document_cannot_name_the_reserved_source_kind(
    world: tuple[ServerApp, str, Scene],
) -> None:
    scene = world[2]
    document = {
        "source": {"name": "pdf", "kind": "extractor"},
        "ops": [{"op": "update", "id": scene.pdf, "set": {"page_count": 9}}],
        "reason": "forged",
    }
    reply = apply(world, document)
    assert reply.status == 400, reply.json
    assert reply.error["code"] == "changeset.malformed"
    assert reply.error["details"]["path"] == "source.kind"


def test_an_edge_from_an_ordinary_entity_to_a_record_is_allowed(
    world: tuple[ServerApp, str, Scene],
) -> None:
    scene = world[2]
    link = command(world, "link", ref=scene.file, rel="core:source", target=scene.pdf)
    assert link.status == 200, link.json
