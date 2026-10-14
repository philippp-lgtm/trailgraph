# graph_cli

A domain-agnostic shell for knowledge graphs. Two command-line tools with a
strict split between usage and maintenance:

- `graph` (distribution `graph_cli`): the application CLI. Installed on
  `PATH`; reads and writes instance data.
- `graph-admin` (distribution `graph_cli_admin`): the maintenance CLI. Not on
  `PATH`; creates graphs, versions ontologies, archives graphs.

Status: repository skeleton. No commands beyond `--help` and `--version` yet.

## Repository layout

```
packages/
  graph_cli/src/graph_cli/              application distribution
  graph_cli_admin/src/graph_cli_admin/  maintenance distribution
tests/                                  tests for both distributions
skills/                                 agent skills
docs/                                   documentation
```

## Layers

Dependencies point down only. Enforced by import-linter
(`[tool.importlinter]` in `pyproject.toml`) and checked by the test suite.

| Layer | `graph_cli` subpackage |
| --- | --- |
| CLI | `app_cli` |
| CLI infrastructure | `cli_kit` |
| Registry and wiring | `catalog` |
| Extensions | `extensions` |
| Services | `services` |
| Cross-cutting (independent of each other) | `audit`, `access_control`, `temporal_knowledge`, `storage` |
| Query model | `query` |
| Schema IR | `ontology_layer` |
| Kernel | `kernel` |

`graph_cli_admin` depends on `graph_cli`; `graph_cli` never imports
`graph_cli_admin`. Within `graph_cli_admin`, `maintenance_cli` sits above
`services_admin`. Only the CLI layers may import the CLI framework (`click`),
and storage adapters import only `kernel`.

## Development

Requires [uv](https://docs.astral.sh/uv/). uv provisions the Python version
from `.python-version`.

```sh
uv sync                     # create .venv with both distributions + dev tools
uv run pytest               # all tests (includes import contracts and wheel checks)
uv run pytest -m "not slow" # skip distribution builds
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run lint-imports
```

Run the CLIs from the checkout:

```sh
uv run graph --help
uv run graph-admin --help
```

### Test harness

CLIs are tested as subprocesses (`tests/conftest.py`: `run_cli`). Every run
gets a throwaway `HOME` and XDG directories and no `GRAPH_CLI_*` variables, so
tests never touch real user config, data or state.
