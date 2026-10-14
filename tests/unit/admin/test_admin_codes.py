"""Admin-only codes: registered by the admin root, absent from the app (R.1; plan M7)."""

from graph_cli.cli_kit.hints import known_hint_codes
from graph_cli.kernel.error_codes import ERROR_CODES, ErrorCategory, all_error_codes
from graph_cli_admin.maintenance_cli import hints
from graph_cli_admin.services_admin import error_codes


def test_admin_codes_live_in_the_admin_distribution() -> None:
    assert all(spec.code.startswith("admin.") for spec in error_codes.ADMIN_CODES)
    assert not [code for code in ERROR_CODES if code.startswith("admin.")]


def test_every_registered_error_code_has_a_hint_entry() -> None:
    error_codes.register()
    hints.register()
    errors = {c for c, s in all_error_codes().items() if s.category is not ErrorCategory.WARNING}
    assert {spec.code for spec in error_codes.ADMIN_CODES} <= set(all_error_codes())
    assert errors <= known_hint_codes()
