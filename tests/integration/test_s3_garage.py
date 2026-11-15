"""I: stored files in a real S3-compatible object store (object storage O10).

Opt-in: skipped unless ``GRAPH_CLI_TEST_S3_ENDPOINT`` is set, so the normal suite and the
image build never need Docker or a network. ``tests/integration/run_garage.sh`` starts the
pinned Garage image, creates a bucket, a key and a small quota and runs this module; any
other S3-compatible store works with the same variables:

- ``GRAPH_CLI_TEST_S3_ENDPOINT``: ``http(s)://host[:port]``; plain ``http://`` to a host that
  is not loopback is allowed here with ``[files.s3] allow_plain_http``
- ``GRAPH_CLI_TEST_S3_BUCKET``, ``GRAPH_CLI_TEST_S3_REGION``,
  ``GRAPH_CLI_TEST_S3_ACCESS_KEY_ID``, ``GRAPH_CLI_TEST_S3_SECRET_ACCESS_KEY``: a key with
  read and write on the bucket
- ``GRAPH_CLI_TEST_S3_QUOTA_BYTES`` (optional): the bucket's size quota; the quota scenario
  adds one file larger than it and is skipped without it

Every test works under a prefix of its own (its own store marker) and removes what it put
there. The 24-hour gc grace period cannot pass against a real store: the process scenario
asserts that a young orphan is kept, the in-process one lowers the grace period to zero to
see a past-grace orphan removed by the store's own clock.
"""

import http.client
import io
import os
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from harness import CliRunner
from support.admin import admin, init
from support.admin_context import admin_context
from support.app import app
from support.clock import FixedClock
from support.fixtures import ONTOLOGIES
from support.move_scene import add_file, move
from support.policies import write_policy
from support.s3_graphs import blob_key
from support.serve import client_env, graph_server
from support.server import SERVER_POLICY

from graph_cli.catalog.file_catalog import FileCatalog
from graph_cli.kernel.canonical import hash_bytes
from graph_cli.kernel.errors import GraphCliError
from graph_cli.storage.files import s3_blobs
from graph_cli.storage.files.maintenance import FileStoreMaintenance
from graph_cli.storage.files.store_marker import MARKER_KEY, reset_identity_cache
from graph_cli.storage.s3.client import ObjectStoreClient
from graph_cli.storage.s3.settings import ObjectStoreCredentials, ObjectStoreSettings
from graph_cli_admin.services_admin import files, graphs, store_check

ENDPOINT = os.environ.get("GRAPH_CLI_TEST_S3_ENDPOINT", "")

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not ENDPOINT, reason="set GRAPH_CLI_TEST_S3_ENDPOINT (run_garage.sh)"),
]

NOTES = ONTOLOGIES / "notes_and_sources" / "1.1.0"
BIG = 2 * 1024 * 1024 + 12345  # several read chunks, so the verified stream holds back a tail


@dataclass(frozen=True)
class Store:
    """The bucket of the test run and the prefix of one test."""

    settings: ObjectStoreSettings

    @property
    def client(self) -> ObjectStoreClient:
        return ObjectStoreClient(self.settings)

    def env(self, base: dict[str, str]) -> dict[str, str]:
        credentials = self.settings.credentials
        assert credentials is not None
        return {
            **base,
            "GRAPH_CLI_S3_BUCKET": self.settings.bucket,
            "AWS_REGION": self.settings.region,
            "AWS_ENDPOINT_URL_S3": ENDPOINT,
            "AWS_ACCESS_KEY_ID": credentials.access_key_id,
            "AWS_SECRET_ACCESS_KEY": credentials.secret_access_key,
        }

    def config(self) -> str:
        """``[files] store = "s3"`` and the non-secret ``[files.s3]`` keys (the rest comes
        from the environment, as in a deployment)."""
        lines = [
            '\n[files]\nstore = "s3"\n',
            "[files.s3]\n",
            f'prefix = "{self.settings.prefix}"\n',
        ]
        if self.settings.allow_plain_http:
            lines.append("allow_plain_http = true\n")
        return "".join(lines)

    def objects(self, under: str = "") -> list[str]:
        return sorted(entry.key for entry in self.client.list_objects_v2(under))

    def blob_keys(self, graph_id: str) -> list[str]:
        return self.objects(f"graphs/{graph_id}/blobs/")


