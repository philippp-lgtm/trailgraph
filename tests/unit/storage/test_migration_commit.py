"""Commit rules of a migration tx (plan M4; contracts B1.6, B8.3).

A migration tx revises existing entities, may change their type, and keeps their identity:
alias, record status, creation time and valid time. It carries a ``migration`` record, no
ops and no alias allocations, needs an allow decision for ``maint.migrate`` and is committed
by ``migrate`` only.
"""

from dataclasses import replace

import pytest
from support.memlog import MemLog

from graph_cli.kernel.authz import AuthorizedTx
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, TermRef
from graph_cli.kernel.records import Revision, revision_content_hash


@pytest.fixture
def log() -> MemLog:
    return MemLog("inventory")


def _seeded(log: MemLog) -> list[EntityId]:
    """Three committed entities, one of them retracted."""
    draft = log.draft()
    ids = [draft.create(), draft.create(), draft.create()]
    log.commit(draft.build())
    draft = log.draft()
    draft.retract(ids[2])
    log.commit(draft.build())
    return ids


def _current(log: MemLog, entity: EntityId) -> Revision:
    revision = log.at().current(entity)
    assert revision is not None
    return revision


def _other_type(log: MemLog, revision: Revision) -> TermRef:
    return TermRef(next(c for c in log.builder.concrete_classes if c != revision.type))


def _rule(log: MemLog, tx: AuthorizedTx, *, migration: bool = True) -> GraphCliError:
    with pytest.raises(GraphCliError) as info:
        log.check(tx, migration=migration)
    assert info.value.code == "internal.error"
    return info.value


def test_a_migration_tx_changes_types_and_keeps_identity(log: MemLog) -> None:
    first, second, retracted = _seeded(log)
    before = {e: _current(log, e) for e in (first, second, retracted)}
    draft = log.migration()
    draft.revise(first, type=_other_type(log, before[first]))
    draft.revise(second)  # a revision with the same content but the new version
    draft.revise(retracted, attrs={**before[retracted].attrs, "note": "kept"})
    log.commit(draft.build(), migration=True)
    after = {e: _current(log, e) for e in before}
    assert after[first].type != before[first].type
    assert all(after[e].rev == before[e].rev + 1 for e in before)
    assert all(after[e].alias == before[e].alias for e in before)
    assert all(after[e].created_at == before[e].created_at for e in before)
    assert after[retracted].record_status == "retracted"  # non-active entities migrate too


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("alias", "ZZ-0001"),
        ("record_status", "superseded"),
        ("created_at", None),
        ("valid_from", "2020-01-01"),
        ("valid_to", "2030-01-01"),
    ],
)
def test_a_migration_tx_keeps_the_identity_fields(log: MemLog, name: str, value: object) -> None:
    first, *_ = _seeded(log)
    previous = _current(log, first)
    if name == "created_at":
        value = previous.created_at.replace(year=previous.created_at.year - 1)
    draft = log.migration()
    draft.revise(first, **{name: value})
    error = _rule(log, draft.build())
    assert error.details["rule"] == "migration_identity"
    assert error.details["field"] == name


def test_a_removed_alias_is_an_identity_change_too(log: MemLog) -> None:
    draft = log.draft()
    cls = next(c for c in log.builder.concrete_classes if log.builder.prefix_of(c))
    entity = draft.create(cls)
    log.commit(draft.build())
    assert _current(log, entity).alias is not None
    draft = log.migration()
    draft.revise(entity, alias=None)
    assert _rule(log, draft.build()).details["field"] == "alias"


def test_a_type_change_outside_a_migration_tx_is_refused(log: MemLog) -> None:
    first, *_ = _seeded(log)
    previous = _current(log, first)
    draft = log.draft()
    draft.update(first)
    tx = draft.build()
    (revision,) = tx.revisions
    changed = replace(revision, type=_other_type(log, previous))
    changed = replace(changed, content_hash=revision_content_hash(changed))
    error = _rule(log, replace(tx, revisions=(changed,)), migration=False)
    assert error.details["rule"] == "type_changed"


def test_the_migration_key_is_refused_by_commit(log: MemLog) -> None:
    first, *_ = _seeded(log)
    draft = log.migration()
    draft.revise(first)
    assert _rule(log, draft.build(), migration=False).details["rule"] == "migration_via_commit"


def test_migrate_refuses_an_ordinary_tx(log: MemLog) -> None:
    draft = log.draft()
    draft.create()
    error = _rule(log, draft.build(), migration=True)
    assert error.details["rule"] == "changeset"


def test_a_migration_tx_needs_the_maint_migrate_decision(log: MemLog) -> None:
    first, *_ = _seeded(log)
    draft = log.migration()
    draft.revise(first)
    error = _rule(log, draft.build(decisions=()))
    assert error.details["rule"] == "decisions"
    assert error.details["action"] == "maint.migrate"


def test_a_migration_tx_has_no_ops(log: MemLog) -> None:
    first, *_ = _seeded(log)
    draft = log.migration()
    draft.revise(first)
    tx = draft.build()
    record = tx.changeset["migration"]
    op: JsonValue = {"op": "update", "id": str(first), "set": {}}
    for changeset in (
        {"schema": "graph_cli.migration/1", "ops": [op], "migration": record},
        {"schema": "graph_cli.migration/1", "migration": record},
        {"schema": "graph_cli.migration/1", "ops": {}, "migration": record},
    ):
        broken = replace(tx, changeset=changeset)
        assert _rule(log, broken).details["rule"] == "changeset"


def test_a_migration_tx_has_the_migration_schema(log: MemLog) -> None:
    first, *_ = _seeded(log)
    draft = log.migration()
    draft.revise(first)
    tx = draft.build()
    wrong = {"schema": "graph_cli.changeset/1", "ops": [], "migration": tx.changeset["migration"]}
    assert _rule(log, replace(tx, changeset=wrong)).details["rule"] == "changeset"


def test_a_migration_tx_creates_no_entity(log: MemLog) -> None:
    first, *_ = _seeded(log)
    draft = log.migration()
    draft.revise(first)
    tx = draft.build()
    (revision,) = tx.revisions
    new = replace(
        revision,
        id=EntityId(log.builder.ids.new_id()),
        rev=1,
        prev_rev=None,
        created_at=tx.recorded_at,
        alias=None,
    )
    new = replace(new, content_hash=revision_content_hash(new))
    assert _rule(log, replace(tx, revisions=(new,))).details["rule"] == "migration_new_entity"


def test_a_migration_tx_allocates_no_alias(log: MemLog) -> None:
    first, *_ = _seeded(log)
    draft = log.migration()
    draft.revise(first)
    tx = replace(draft.build(), alias_allocations=((first, "ZZ-0001"),))
    assert _rule(log, tx).details["rule"] == "migration_aliases"


def test_the_other_commit_rules_still_apply_to_a_migration_tx(log: MemLog) -> None:
    first, *_ = _seeded(log)
    draft = log.migration()
    draft.revise(first, rev=9, prev_rev=8)
    assert _rule(log, draft.build()).details["rule"] == "rev"
    draft = log.migration()
    draft.revise(first)
    tx = draft.build()
    assert _rule(log, replace(tx, seq=tx.seq + 1)).details["rule"] == "seq"
    (revision,) = tx.revisions
    bad = replace(revision, content_hash="sha256:" + "1" * 64)
    assert _rule(log, replace(tx, revisions=(bad,))).details["rule"] == "content_hash"
