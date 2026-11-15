"""S: ``[files] store``, ``[files.s3]`` and manifest format 2 through both programs.

The settings reach the app's catalog and graph-admin's catalog and maintenance port.
A graph whose manifest names an object store opens without them: ``graph find`` and
``graph-admin graph show`` touch no file and work, while a blob access such as
``graph-admin files verify`` is refused with what is missing named, or, against a bucket
without the graph's store marker, as "not in that bucket" (never as missing files). An
installation that does not use an object store never meets ``[files.s3]``.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from harness import CliRunner
from support.admin import admin, init
from support.app import app, create_graph
from support.fake_s3 import ACCESS_KEY_ID, SECRET_ACCESS_KEY, FakeS3
from support.output_schemas import validator

from graph_cli.kernel.ports.catalog import StoreInfo
from graph_cli.storage.files.layout import GraphLayout
from graph_cli.storage.files.manifest import format_manifest
from graph_cli.storage.files.releases import read_manifest
from graph_cli.storage.files.store_marker import MARKER_KEY
from graph_cli_admin.maintenance_cli.commands.graph import CREATE_OUTPUT
from graph_cli_admin.maintenance_cli.output_schemas import DEFS
from graph_cli_admin.services_admin.graphs import describe

STORE_ID = "4b0c9f3e-2d1a-4c5b-9e8f-7a6b5c4d3e2f"
SECRET = "example/secret+value"
S3_ENV = {
    "GRAPH_CLI_S3_BUCKET": "example-bucket",
    "AWS_REGION": "example-region",
    "AWS_ACCESS_KEY_ID": "EXAMPLEKEYID0000",
    "AWS_SECRET_ACCESS_KEY": SECRET,
}


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    return init(run_cli, isolated_env)


def config_file(env: dict[str, str]) -> Path:
    return Path(env["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"


def append_config(env: dict[str, str], text: str) -> None:
    path = config_file(env)
    path.write_text(path.read_text() + text)


def move_to_object_store(root: Path, graph_id: str) -> None:
    layout = GraphLayout(root / "graphs" / graph_id)
    current = read_manifest(layout)[0]
    layout.manifest.write_bytes(
        format_manifest(replace(current, store=StoreInfo("files", 1, "s3", STORE_ID)))
    )


def test_graph_show_names_the_blob_location(run_cli: CliRunner, root: Path) -> None:
    graph_id = create_graph(run_cli, "example_graph")
    shown = admin(run_cli, "graph", "show", "example_graph")
    assert shown.data["store"] == {
        "backend": "files",
        "format_version": 1,
        "blob_location": "local",
        "store_id": None,
    }
    manifest = (root / "graphs" / graph_id / "manifest.toml").read_text()
    assert manifest.startswith("format_version = 1\n\n[graph]\n")
    assert "requires" not in manifest
    assert "[store.blobs]" not in manifest


def test_the_manifest_description_carries_an_object_store(root: Path, run_cli: CliRunner) -> None:
    """``graph create`` and ``graph show`` answer the location in their closed schema."""
    graph_id = create_graph(run_cli, "example_graph")
    move_to_object_store(root, graph_id)
    data = describe(read_manifest(GraphLayout(root / "graphs" / graph_id))[0])
    assert data["store"] == {
        "backend": "files",
        "format_version": 1,
        "blob_location": "s3",
        "store_id": STORE_ID,
    }
    assert list(validator(CREATE_OUTPUT, DEFS).iter_errors(data)) == []


def test_a_local_installation_never_meets_files_s3(
    run_cli: CliRunner, root: Path, isolated_env: dict[str, str]
) -> None:
    create_graph(run_cli, "example_graph")
    append_config(
        isolated_env, '[files.s3]\nbucket = "Not_A_Bucket"\nendpoint_url = "http://x.example"\n'
    )
    assert admin(run_cli, "graph", "show", "example_graph").code == 0
    found = app(run_cli, "find", "--graph", "example_graph", "--type", "inv:Location")
    assert found.code == 0, found.envelope


def test_store_s3_with_an_invalid_files_s3_stops_every_command(
    run_cli: CliRunner, root: Path, isolated_env: dict[str, str]
) -> None:
    create_graph(run_cli, "example_graph")
    append_config(isolated_env, '[files]\nstore = "s3"\n')
    for outcome in (admin(run_cli, "graph", "list"), app(run_cli, "graphs")):
        assert (outcome.code, outcome.error["code"]) == (5, "config.invalid")
        assert outcome.error["details"]["key"] == "files.s3.bucket"
    assert admin(run_cli, "graph", "list", env={**isolated_env, **S3_ENV}).code == 0
    assert app(run_cli, "graphs", env={**isolated_env, **S3_ENV}).code == 0


def test_a_secret_pasted_into_files_s3_is_refused(
    run_cli: CliRunner, root: Path, isolated_env: dict[str, str]
) -> None:
    append_config(isolated_env, f'[files.s3]\nsecret_access_key = "{SECRET}"\n')
    for outcome in (admin(run_cli, "graph", "list"), app(run_cli, "graphs")):
        assert (outcome.code, outcome.error["code"]) == (5, "config.invalid")
        assert outcome.error["details"]["key"] == "files.s3.secret_access_key"
        assert SECRET not in str(outcome.envelope)


def test_a_graph_in_an_object_store_is_refused_on_blob_access_only(
    run_cli: CliRunner, root: Path, isolated_env: dict[str, str]
) -> None:
    graph_id = create_graph(run_cli, "example_graph")
    move_to_object_store(root, graph_id)
    with FakeS3() as fake:
        fake_env = {
            **isolated_env,
            **S3_ENV,
            "GRAPH_CLI_S3_BUCKET": fake.bucket,
            "AWS_REGION": fake.region,
            "AWS_ENDPOINT_URL_S3": fake.endpoint_url,
            "AWS_ACCESS_KEY_ID": ACCESS_KEY_ID,
            "AWS_SECRET_ACCESS_KEY": SECRET_ACCESS_KEY,
        }
        for env in (isolated_env, fake_env):
            shown = admin(run_cli, "graph", "show", "example_graph", env=env)
            assert shown.data["store"]["store_id"] == STORE_ID, shown.envelope
            found = app(
                run_cli, "find", "--graph", "example_graph", "--type", "inv:Location", env=env
            )
            assert found.code == 0, found.envelope
        verify = ("files", "verify", "--graph", "example_graph")
        missing = admin(run_cli, *verify)  # a finding, not a stop (step 6)
        assert (missing.code, missing.error["code"]) == (1, "admin.integrity_errors")
        (finding,) = missing.error["details"]["findings"]
        assert finding["code"] == "integrity.storage.blob_store_unavailable", finding
        details = finding["actual"]
        assert (details["code"], details["store_id"], details["cause"], details["key"]) == (
            "backend.unavailable",
            STORE_ID,
            "config.invalid",
            "files.s3.bucket",
        )
        foreign = admin(run_cli, *verify, env=fake_env)  # the bucket has no store marker
        assert foreign.error["code"] == "admin.integrity_errors", foreign.envelope
        (finding,) = foreign.error["details"]["findings"]
        assert "not in that bucket" in finding["message"]
        assert graph_id in finding["message"]
        fake.put(
            MARKER_KEY,
            json.dumps(
                {"schema": "graph_cli.object_store/1", "store_id": STORE_ID, "created_at": "x"}
            ).encode(),
        )
        clean = admin(run_cli, *verify, env=fake_env)
        assert clean.code == 0, clean.envelope
        for outcome in (missing, foreign, clean):
            assert SECRET not in str(outcome.envelope)
            assert SECRET_ACCESS_KEY not in str(outcome.envelope)
            assert ACCESS_KEY_ID not in str(outcome.envelope)