def plain_http_elsewhere(endpoint: str) -> bool:
    parts = urlsplit(endpoint)
    return parts.scheme == "http" and parts.hostname not in ("127.0.0.1", "localhost", "::1")


@pytest.fixture
def store() -> Iterator[Store]:
    """A prefix of this test in the run's bucket; everything under it is removed after."""
    settings = ObjectStoreSettings(
        bucket=os.environ["GRAPH_CLI_TEST_S3_BUCKET"],
        region=os.environ.get("GRAPH_CLI_TEST_S3_REGION", "garage"),
        prefix=f"graph_cli_test/{uuid.uuid4().hex}/",
        endpoint_url=ENDPOINT,
        allow_plain_http=plain_http_elsewhere(ENDPOINT),
        credentials=ObjectStoreCredentials(
            os.environ["GRAPH_CLI_TEST_S3_ACCESS_KEY_ID"],
            os.environ["GRAPH_CLI_TEST_S3_SECRET_ACCESS_KEY"],
        ),
    )
    found = Store(settings)
    for _ in range(30):  # a store that was just started may answer 503 for a moment
        try:
            found.objects()
            break
        except Exception:
            time.sleep(1)
    reset_identity_cache()
    yield found
    reset_identity_cache()
    client = found.client
    for key in found.objects():
        client.delete_object(key)


