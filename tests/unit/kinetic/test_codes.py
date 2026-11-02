"""Every ``function.*`` and ``action.*`` code the kinetic package emits is in the error
registry (B9) and has a hint entry."""

import re
from pathlib import Path

import graph_cli.kinetic as kinetic
import graph_cli_admin.services_admin as admin_services
from graph_cli.cli_kit.hints import known_hint_codes
from graph_cli.kernel.error_codes import ERROR_CODES, ErrorCategory, lookup_error_code

SOURCES = Path(kinetic.__file__).parent
# The publish flow of graph-admin emits the one code of a folder that holds other files.
ADMIN_SOURCES = (Path(admin_services.__file__).parent / "kinetic_slots.py",)
LITERAL = re.compile(r'"((?:function|action)\.[a-z_]+)"')

# Codes the package builds from the kind (``f"{kind}.parameter_missing"``): each is
# checked for both families.
KIND_BUILT = (
    "parameter_missing",
    "parameter_unknown",
    "parameter_invalid",
    "invalid_value",
    "missing_key",
    "unknown_key",
    "module_required",
    "builtin_module",
    "unknown_module",
)
# Codes built from a term kind (``f"action.unknown_{kind}"``): actions only.
TERM_BUILT = ("action.unknown_type", "action.unknown_relation")


def emitted() -> set[str]:
    paths = [*SOURCES.glob("*.py"), *ADMIN_SOURCES]
    literal = {code for path in paths for code in LITERAL.findall(path.read_text())}
    built = {f"{kind}.{suffix}" for kind in ("function", "action") for suffix in KIND_BUILT}
    return literal | built | set(TERM_BUILT)


def test_every_emitted_code_is_registered_with_a_hint_entry() -> None:
    codes = emitted()
    assert "function.pattern_impossible" in codes  # positive controls for the scan
    assert "action.op_impossible" in codes
    assert "action.parameter_missing" in codes and "function.parameter_missing" in codes
    missing = sorted(c for c in codes if lookup_error_code(c) is None)
    assert missing == []
    assert sorted(c for c in codes if c not in known_hint_codes()) == []


def test_every_registered_action_code_is_emitted_somewhere() -> None:
    registered = {c for c in ERROR_CODES if c.startswith("action.")}
    assert registered - emitted() == set()


def test_compile_problems_are_invalid_not_usage() -> None:
    for code in ("function.pattern_impossible", "action.op_impossible", "action.template_invalid"):
        spec = lookup_error_code(code)
        assert spec is not None and spec.category is ErrorCategory.INVALID


def test_binding_errors_are_usage_for_both_kinds() -> None:
    for kind in ("function", "action"):
        for suffix in ("parameter_missing", "parameter_unknown", "parameter_invalid"):
            spec = lookup_error_code(f"{kind}.{suffix}")
            assert spec is not None and spec.category is ErrorCategory.USAGE


def test_the_run_time_refusals_of_an_action() -> None:
    expected = {
        "action.not_found": ErrorCategory.NOT_FOUND,
        "action.precondition_failed": ErrorCategory.INVALID,
        "action.nothing_to_do": ErrorCategory.REFUSED,
        "action.reason_required": ErrorCategory.USAGE,
    }
    for code, category in expected.items():
        spec = lookup_error_code(code)
        assert spec is not None and spec.category is category


def test_hints_point_at_discovery_and_the_action() -> None:
    from graph_cli.cli_kit.hints import hint_for

    assert hint_for("action.not_found", {"name": "x"}, program="graph") == (
        "graph describe search <words> --kind action"
    )
    assert hint_for(
        "action.precondition_failed", {"action": "assign_position"}, program="graph"
    ) == ("graph describe action assign_position")
    assert hint_for(
        "action.parameter_invalid",
        {"action": "a", "parameter": "p", "type": "ref x"},
        program="graph",
    ) == ("graph lookup <text> --for a.p")
