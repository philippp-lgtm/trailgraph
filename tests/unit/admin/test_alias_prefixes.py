"""Alias prefixes seeded at graph create (B1 open point), imported module classes included."""

from support.fixtures import ONTOLOGIES

from graph_cli.kernel.ids import TermRef
from graph_cli_admin.services_admin.graphs import alias_prefixes
from graph_cli_admin.services_admin.ontology import compile_source


def test_classes_of_imported_modules_seed_their_prefix() -> None:
    prefixes = alias_prefixes(compile_source(ONTOLOGIES / "notes_and_sources" / "1.1.0"))
    assert prefixes[TermRef("files:File")] == "FILE"
    assert prefixes[TermRef("nas:Scan")] == "SCAN"
    assert prefixes[TermRef("nas:Note")] == "NOTE"
