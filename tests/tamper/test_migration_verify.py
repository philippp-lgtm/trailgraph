"""T: the migration scenarios of ``integrity.audit.*`` (plan M4, M5; contracts B5.6, B5.12, R.6).

A graph created with real events, written under 1.0.0 and migrated to 2.0.0 the way the release
does it (release, marker, ``maint.migrate`` intent, ``migrate``, pin intent with the alias
prefixes, manifest switch, marker removed). One scenario per migration finding code; the
other audit codes have theirs in ``test_audit_verify.py``.
"""

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from support.audit_scene import AuditedScene
from support.fixtures import compiled_model
from support.graphs import pin_of

from graph_cli.audit import finding_codes as audit_codes
from graph_cli.kernel.canonical import JsonValue, hash_document, jsonl_line
from graph_cli.kernel.findings import Severity
from graph_cli.kernel.ids import GraphId
from graph_cli.storage.files.store import FileStore

Document = dict[str, JsonValue]
PREFIXES: Document = {
    "added": {},
    "moved": {"area_a:renamed": {"from": "area_a:old", "prefix": "R"}},
}


class MigratedScene(AuditedScene):
    graph_id: GraphId

    def audit_codes(self) -> list[str]:
        """What the audit verifier says; tx-log edits also make storage verify object."""
        return sorted(c for c in self.codes() if c.startswith("integrity.audit."))

    def store(self) -> FileStore:
        return FileStore(
            self.root / "graphs" / str(self.graph_id), self.graph_id, syncer=self.syncer
        )

    def rewrite_last_tx(self, change: Callable[[Document], None]) -> None:
        """Edit the last tx line (the migration) and seal it again."""
        store = self.store()
        segment = store.txlog.path(store.txlog.segments()[-1][1])
        lines = segment.read_bytes().splitlines(keepends=True)
        document: Document = json.loads(lines[-1])
        change(document)
        body = {k: v for k, v in document.items() if k != "hash"}
        body["hash"] = hash_document(body)
        lines[-1] = jsonl_line(body)
        segment.write_bytes(b"".join(lines))


def scene_after(tmp_path: Path, **flags: object) -> MigratedScene:
    """A graph with two write txs, then a migration of every entity with ``flags``."""
    scene = MigratedScene(tmp_path / "root")
    scene.graph_id = scene.create_audited()
    scene.write_entities(scene.graph_id, 3)
    scene.write_entities(scene.graph_id, 2)
    scene.migrate(scene.graph_id, **flags)  # type: ignore[arg-type]
    return scene


def revisions(document: Document) -> list[Document]:
    found = document["revisions"]
    assert isinstance(found, list)
    return [r for r in found if isinstance(r, dict)]


def change_alias(document: Document) -> None:
    revisions(document)[0]["alias"] = "ZZ-99"


def change_status(document: Document) -> None:
    revisions(document)[0]["record_status"] = "retracted"


def change_version(document: Document) -> None:
    revisions(document)[0]["ontology_version"] = "1.0.0"


def change_tx_pin(document: Document) -> None:
    ontology = document["ontology"]
    assert isinstance(ontology, dict)
    ontology["version"] = "1.0.0"


def alter_revisions_hash(migration: Document) -> None:
    migration["revisions_hash"] = "sha256:" + "0" * 64


def alter_entity_count(migration: Document) -> None:
    migration["counts"] = {"entities": 4, "edges": 0}  # the tx revises five


def intent_to_another_version(migration: Document) -> None:
    migration["to"] = "2.0.1"


def changed_identity(scene: MigratedScene) -> None:
    scene.rewrite_last_tx(change_alias)


def version_mismatch(scene: MigratedScene) -> None:
    scene.rewrite_last_tx(change_version)


# How each scene is built (the intent and the pin state are history, not files to edit later),
# and the edit applied afterwards.
SCENES: dict[str, tuple[dict[str, object], Callable[[MigratedScene], None]]] = {
    audit_codes.MIGRATION_CHANGED_IDENTITY: ({}, changed_identity),
    audit_codes.MIGRATION_VERSION_MISMATCH: ({}, version_mismatch),
    audit_codes.MIGRATION_REVISIONS_MISMATCH: (
        {"intent_edit": alter_revisions_hash},
        lambda scene: None,
    ),
    audit_codes.MIGRATION_UNPINNED: ({"pin": False, "marker": False}, lambda scene: None),
    audit_codes.MIGRATION_INCOMPLETE: ({"pin": False, "marker": True}, lambda scene: None),
}


