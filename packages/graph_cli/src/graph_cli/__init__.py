"""Domain-agnostic knowledge graph shell: application distribution."""

from graph_cli.kernel import timing as _timing  # noqa: F401  (first: the startup phase starts here)

# isort: split
from importlib.metadata import version

__version__ = version("graph_cli")
