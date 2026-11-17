"""The static registry of extractors (the B10.1 pattern: code, not configuration).

Third-party extractors through an entry-point group are reserved (B10) and not built.
"""

from __future__ import annotations

from collections.abc import Mapping

from graph_cli.extractors.format import FORMAT
from graph_cli.extractors.model import Extractor

REGISTRY: dict[str, Extractor] = {extractor.name: extractor for extractor in (FORMAT,)}


def extractors() -> Mapping[str, Extractor]:
    """The extractors by name, in registration order (``format`` first)."""
    return REGISTRY


def get(name: str) -> Extractor | None:
    return REGISTRY.get(name)
