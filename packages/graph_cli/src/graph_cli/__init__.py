"""Domain-agnostic knowledge graph shell: application distribution."""

from graph_cli.kernel import timing as _timing  # noqa: F401  (first: the startup phase starts here)

# A constant, not importlib.metadata: looking the version up costs every command
# about 10 ms. tests/test_smoke.py keeps it equal to pyproject.toml.
__version__ = "0.12.0"
