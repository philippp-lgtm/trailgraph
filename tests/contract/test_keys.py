"""``graph-admin keys verify|gc|backup|rotate-kek`` (plan T16, step 7) through the admin service.

The key store of a sealed graph is checked against its records, its stored files
and its purges; orphan keys go once they are an hour old, the keys of stored
files only after the files; a backup is a loadable copy; a KEK rotation rewraps
every live key, after which the earlier KEK is needed nowhere.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from support.admin_context import admin_context, key_settings
from support.clock import StepClock
from support.services import ServiceEnv

from graph_cli.catalog.file_catalog import FileCatalog
from graph_cli.kernel.clock import SystemClock
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, Uuid7Provider
from graph_cli.kernel.records import RequestMeta
from graph_cli.services.files import FileAddRequest
from graph_cli.storage.files.keyrings import KeyRings, KeySettings
from graph_cli.storage.files.keystore import KeyStore, create_kek_file, read_kek_file
from graph_cli.storage.files.maintenance import FileStoreMaintenance
from graph_cli_admin.services_admin import audit, files, keys
from graph_cli_admin.services_admin.context import AdminContext

CONTENT = b"%PDF-1.4\n% synthetic key test\n%%EOF\n"
ORPHAN = "integrity.keys.orphan"
KEK_NOT_CURRENT = "integrity.keys.kek_not_current"


@pytest.fixture
def svc(tmp_path: Path) -> ServiceEnv:
    return ServiceEnv("files-sealed", "notes_and_sources", tmp_path / "root", version="1.1.0")


@pytest.fixture
def ctx(svc: ServiceEnv) -> AdminContext:
    return admin_context(svc.root)


def later(ctx: AdminContext) -> AdminContext:
    """The context two hours on: every key created so far is older than an hour."""
    return replace(ctx, clock=StepClock(datetime.now(UTC) + timedelta(hours=2)))


def with_keys(ctx: AdminContext, settings: KeySettings) -> AdminContext:
    """The context with other ``[keys]`` settings, as after a config change."""
    clock = SystemClock()
    rings = KeyRings(settings, clock=clock, ids=Uuid7Provider(clock))
    root = ctx.roots.data_root
    return replace(
        ctx,
        catalog=FileCatalog(root, rings=rings),
        maintenance=FileStoreMaintenance(root, rings=rings),
    )


def add_file(svc: ServiceEnv, tmp_path: Path) -> EntityId:
    path = tmp_path / f"input-{len(list(tmp_path.glob('input-*')))}.pdf"
    path.write_bytes(CONTENT)
    request = RequestMeta.of("file add", {"args": {"path": path.name}})
    result = svc.make_files().add(FileAddRequest(svc.actor, svc.name, path, request))
    return EntityId(UUID(str(result.entity["id"])))


def codes(data: dict[str, object]) -> list[str]:
    findings = data["findings"]
    assert isinstance(findings, list)
    return sorted(f["code"] for f in findings)


def verify_codes(ctx: AdminContext) -> list[str]:
    verification = audit.verify(ctx)
    verification.raise_on_errors()
    return sorted(f.code for f in verification.findings)


def test_a_clean_key_store_verifies_clean(svc: ServiceEnv, ctx: AdminContext) -> None:
    svc.apply({"op": "create", "type": "nas:Topic", "attrs": {"label": "a topic"}})
    data = keys.verify(ctx, graph_ref=svc.name).data
    assert codes(data) == []
    assert data["kek_id"] == read_kek_file(key_settings(svc.root).kek_file).kek_id
    assert data["live"] == {data["kek_id"]: 2}  # the entity's key and the tx's


def test_orphan_keys_go_once_they_are_an_hour_old(
    svc: ServiceEnv, ctx: AdminContext, tmp_path: Path
) -> None:
    add_file(svc, tmp_path)
    add_file(svc, tmp_path)  # known content: the staged copy and its key are discarded
    assert codes(keys.verify(ctx, graph_ref=svc.name).data) == [ORPHAN]

    young = keys.gc(ctx, graph_ref=svc.name, reason=None, dry_run=False).data
    assert (young["keys_removed"], young["keys_kept"], young["removed"]) == ([], 1, False)
    events = len(svc.graph_events())
    dry = keys.gc(later(ctx), graph_ref=svc.name, reason=None, dry_run=True).data
    assert (len(dry["keys_removed"]), dry["removed"]) == (1, False)
    assert len(svc.graph_events()) == events

    done = keys.gc(later(ctx), graph_ref=svc.name, reason="cleanup", dry_run=False).data
    assert done["keys_removed"] == dry["keys_removed"] and done["removed"]
    (event,) = [e for e in svc.graph_events() if e["actions"] == ["maint.repair"]]
    assert event["change"] == {"keys_removed": done["keys_removed"]}
    assert codes(keys.verify(ctx, graph_ref=svc.name).data) == []
    assert files.verify(ctx, graph_ref=svc.name, rehash=True, decrypt=True).data["counts"] == {
        "error": 0,
        "warning": 0,
        "info": 0,
    }
    assert verify_codes(ctx) == []
    again = keys.gc(later(ctx), graph_ref=svc.name, reason=None, dry_run=False).data
    assert (again["keys_removed"], again["removed"]) == ([], False)


def test_the_key_of_a_stored_file_stays_until_files_gc_removes_it(
    svc: ServiceEnv, ctx: AdminContext
) -> None:
    staged = svc.store.stage_blob([b"left behind by a crash"], max_bytes=1 << 20)
    with ctx.maintenance.maintenance_session(svc.entry.id, timeout_s=5) as session:
        session.publish_blob(staged)  # stored, but no tx lists it
    spared = keys.gc(later(ctx), graph_ref=svc.name, reason=None, dry_run=False).data
    assert (spared["keys_removed"], spared["keys_kept"]) == ([], 0)
    assert codes(keys.verify(ctx, graph_ref=svc.name).data) == []

    assert files.gc(ctx, graph_ref=svc.name, reason=None, dry_run=False).data["removed"]
    assert codes(keys.verify(ctx, graph_ref=svc.name).data) == [ORPHAN]
    done = keys.gc(later(ctx), graph_ref=svc.name, reason=None, dry_run=False).data
    assert len(done["keys_removed"]) == 1


def test_a_backup_is_a_loadable_copy_of_each_key_store(
    svc: ServiceEnv, ctx: AdminContext, tmp_path: Path
) -> None:
    add_file(svc, tmp_path)
    target = tmp_path / "backups" / "2026-11-05"
    data = keys.backup(ctx, graph_ref=None, to=target)
    copy = target / f"{svc.entry.id}.sqlite"
    assert data == {
        "dir": str(target),
        "graphs": [{"graph_id": str(svc.entry.id), "name": svc.name, "file": str(copy)}],
    }
    assert sorted(p.name for p in target.iterdir()) == [copy.name]
    assert (target.stat().st_mode & 0o777, copy.stat().st_mode & 0o777) == (0o700, 0o600)
    kek = read_kek_file(key_settings(svc.root).kek_file)
    clock = SystemClock()
    with KeyStore.load(copy, svc.entry.id, (kek,), clock=clock, ids=Uuid7Provider(clock)) as loaded:
        assert sum(loaded.wrapping().live.values()) == 3  # entity, tx and file key

    with pytest.raises(GraphCliError, match="already exists") as caught:
        keys.backup(ctx, graph_ref=svc.name, to=target)
    assert caught.value.code == "usage.invalid_argument"


def test_a_kek_rotation_rewraps_every_key_and_retires_the_earlier_kek(
    svc: ServiceEnv, ctx: AdminContext, tmp_path: Path
) -> None:
    add_file(svc, tmp_path)
    earlier = key_settings(svc.root)
    new_kek = create_kek_file(tmp_path / "secrets" / "kek-2026-11")
    rotating = with_keys(
        ctx,
        replace(
            earlier,
            kek_file=tmp_path / "secrets" / "kek-2026-11",
            previous_kek_file=earlier.kek_file,
        ),
    )
    before = keys.verify(rotating, graph_ref=svc.name).data
    assert codes(before) == [KEK_NOT_CURRENT]  # a warning: the command succeeds
    old_id = read_kek_file(earlier.kek_file).kek_id
    assert before["live"] == {old_id: 3}

    dry = keys.rotate_kek(rotating, graph_ref=None, reason=None, dry_run=True)
    assert dry["kek_id"] == new_kek.kek_id
    assert dry["graphs"] == [
        {
            "graph_id": str(svc.entry.id),
            "name": svc.name,
            "rewrapped": 3,
            "retired": [old_id],
            "rotated": False,
        }
    ]
    done = keys.rotate_kek(rotating, graph_ref=svc.name, reason="yearly rotation", dry_run=False)
    assert done["graphs"][0]["rotated"] and done["graphs"][0]["rewrapped"] == 3
    (event,) = [e for e in svc.graph_events() if e["actions"] == ["maint.graph_configure"]]
    assert event["change"] == {"kek": {"from": [old_id], "to": new_kek.kek_id}}

    # the earlier KEK is needed nowhere now
    after = with_keys(ctx, KeySettings(earlier.dir, tmp_path / "secrets" / "kek-2026-11"))
    data = keys.verify(after, graph_ref=svc.name).data
    assert (codes(data), data["live"]) == ([], {new_kek.kek_id: 3})
    assert (
        files.verify(after, graph_ref=svc.name, rehash=False, decrypt=True).data["counts"]["error"]
        == 0
    )
    assert verify_codes(after) == []
    events = len(svc.graph_events())
    again = keys.rotate_kek(after, graph_ref=None, reason=None, dry_run=False)
    assert again["graphs"][0] == {**dry["graphs"][0], "rewrapped": 0, "retired": []}
    assert len(svc.graph_events()) == events


def test_a_rotation_without_the_earlier_kek_changes_nothing(
    svc: ServiceEnv, ctx: AdminContext, tmp_path: Path
) -> None:
    svc.apply({"op": "create", "type": "nas:Topic", "attrs": {"label": "a topic"}})
    earlier = key_settings(svc.root)
    create_kek_file(tmp_path / "kek-new")
    alone = with_keys(ctx, KeySettings(earlier.dir, tmp_path / "kek-new"))
    events = len(svc.graph_events())
    with pytest.raises(GraphCliError, match="previous_kek_file") as caught:
        keys.rotate_kek(alone, graph_ref=None, reason=None, dry_run=False)
    assert caught.value.code == "config.invalid"
    assert len(svc.graph_events()) == events
    assert codes(keys.verify(ctx, graph_ref=svc.name).data) == []


def test_a_graph_in_the_clear_has_no_key_store(tmp_path: Path) -> None:
    plain = ServiceEnv("files", "notes_and_sources", tmp_path / "root", version="1.1.0")
    ctx = admin_context(plain.root)
    for call in (
        lambda: keys.verify(ctx, graph_ref=plain.name),
        lambda: keys.gc(ctx, graph_ref=plain.name, reason=None, dry_run=False),
        lambda: keys.rotate_kek(ctx, graph_ref=plain.name, reason=None, dry_run=False),
        lambda: keys.backup(ctx, graph_ref=plain.name, to=tmp_path / "a"),
    ):
        with pytest.raises(GraphCliError) as caught:
            call()
        assert caught.value.code == "admin.graph_not_sealed"
    assert not (tmp_path / "a").exists()
    assert keys.rotate_kek(ctx, graph_ref=None, reason=None, dry_run=False)["graphs"] == []
    assert keys.backup(ctx, graph_ref=None, to=tmp_path / "b") == {
        "dir": str(tmp_path / "b"),
        "graphs": [],
    }
