"""Error registry, typed errors, finding grammar and exit mapping (R.1-R.5, 1.5.1)."""

import pytest

from graph_cli.cli_kit.exit_codes import EXIT_BY_CATEGORY, exit_code_for
from graph_cli.kernel import error_codes
from graph_cli.kernel.error_codes import (
    ERROR_CODE_RE,
    ERROR_CODES,
    ErrorCategory,
    ErrorSpec,
    all_error_codes,
    lookup_error_code,
    register_error_codes,
)
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.findings import (
    Finding,
    FindingCodeSpec,
    Severity,
    has_errors,
    sort_findings,
)


def test_codes_follow_the_grammar() -> None:
    for code in ERROR_CODES:
        assert ERROR_CODE_RE.match(code), code


def test_every_error_category_has_an_exit_code() -> None:
    assert set(EXIT_BY_CATEGORY) == set(ErrorCategory) - {ErrorCategory.WARNING}
    for spec in ERROR_CODES.values():
        if spec.category is not ErrorCategory.WARNING:
            assert exit_code_for(spec.category) in range(1, 7)


def test_warning_category_has_no_exit_code() -> None:
    with pytest.raises(ValueError):
        exit_code_for(ErrorCategory.WARNING)


@pytest.mark.parametrize(
    ("code", "exit_code"),
    [
        ("entity.not_found", 1),
        ("usage.invalid_argument", 2),
        ("access.ceiling", 3),
        ("conflict.version_mismatch", 4),
        ("backend.lock_timeout", 5),
        ("internal.error", 6),
    ],
)
def test_exit_mapping_examples(code: str, exit_code: int) -> None:
    spec = lookup_error_code(code)
    assert spec is not None
    assert exit_code_for(spec.category) == exit_code


def test_known_code() -> None:
    error = GraphCliError("entity.not_found", "no such entity", details={"id": "x"})
    assert error.code == "entity.not_found"
    assert error.category is ErrorCategory.NOT_FOUND
    assert not error.retryable
    assert dict(error.details) == {"id": "x"}


@pytest.mark.parametrize("code", ["nope.unknown", "entity.superseded"])
def test_unregistered_or_warning_code_becomes_internal_error(code: str) -> None:
    error = GraphCliError(code, "boom")
    assert error.code == "internal.error"
    assert error.details["unregistered_code"] == code


def test_register_error_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    # A copy of the registry: the test-only code must not reach later tests in this process
    # (the hint check of tests/unit/admin/test_admin_codes.py sees every registered code).
    monkeypatch.setattr(error_codes, "_registered", dict(error_codes._registered))
    spec = ErrorSpec("testonly.example", ErrorCategory.REFUSED, False, "Test-only code.")
    register_error_codes([spec])
    register_error_codes([spec])  # identical re-registration is a no-op
    assert GraphCliError("testonly.example", "x").code == "testonly.example"
    assert "testonly.example" in all_error_codes()
    with pytest.raises(ValueError):
        register_error_codes([ErrorSpec("testonly.example", ErrorCategory.USAGE, False, "Other.")])
    with pytest.raises(ValueError):
        register_error_codes([ErrorSpec("internal.error", ErrorCategory.USAGE, False, "Other.")])


def test_error_spec_grammar() -> None:
    with pytest.raises(ValueError):
        ErrorSpec("NoDots", ErrorCategory.USAGE, False, "x")


def test_warning_record_json() -> None:
    assert WarningRecord("entity.superseded", "m").to_json() == {
        "code": "entity.superseded",
        "message": "m",
        "details": {},
    }


@pytest.mark.parametrize(
    "code",
    ["model.structural.unknown_key", "integrity.audit.seq_gap", "ext.probe.some.thing"],
)
def test_finding_code_grammar_accepts(code: str) -> None:
    scope = code.split(".")[0]
    layer = "ext" if scope == "ext" else code.split(".")[1]
    FindingCodeSpec(code, scope, layer, Severity.ERROR, "release", "none", "x")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("code", "scope", "layer"),
    [
        ("model.unknown", "model", "structural"),
        ("other.structural.x", "model", "structural"),
        ("model.structural.x", "data", "structural"),
        ("model.structural.x", "model", "semantic"),
        ("ext.x.y", "ext", "ext"),
    ],
)
def test_finding_code_grammar_refuses(code: str, scope: str, layer: str) -> None:
    with pytest.raises(ValueError):
        FindingCodeSpec(code, scope, layer, Severity.ERROR, "release", "none", "x")  # type: ignore[arg-type]


def test_finding_json_omits_absent_optionals() -> None:
    finding = Finding(Severity.WARNING, "model.semantic.x", {"path": "a"}, "m", actual=0)
    assert finding.to_json() == {
        "severity": "warning",
        "code": "model.semantic.x",
        "target": {"path": "a"},
        "message": "m",
        "actual": 0,
    }


def test_sort_findings_and_has_errors() -> None:
    a = Finding(Severity.WARNING, "model.b.x", {"path": "b"}, "m")
    b = Finding(Severity.ERROR, "model.a.x", {"path": "b"}, "m")
    c = Finding(Severity.ERROR, "model.z.x", {"path": "a"}, "m")
    assert sort_findings([a, b, c]) == [c, b, a]
    assert has_errors([a, b])
    assert not has_errors([a])
