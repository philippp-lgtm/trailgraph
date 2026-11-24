# graph_cli

A domain-agnostic shell for knowledge graphs. Two command-line tools with a
strict split between usage and maintenance, and a server:

- `graph` (distribution `graph_cli`): the application CLI. Installed on
  `PATH`; reads and writes instance data. Built for coding agents and people
  alike: every command prints one envelope as text or, with `--json`, as one
  JSON document.
- `graph-admin` (distribution `graph_cli_admin`): the maintenance CLI. Not on
  `PATH`; run from the repository checkout, or installed from a server on a
  machine in remote mode. Creates graphs, versions ontologies, installs
  policies and skills, verifies the audit trail and serves a
  [live view](docs/live_view.md) of the graphs in the browser; `graph-admin
  workspace sync <dir>` sets up a folder to maintain graphs from, with
  skills for agents ([Maintainer workspaces](docs/cli_graph_admin.md#maintainer-workspaces)).
- `graph-server` (distribution `graph_cli_server`): the command surface of
  `graph` over HTTP with bearer tokens, for agents in containers, other
  machines and web apps; `graph` in remote mode is its client. Runs from
  the checkout or in the container (`docker compose up -d`, see
  [Server](docs/server.md)).

Every write is a validated, audited change set against a versioned ontology.
Access is decided by policies per principal, project and graph, and agents
get generated skills that describe the CLI and each graph.

Storage is local to the machine that owns the data (POSIX file systems
only). To use one graph from several machines, run `graph-server` where the
data lives and `graph` in remote mode elsewhere.

## Install

Requires [uv](https://docs.astral.sh/uv/). uv provisions the Python version
from `.python-version`.

```sh
uv sync                     # create .venv with all distributions + dev tools
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

For a client of a `graph-server`, wrap the install in `python3
scripts/build_id.py` and `python3 scripts/build_id.py --clean`, so the
installed program names its build ([Builds](docs/server.md#builds)), or
install the server's own wheel from `<endpoint>/v1/dist/`; from then on
`graph self update` keeps program and skills in step with the server
([Self update](docs/server.md#self-update)).

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

A larger, modular graph with seed data, a graph skill and a setup script is in
[`examples/company`](examples/company/README.md).

A graph skill that tells agents what the graph holds is published with
`graph-admin graph skill publish` and installed with `--graph` (see
[Skills](docs/skills.md)).

## Documentation

New here? The [overview](docs/overview.md) explains what graph_cli is for and
how its parts fit together, [deployment options](docs/deployment_options.md)
how to run it and [design decisions](docs/design_decisions.md) why it is
built this way.

See [`docs/README.md`](docs/README.md) for the full documentation: the
command references, configuration, access control, stored files, agent
skills and the internals. To work on graph_cli itself, see
[`CONTRIBUTING.md`](CONTRIBUTING.md).
