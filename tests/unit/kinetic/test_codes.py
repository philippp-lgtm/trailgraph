"""Every ``function.*``, ``action.*`` and ``view.*`` code the kinetic package emits is in the
error registry (B9) and has a hint entry."""

import re
from pathlib import Path

import graph_cli.kinetic as kinetic
import graph_cli.services.actions as action_service
import graph_cli.services.read as read_service
import graph_cli_admin.services_admin as admin_services
from graph_cli.cli_kit.hints import known_hint_codes
from graph_cli.kernel.error_codes import ERROR_CODES, ErrorCategory, lookup_error_code

SOURCES = Path(kinetic.__file__).parent
# The publish flow of graph-admin emits the one code of a folder that holds other files.
ADMIN_SOURCES = (
    Path(admin_services.__file__).parent / "kinetic_slots.py",
    Path(action_service.__file__),  # ``graph do``: action.stale, action.deprecated
)
# ``graph view``, the read service: view.stale, view.takes_entity, ... (its function.* codes are
# the function tests' business)
VIEW_SOURCES = tuple(sorted(Path(read_service.__file__).parent.glob("*.py")))
LITERAL = re.compile(r'"((?:function|action|view)\.[a-z_]+)"')

# Codes the package builds from the kind (``f"{kind}.parameter_missing"``): each is
# checked for the three families.
KIND_BUILT = (
    "parameter_missing",
    "parameter_unknown",
    "parameter_invalid",
    "parameter_type",  # services.refs.entity_params
    "invalid_value",
    "missing_key",
    "unknown_key",
    "module_required",
    "builtin_module",
    "unknown_module",
)
# Codes built from a term kind (``f"action.unknown_{kind}"``): actions only; views build
# ``view.unknown_class`` and ``view.unknown_relation`` the same way.
TERM_BUILT = (
    "action.unknown_type",
    "action.unknown_relation",
    "view.unknown_class",
    "view.unknown_relation",
)


def emitted() -> set[str]:
    paths = [*SOURCES.glob("*.py"), *ADMIN_SOURCES]
    literal = {code for path in paths for code in LITERAL.findall(path.read_text())}
    literal |= {
        code
        for path in VIEW_SOURCES
        for code in LITERAL.findall(path.read_text())
        if code.startswith("view.")
    }
    kinds = ("function", "action", "view")
    built = {f"{kind}.{suffix}" for kind in kinds for suffix in KIND_BUILT}
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


def test_every_registered_view_code_is_emitted_somewhere() -> None:
    registered = {c for c in ERROR_CODES if c.startswith("view.")}
    assert registered - emitted() == set()


def test_compile_problems_are_invalid_not_usage() -> None:
    for code in (
        "function.pattern_impossible",
        "action.op_impossible",
        "action.template_invalid",
        "view.invalid_where",
        "view.unknown_function",
        "view.stale",
    ):
        spec = lookup_error_code(code)
        assert spec is not None and spec.category is ErrorCategory.INVALID


def test_binding_errors_are_usage_for_both_kinds() -> None:
    for kind in ("function", "action", "view"):
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


def test_a_reference_of_the_wrong_class_in_a_view_is_looked_up_by_the_kind_of_the_view() -> None:
    from graph_cli.cli_kit.hints import hint_for

    # the kind names a view (a function may share its name); an entity view's slot is _entity
    assert hint_for(
        "view.parameter_type",
        {"view": "v", "parameter": "p", "expected": "ex:C", "found": "ex:D"},
        program="graph",
    ) == ("graph lookup <text> --for view:v.p")
    assert hint_for(
        "view.class_mismatch", {"view": "v", "expected": "ex:C", "found": "ex:D"}, program="graph"
    ) == ("graph lookup <text> --for view:v._entity")
