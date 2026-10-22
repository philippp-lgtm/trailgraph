"""Audit finding codes (contracts R.4, R.5, section 1.5.2)."""

from graph_cli.audit import finding_codes
from graph_cli.kernel.findings import FINDING_CODE_RE, Severity


def test_codes_follow_the_grammar_and_are_unique() -> None:
    codes = [spec.code for spec in finding_codes.FINDING_CODES]
    assert len(codes) == len(set(codes))
    for spec in finding_codes.FINDING_CODES:
        assert FINDING_CODE_RE.match(spec.code)
        assert spec.code.startswith("integrity.audit.")
        assert (spec.when, spec.fixture) == ("verify", "tamper")


def test_severities_follow_the_contract() -> None:
    severity = {spec.code: spec.default_severity for spec in finding_codes.FINDING_CODES}
    assert severity.pop(finding_codes.UNKNOWN_ACTION) is Severity.WARNING
    assert severity.pop(finding_codes.INTENT_WITHOUT_EFFECT) is Severity.INFO
    assert severity.pop(finding_codes.POLICY_FALLBACK) is Severity.WARNING
    assert severity.pop(finding_codes.POLICY_HASH_UNCHECKED) is Severity.INFO
    assert set(severity.values()) == {Severity.ERROR}


def test_constants_name_registered_codes() -> None:
    codes = {spec.code for spec in finding_codes.FINDING_CODES}
    constants = {
        value
        for name, value in vars(finding_codes).items()
        if name.isupper() and isinstance(value, str)
    }
    assert constants == codes
