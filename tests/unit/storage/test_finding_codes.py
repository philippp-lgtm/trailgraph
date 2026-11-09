"""Storage finding codes and fault points (contracts R.4, R.5, section 4.1)."""

from graph_cli.kernel.findings import FINDING_CODE_RE, Severity
from graph_cli.storage import finding_codes
from graph_cli.storage.common.faults import (
    EXTRA_POINTS,
    KEYS_POINTS,
    MAINT_POINTS,
    CrashPoint,
    fire,
)


def test_codes_follow_the_grammar_and_are_unique() -> None:
    codes = [spec.code for spec in finding_codes.FINDING_CODES]
    assert len(codes) == len(set(codes))
    for spec in finding_codes.FINDING_CODES:
        assert FINDING_CODE_RE.match(spec.code)
        assert spec.code.startswith("integrity.storage.")
        assert (spec.when, spec.fixture) == ("verify", "tamper")
        info = spec.code in (finding_codes.BLOB_ORPHAN, finding_codes.BLOB_STAGING_LEFTOVER)
        assert spec.default_severity is (Severity.INFO if info else Severity.ERROR)


def test_constants_name_registered_codes() -> None:
    codes = {spec.code for spec in finding_codes.FINDING_CODES}
    constants = {
        value
        for name, value in vars(finding_codes).items()
        if name.isupper() and isinstance(value, str)
    }
    assert constants == codes


def test_crash_points_cover_the_contract_table() -> None:
    contract = {
        "audit.before_write",
        "audit.after_write_before_sync",
        "audit.after_sync",
        "tx.after_write_before_sync",
        "tx.after_sync",
        "materialize.after_first_file",
        "materialize.before_dir_sync",
        "head.before_rename",
        "head.after_rename",
        "blob.after_stage",
        "blob.before_rename",
        "blob.after_rename",
    }
    assert {p.value for p in CrashPoint} - {
        p.value for p in EXTRA_POINTS | MAINT_POINTS | KEYS_POINTS
    } == contract


def test_fire_without_hook_is_a_no_op() -> None:
    seen: list[CrashPoint] = []
    fire(None, CrashPoint.TX_AFTER_SYNC)
    fire(seen.append, CrashPoint.TX_AFTER_SYNC)
    assert seen == [CrashPoint.TX_AFTER_SYNC]