def installation(run_cli: CliRunner, env: dict[str, str], store: Store) -> Path:
    root = init(run_cli, env)
    config = Path(env["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.write_text(config.read_text() + store.config())
    return root


def payload(size: int, seed: int) -> bytes:
    block = hash_bytes(str(seed).encode()).encode()
    return (block * (size // len(block) + 1))[:size]


def put_orphan(store: Store, graph_id: str, data: bytes) -> str:
    """Bytes under the graph's prefix that no tx names, written past graph_cli."""
    digest = hash_bytes(data)
    key = blob_key(graph_id, digest)
    store.client.put_object(key, io.BytesIO(data), len(data), digest.removeprefix("sha256:"))
    return key


def checks(outcome: Any) -> dict[str, str]:
    return {c["name"]: c["status"] for c in outcome.data["checks"]}


def no_secret(store: Store, *outcomes: Any) -> None:
    credentials = store.settings.credentials
    assert credentials is not None
    for outcome in outcomes:
        text = str(outcome.envelope) + outcome.stderr
        assert credentials.secret_access_key not in text
        assert credentials.access_key_id not in text


def test_local_mode_add_read_verify_gc_move_and_quota(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path, store: Store
) -> None:
    env = store.env(isolated_env)
    root = installation(run_cli, env, store)

    checked = admin(run_cli, "files", "store", "check", env=env)
    assert checked.code == 0, checked.envelope
    assert checks(checked)["marker"] == "warning"  # no graph yet, no marker yet
    assert {s for n, s in checks(checked).items() if n != "marker"} == {"ok"}, checked.data

    created = admin(run_cli, "graph", "create", "example_graph", "--ontology", str(NOTES), env=env)
    assert created.code == 0, created.envelope
    graph_id = created.data["id"]
    store_id = created.data["store"]["store_id"]
    assert created.data["store"]["blob_location"] == "s3"
    assert MARKER_KEY in store.objects()
    checked = admin(run_cli, "files", "store", "check", env=env)
    assert checked.code == 0, checked.envelope
    assert set(checks(checked).values()) == {"ok"}, checked.data
    assert checked.data["store_id"] == store_id
    assert checked.data["graphs"] == [{"id": graph_id, "name": "example_graph"}]
    assert [k for k in store.objects() if k.startswith("graph_cli_probe/")] == []

    home = Path(env["HOME"])
    contents = {"small.bin": payload(1000, 1), "big.bin": payload(BIG, 2)}
    app_env = {**env, "GRAPH_CLI_GRAPH": "example_graph"}
    ids = {}
    for name, data in contents.items():
        (home / name).write_bytes(data)
        added = app(run_cli, "file", "add", str(home / name), env=app_env)
        assert added.code == 0, added.envelope
        assert added.data["file"]["hash"] == hash_bytes(data)
        ids[name] = added.data["entity"]["id"]
    assert store.blob_keys(graph_id) == sorted(
        blob_key(graph_id, hash_bytes(d)) for d in contents.values()
    )
    blobs = root / "graphs" / graph_id / "blobs"
    assert not blobs.exists() or not any(p.is_file() for p in blobs.rglob("*"))

    def read_back() -> None:
        for name, data in contents.items():
            shown = app(run_cli, "file", "path", ids[name], env=app_env)
            assert shown.code == 0, shown.envelope
            assert Path(shown.data["path"]).read_bytes() == data

    read_back()
    file_check = app(run_cli, "file", "check", env=app_env)
    assert file_check.code == 0, file_check.envelope

    orphan = put_orphan(store, graph_id, b"bytes no tx names\n")
    verified = admin(run_cli, "files", "verify", "--graph", "example_graph", "--rehash", env=env)
    assert verified.code == 0, verified.envelope
    assert [f["code"] for f in verified.data["findings"]] == ["integrity.storage.blob_orphan"]
    gc = admin(run_cli, "files", "gc", "--graph", "example_graph", env=env)
    assert gc.code == 0, gc.envelope
    assert orphan in store.objects()  # younger than the grace period by the store's clock
    audit = admin(run_cli, "audit", "verify", env=env)
    assert audit.code == 0 and audit.data["counts"]["error"] == 0, audit.envelope

    quota = os.environ.get("GRAPH_CLI_TEST_S3_QUOTA_BYTES")
    if quota:
        before = store.objects()
        (home / "huge.bin").write_bytes(payload(int(quota) + 1024 * 1024, 3))
        refused = app(run_cli, "file", "add", str(home / "huge.bin"), env=app_env)
        assert refused.code == 1, refused.envelope
        assert refused.error["code"] == "files.store_refused", refused.envelope
        assert "quota" in refused.error["message"]
        assert store.objects() == before
        no_secret(store, refused)

    # s3 -> local: the bucket's copies (the orphan too) go to the data root, gc removes them
    moved = admin(run_cli, "files", "move", "example_graph", "--to", "local", env=env)
    assert moved.code == 0, moved.envelope
    assert moved.data["graphs"][0]["status"] == "moved"
    audit = admin(run_cli, "audit", "verify", env=env)
    assert audit.code == 0 and audit.data["counts"]["error"] == 0, audit.envelope
    verified = admin(run_cli, "files", "verify", "--graph", "example_graph", "--rehash", env=env)
    assert verified.code == 0, verified.envelope
    gc = admin(run_cli, "files", "gc", "--graph", "example_graph", env=env)
    assert gc.code == 0, gc.envelope
    assert len(gc.data["misplaced_removed"]) == 3, gc.envelope
    assert store.blob_keys(graph_id) == []
    read_back()

    # local -> s3 again: the dry run copies nothing, the move does, gc empties blobs/
    planned = admin(run_cli, "files", "move", "example_graph", "--to", "s3", "--dry-run", env=env)
    assert planned.code == 0, planned.envelope
    assert store.blob_keys(graph_id) == []
    moved = admin(run_cli, "files", "move", "example_graph", "--to", "s3", env=env)
    assert moved.code == 0, moved.envelope
    (row,) = moved.data["graphs"]
    assert (row["status"], row["store_id"]) == ("moved", store_id)
    audit = admin(run_cli, "audit", "verify", env=env)
    assert audit.code == 0 and audit.data["counts"]["error"] == 0, audit.envelope
    verified = admin(run_cli, "files", "verify", "--graph", "example_graph", "--rehash", env=env)
    assert verified.code == 0, verified.envelope
    gc = admin(run_cli, "files", "gc", "--graph", "example_graph", env=env)
    assert gc.code == 0, gc.envelope
    assert len(gc.data["misplaced_removed"]) == 2, gc.envelope  # the orphan went in the last gc
    assert not any(p.is_file() for p in blobs.rglob("*") if not p.name.startswith(".tmp-"))
    assert len(store.blob_keys(graph_id)) == 2
    read_back()
    no_secret(store, checked, verified, gc, moved)


def get(port: int, path: str, token: str) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    conn.request("GET", path, headers={"Authorization": f"Bearer {token}"})
    response = conn.getresponse()
    body = response.read()
    conn.close()
    return response.status, dict(response.getheaders()), body


def test_remote_mode_add_and_download(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path, store: Store
) -> None:
    env = store.env(isolated_env)
    root = installation(run_cli, env, store)
    created = admin(run_cli, "graph", "create", "example_graph", "--ontology", str(NOTES), env=env)
    assert created.code == 0, created.envelope
    graph_id = created.data["id"]
    # with store = "s3" every command of this installation needs the store's settings
    policy = write_policy(tmp_path / "policies", "installation.yaml", SERVER_POLICY)
    applied = admin(run_cli, "policy", "apply", str(policy), "--reason", "serve", env=env)
    assert applied.code == 0, applied.envelope
    token = admin(run_cli, "token", "create", "ci", env=env).data["token"]
    data = payload(BIG, 4)
    with graph_server(env, tmp_path) as server:
        client = client_env(isolated_env, tmp_path / "client", server.port, token)
        client["GRAPH_CLI_GRAPH"] = "example_graph"
        source = tmp_path / "client" / "upload.bin"
        source.write_bytes(data)
        added = app(run_cli, "file", "add", str(source), env=client)
        assert added.code == 0, added.envelope
        entity = added.data["entity"]["id"]
        assert store.blob_keys(graph_id) == [blob_key(graph_id, hash_bytes(data))]

        shown = app(run_cli, "file", "path", entity, env=client)
        assert shown.code == 0, shown.envelope
        copy = Path(shown.data["path"])
        assert copy.is_relative_to(tmp_path / "client") and copy.read_bytes() == data

        status, headers, body = get(server.port, f"/v1/graphs/example_graph/files/{entity}", token)
        assert status == 200 and body == data
        assert headers["Graph-Cli-Content-Hash"] == hash_bytes(data)
    server_state = Path(env["XDG_STATE_HOME"]) / "graph_cli" / "files"
    assert not server_state.exists()  # the server streams, it keeps no copy
    blobs = root / "graphs" / graph_id / "blobs"
    assert not blobs.exists() or not any(p.is_file() for p in blobs.rglob("*"))
    verified = admin(run_cli, "files", "verify", "--graph", "example_graph", "--rehash", env=env)
    assert verified.code == 0 and verified.data["findings"] == [], verified.envelope
    audit = admin(run_cli, "audit", "verify", env=env)
    assert audit.code == 0 and audit.data["counts"]["error"] == 0, audit.envelope


def test_gc_removes_an_orphan_past_the_grace_period_by_the_store_clock(
    tmp_path: Path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    ctx = admin_context(root)
    settings = store.settings
    ctx = replace(
        ctx,
        catalog=FileCatalog(root, object_store=settings),
        maintenance=FileStoreMaintenance(
            root, syncer=ctx.maintenance.syncer, object_store=settings
        ),
    )
    created = graphs.create(
        ctx, name="example_graph", source=NOTES, module=None, title=None, language=None
    )
    graph_id = str(created.entry.id)
    source = tmp_path / "in" / "kept.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(payload(5000, 5))
    add_file(root, settings, source)
    assert move(ctx, "s3").error is None
    orphan = put_orphan(store, graph_id, b"an orphan to collect\n")

    young = files.gc(ctx, graph_ref="example_graph", reason=None, dry_run=False).data
    assert orphan in store.objects()
    assert young["blobs_removed"] == [] and len(young["misplaced_removed"]) == 1  # the local copy

    monkeypatch.setattr(s3_blobs, "GC_GRACE", timedelta(0))
    time.sleep(2)  # the store's Date and LastModified have whole seconds
    old = files.gc(ctx, graph_ref="example_graph", reason=None, dry_run=False).data
    assert old["blobs_removed"] == [{"hash": hash_bytes(b"an orphan to collect\n"), "size": 21}], (
        old
    )
    assert store.blob_keys(graph_id) == [blob_key(graph_id, hash_bytes(source.read_bytes()))]


def test_wrong_settings_name_the_fix_never_a_secret(
    run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path, store: Store
) -> None:
    """A wrong secret, an unknown key id, a missing bucket, a missing marker (another
    prefix) and missing credentials each stop the read with the documented code and words
    that name the fix: never a missing file, never a secret."""
    env = store.env(isolated_env)
    installation(run_cli, env, store)
    created = admin(run_cli, "graph", "create", "example_graph", "--ontology", str(NOTES), env=env)
    assert created.code == 0, created.envelope
    home = Path(env["HOME"])
    (home / "kept.bin").write_bytes(payload(3000, 6))
    app_env = {**env, "GRAPH_CLI_GRAPH": "example_graph"}
    entity = app(run_cli, "file", "add", str(home / "kept.bin"), env=app_env).data["entity"]["id"]
    config = Path(env["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    written = config.read_text()

    cases: dict[str, tuple[dict[str, str], str]] = {
        "wrong secret": ({"AWS_SECRET_ACCESS_KEY": "0" * 64}, "credentials"),
        "unknown key id": ({"AWS_ACCESS_KEY_ID": "GK" + "0" * 24}, "credentials"),
        "missing bucket": ({"GRAPH_CLI_S3_BUCKET": "graph-cli-no-such-bucket"}, "bucket"),
    }
    for case, (change, words) in cases.items():
        changed = {**app_env, **change}
        read = app(run_cli, "file", "path", entity, env=changed)
        assert read.error["code"] == "backend.unavailable", (case, read.envelope)
        assert words in read.error["message"], (case, read.envelope)
        check = admin(run_cli, "files", "store", "check", env=changed)
        assert check.error["code"] == "admin.store_check_failed", (case, check.envelope)
        assert words in check.error["message"], (case, check.envelope)
        no_secret(store, read, check)

    config.write_text(written.replace(store.settings.prefix, store.settings.prefix + "other/"))
    unmarked = app(run_cli, "file", "path", entity, env=app_env)
    config.write_text(written)
    assert unmarked.error["code"] == "backend.unavailable", unmarked.envelope
    assert "not in that bucket" in unmarked.error["message"], unmarked.envelope

    keyless = {
        k: v for k, v in app_env.items() if k not in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
    }
    missing = app(run_cli, "file", "path", entity, env=keyless)
    assert missing.error["code"] == "backend.unavailable", missing.envelope
    assert "AWS_SECRET_ACCESS_KEY" in missing.error["message"], missing.envelope
    no_secret(store, unmarked, missing)
    assert app(run_cli, "file", "path", entity, env=app_env).code == 0


def test_store_check_names_a_skewed_clock(tmp_path: Path, store: Store) -> None:
    """This machine's clock one hour behind the store's: the store may still accept the
    signatures (Garage accepts requests up to a day old and answers older ones with 400
    ``InvalidRequest``, not ``RequestTimeTooSkewed``), so ``files store check`` judges the
    skew from the store's ``Date`` and fails on it."""
    skewed = FixedClock(datetime.now(UTC) - timedelta(hours=1))
    root = tmp_path / "root"
    ctx = admin_context(root)
    ctx = replace(
        ctx,
        clock=skewed,
        maintenance=FileStoreMaintenance(
            root,
            syncer=ctx.maintenance.syncer,
            object_store=store.settings,
            client_factory=lambda settings: ObjectStoreClient(settings, clock=skewed),
        ),
    )
    with pytest.raises(GraphCliError) as caught:
        store_check.check(ctx)
    assert caught.value.code == "admin.store_check_failed", caught.value
    found = {c["name"]: c for c in caught.value.details["checks"]}  # type: ignore[union-attr]
    assert found["clock"]["status"] == "error", found
    assert "correct this machine's clock" in found["clock"]["message"]
    assert 3500 <= caught.value.details["skew_s"] <= 3700  # type: ignore[operator]
