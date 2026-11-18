"""The tracked view fixtures: a valid set and one broken file per rule.

They are written against the tracked example ontology ``examples/company/ontology/1.1.0``
(``example_model``) and the example functions of ``examples/company/functions``, plus the one
function fixture with a list and a date parameter.
"""

from pathlib import Path

from graph_cli.kinetic.compile import compile_catalog
from graph_cli.kinetic.entries import FunctionEntry, load_entries
from graph_cli.ontology_layer.model import CompiledModel
from support import functions
from support.fixtures import EXAMPLES, FIXTURES, example_model

VIEWS = FIXTURES / "views" / "company"
VALID = VIEWS / "valid"
BROKEN_VIEWS = VIEWS / "broken"


def company_model() -> CompiledModel:
    return example_model("company", "1.1.0")


def function_sources() -> dict[str, str]:
    """The example functions of the company and the fixture with ``many`` and ``date``."""
    sources = functions.read_dir(EXAMPLES / "company" / "functions")
    name = "training_completions.yaml"
    sources[name] = (functions.VALID / name).read_text()
    return sources


def function_entries(
    model: CompiledModel | None = None, sources: dict[str, str] | None = None
) -> tuple[FunctionEntry, ...]:
    """The function catalog the views are checked with: ``function_sources`` compiled."""
    document, problems = compile_catalog(sources or function_sources(), model or company_model())
    assert problems == []
    return load_entries(document)


def read_dir(path: Path) -> dict[str, str]:
    """File name -> content of the view files in a folder."""
    return {p.name: p.read_text() for p in sorted(path.glob("*.yaml"))}


def valid_sources() -> dict[str, str]:
    return read_dir(VALID)


def edited(file_name: str, old: str, new: str) -> dict[str, str]:
    """The valid sources with one replacement in one file (``old`` must occur)."""
    sources = valid_sources()
    assert old in sources[file_name], old
    sources[file_name] = sources[file_name].replace(old, new)
    return sources
