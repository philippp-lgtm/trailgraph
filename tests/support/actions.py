"""The tracked action fixtures: a valid set and one broken file per rule.

They are written against the tracked example ontology ``examples/company/ontology/1.1.0``
(``example_model``): the fixture ontologies lack the employment lifecycle and the legal
module the actions need.
"""

from pathlib import Path

from graph_cli.ontology_layer.model import CompiledModel
from support.fixtures import FIXTURES, example_model

ACTIONS = FIXTURES / "actions" / "company"
VALID = ACTIONS / "valid"
BROKEN_ACTIONS = ACTIONS / "broken"


def company_model() -> CompiledModel:
    return example_model("company", "1.1.0")


def read_dir(path: Path) -> dict[str, str]:
    """File name -> content of the action files in a folder."""
    return {p.name: p.read_text() for p in sorted(path.glob("*.yaml"))}


def valid_sources() -> dict[str, str]:
    return read_dir(VALID)


def edited(file_name: str, old: str, new: str) -> dict[str, str]:
    """The valid sources with one replacement in one file (``old`` must occur)."""
    sources = valid_sources()
    assert old in sources[file_name], old
    sources[file_name] = sources[file_name].replace(old, new)
    return sources
