"""The static registry of extractors (the B10.1 pattern: code, not configuration).

Third-party extractors through an entry-point group are reserved (B10) and not built.
``format`` comes first: every other extractor dispatches on its result.
"""

from __future__ import annotations

from collections.abc import Mapping

from graph_cli.extractors.format import FORMAT
from graph_cli.extractors.image import IMAGE
from graph_cli.extractors.model import Extractor
from graph_cli.extractors.pdf import PDF
from graph_cli.extractors.sqlite import SQLITE
from graph_cli.extractors.table import TABLE

REGISTRY: dict[str, Extractor] = {
    extractor.name: extractor for extractor in (FORMAT, PDF, IMAGE, TABLE, SQLITE)
}


def extractors() -> Mapping[str, Extractor]:
    """The extractors by name, in registration order (``format`` first)."""
    return REGISTRY


def get(name: str) -> Extractor | None:
    return REGISTRY.get(name)
