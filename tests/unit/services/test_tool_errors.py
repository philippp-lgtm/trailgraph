"""The error map of the tool manifest (tool manifest plan TM8): every code a tool lists has an
``on_error`` value, the override table names only registered codes, and a ``refused`` code a tool
lists is decided explicitly."""

import pytest

from graph_cli.app_cli.tool_manifest import tool_errors
from graph_cli.kernel.error_codes import ERROR_CODES, ErrorCategory
from graph_cli.services import tool_errors as te
from graph_cli.services.describe_tools import KINDS


def listed() -> set[str]:
    return {code for kind in KINDS for code in tool_errors(kind)}


def test_every_code_a_tool_lists_is_registered_and_has_a_value() -> None:
    for code in listed():
        assert code in ERROR_CODES, code
        assert te.on_error(code) in te.ON_ERROR_VALUES, code
        info = te.error_info(code)
        assert set(info) == {"category", "retryable", "summary", "on_error"}
        assert info["on_error"] == te.on_error(code)


def test_the_override_table_names_only_registered_codes_and_known_values() -> None:
    assert set(te.OVERRIDES) <= set(ERROR_CODES)
    assert set(te.OVERRIDES.values()) <= set(te.ON_ERROR_VALUES)


def test_the_override_table_has_no_entry_the_category_already_gives() -> None:
    """An entry that only repeats the default is noise: it would hide a later change of the
    registry."""
    for code, value in te.OVERRIDES.items():
        spec = ERROR_CODES[code]
        default = te.RETRY if spec.retryable else te.ON_ERROR_BY_CATEGORY[spec.category]
        if spec.category is ErrorCategory.REFUSED:
            continue  # decided explicitly, stop or fix_input
        assert value != default, code


def test_a_refused_code_a_tool_lists_is_decided_explicitly() -> None:
    for code in listed():
        if ERROR_CODES[code].category is ErrorCategory.REFUSED:
            assert code in te.OVERRIDES, f"decide what a harness does about {code}"


def test_every_category_has_a_default() -> None:
    assert set(te.ON_ERROR_BY_CATEGORY) == set(ErrorCategory) - {ErrorCategory.WARNING}


@pytest.mark.parametrize(
    ("code", "value"),
    [
        ("backend.lock_timeout", te.RETRY),  # retryable
        ("audit.unavailable", te.RETRY),
        ("function.parameter_invalid", te.FIX_INPUT),  # usage
        ("write.invalid", te.FIX_INPUT),  # invalid
        ("function.parameter_type", te.FIX_INPUT),  # refused, the argument's class is wrong
        ("entity.ambiguous_ref", te.FIX_INPUT),
        ("entity.not_found", te.FIX_INPUT),
        ("access.reason_required", te.FIX_INPUT),
        ("action.precondition_failed", te.CHECK_STATE),
        ("action.nothing_to_do", te.CHECK_STATE),
        ("view.cursor_stale", te.RESTART),
        ("tool.hash_mismatch", te.REFRESH),
        ("function.stale", te.STOP),  # invalid, but only a maintainer can fix it
        ("action.stale", te.STOP),
        ("view.stale", te.STOP),
        ("access.denied", te.STOP),
        ("function.not_found", te.STOP),
        ("tool.not_found", te.STOP),
        ("graph.archived", te.STOP),
        ("conflict.idempotency_key_reuse", te.STOP),
        ("internal.error", te.STOP),
        ("remote.unauthorized", te.STOP),
    ],
)
def test_what_a_harness_does_about_a_code(code: str, value: str) -> None:
    assert te.on_error(code) == value


def test_a_retryable_code_is_retried_whatever_its_category() -> None:
    for code, spec in ERROR_CODES.items():
        if spec.retryable and code not in te.OVERRIDES:
            assert te.on_error(code) == te.RETRY


def test_an_unknown_code_is_a_bug() -> None:
    with pytest.raises(KeyError):
        te.on_error("no.such_code")
    with pytest.raises(KeyError):
        te.error_info("no.such_code")


def test_the_tool_codes_are_in_the_registry() -> None:
    assert ERROR_CODES["tool.not_found"].category is ErrorCategory.NOT_FOUND
    assert ERROR_CODES["tool.hash_mismatch"].category is ErrorCategory.CONFLICT
    assert not ERROR_CODES["tool.hash_mismatch"].retryable