def test_every_migration_code_has_one_scene() -> None:
    migration_codes = {
        spec.code for spec in audit_codes.FINDING_CODES if ".migration_" in spec.code
    }
    assert set(SCENES) == migration_codes


def test_a_migration_with_its_pin_and_prefixes_verifies_clean(tmp_path: Path) -> None:
    scene = scene_after(tmp_path, prefixes=PREFIXES)
    assert scene.manifest_of(scene.graph_id).entry.pin == pin_of(
        compiled_model("inventory", "2.0.0")
    )
    assert scene.store().migration_marker() is None
    assert scene.codes() == []


def test_a_migration_without_prefix_changes_verifies_clean(tmp_path: Path) -> None:
    assert scene_after(tmp_path).codes() == []


def test_the_pin_switched_but_the_marker_not_yet_removed_is_clean(tmp_path: Path) -> None:
    scene = scene_after(tmp_path, remove_marker=False)
    assert scene.store().migration_marker() is not None
    assert scene.codes() == []


def test_later_writes_after_the_migration_stay_clean(tmp_path: Path) -> None:
    scene = scene_after(tmp_path)
    scene.write_entities(scene.graph_id, 2, version="2.0.0")
    assert scene.codes() == []


def test_a_second_migration_is_judged_against_the_first(tmp_path: Path) -> None:
    scene = scene_after(tmp_path)
    scene.migrate(scene.graph_id, "2.1.0")
    assert scene.codes() == []


def test_the_incomplete_state_is_a_warning_not_an_error(tmp_path: Path) -> None:
    scene = scene_after(tmp_path, pin=False, marker=True)
    findings = [f for f in scene.verify() if f.code.startswith("integrity.audit.")]
    assert [(f.code, f.severity) for f in findings] == [
        (audit_codes.MIGRATION_INCOMPLETE, Severity.WARNING)
    ]
    assert findings[0].target["tx_seq"] == 3


@pytest.mark.parametrize("code", sorted(SCENES))
def test_tamper_scenario_triggers_only_its_code(tmp_path: Path, code: str) -> None:
    flags, edit = SCENES[code]
    scene = scene_after(tmp_path, **flags)
    edit(scene)
    assert scene.audit_codes() == [code]


def test_a_changed_record_status_is_the_same_finding(tmp_path: Path) -> None:
    scene = scene_after(tmp_path)
    scene.rewrite_last_tx(change_status)
    assert scene.audit_codes() == [audit_codes.MIGRATION_CHANGED_IDENTITY]


def test_each_part_of_the_version_check(tmp_path: Path) -> None:
    scene = scene_after(tmp_path / "pin")
    scene.rewrite_last_tx(change_tx_pin)
    assert scene.audit_codes() == [audit_codes.MIGRATION_VERSION_MISMATCH]
    scene = scene_after(tmp_path / "intent", intent_edit=intent_to_another_version)
    assert scene.audit_codes() == [audit_codes.MIGRATION_VERSION_MISMATCH]


def test_a_wrong_entity_count_is_a_revisions_mismatch(tmp_path: Path) -> None:
    scene = scene_after(tmp_path, intent_edit=alter_entity_count)
    assert scene.audit_codes() == [audit_codes.MIGRATION_REVISIONS_MISMATCH]


def test_a_migration_tx_that_lost_its_migration_key_differs_in_kind(tmp_path: Path) -> None:
    scene = scene_after(tmp_path)

    def become_a_write(document: Document) -> None:
        changeset = document["changeset"]
        assert isinstance(changeset, dict)
        del changeset["migration"]

    scene.rewrite_last_tx(become_a_write)
    findings = [f for f in scene.verify() if f.code.startswith("integrity.audit.")]
    assert sorted(f.code for f in findings) == [
        audit_codes.INTENT_WITHOUT_EFFECT,
        audit_codes.TX_WITHOUT_INTENT,
    ]
    (without_intent,) = [f for f in findings if f.code == audit_codes.TX_WITHOUT_INTENT]
    assert "differs in kind" in without_intent.message
