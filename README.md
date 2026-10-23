# graph_cli

A domain-agnostic shell for knowledge graphs. Two command-line tools with a
strict split between usage and maintenance:

- `graph` (distribution `graph_cli`): the application CLI. Installed on
  `PATH`; reads and writes instance data. Built for coding agents and people
  alike: every command prints one envelope as text or, with `--json`, as one
  JSON document.
- `graph-admin` (distribution `graph_cli_admin`): the maintenance CLI. Not on
  `PATH`; run from the repository checkout. Creates graphs, versions
  ontologies, installs policies and skills, verifies the audit trail.

Every write is a validated, audited change set against a versioned ontology.
Access is decided by policies per principal, project and graph, and agents
get generated skills that describe the CLI and each graph.

Status: foundation. Both CLIs, the ontology layer, the file store, audit,
access control, stored files and the agent skills work; storage is local
(POSIX file systems only).

## Install

Requires [uv](https://docs.astral.sh/uv/). uv provisions the Python version
from `.python-version`.

```sh
uv sync                     # create .venv with both distributions + dev tools
```

`graph-admin` stays in the checkout (`uv run graph-admin ...`). Install
`graph` as a tool, with its dependencies pinned to the workspace lock
(`uv tool install` ignores `uv.lock`) and compiled to bytecode, so the first
call after an install starts as fast as the others:

```sh
uv export --frozen --package graph-cli --no-hashes --no-emit-workspace \
  --no-dev --no-header -o <file>
uv tool install --reinstall --compile-bytecode --constraints <file> ./packages/graph_cli
```

## Quickstart

Create a data root and a graph from one of the toy ontologies:

```sh
uv run graph-admin init
uv run graph-admin graph create notes --ontology tests/fixtures/ontologies/notes_and_sources/1.1.0
```

In a project directory, a pointer file names the graphs the project uses:

```toml
# .graph_cli.toml
format_version = 1
graphs = ["notes"]
default = "notes"
```

Then, from that directory:

```sh
graph graphs                                         # the graphs this project may use
graph describe types                                 # the ontology of the selected graph
graph create nas:Topic --set label="Graph databases"
graph lookup "Graph databases"
graph find --type nas:Topic --json
mkdir -p .claude && graph skills install --scope project   # the graph-cli agent skill
```

A graph skill that tells agents what the graph holds is published with
`graph-admin graph skill publish` and installed with `--graph` (see
[Skills](docs/skills.md)).

## Documentation

See [`docs/README.md`](docs/README.md) for the full documentation: the
command references, configuration, access control, stored files, agent
skills and the internals. To work on graph_cli itself, see
[`CONTRIBUTING.md`](CONTRIBUTING.md).
