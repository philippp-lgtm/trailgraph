# graph_cli

A domain-agnostic shell for knowledge graphs. Two command-line tools with a
strict split between usage and maintenance:

- `graph` (distribution `graph_cli`): the application CLI. Installed on
  `PATH`; reads and writes instance data.
- `graph-admin` (distribution `graph_cli_admin`): the maintenance CLI. Not on
  `PATH`; creates graphs, versions ontologies, archives graphs.

Status: foundation. The kernel (ids, canonical JSON, registries, ports), the
ontology layer (schema IR `graph_cli.ir/1`: loader, validator, compiler,
semver classifier, JSON Schema export) and storage (FileStore and
InMemoryStore behind the storage ports) exist. No commands beyond `--help`
and `--version` yet.

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
and storage adapters import only `kernel`. The storage maintenance port
(`storage.files.maintenance`) is imported only by `graph_cli_admin`, and the
two storage backends are independent of each other.

## Storage

`graph_cli.storage` implements the kernel's storage ports (read, write
session, maintenance, audit sink). It knows no ontology semantics: services
hand it fully computed transactions, and it checks only structural
invariants (sequence, revision chain, hashes, aliases, edge targets).

| Package | Content |
| --- | --- |
| `storage.common` | chained JSONL codec, fault points, commit checks, tx-log replay view, reference query evaluator |
| `storage.memory` | `InMemoryStore`, `InMemoryAuditLog`: the reference backend |
| `storage.files` | `FileStore` (one graph directory), `FileAuditLog` (installation audit stream), lock, durable writes, recovery |
| `storage.files.maintenance` | `FileStoreMaintenance`: `verify`, `recover`, `reindex` (admin only) |

FileStore in short (POSIX only, local filesystems only):

- The per-graph transaction log is the source of truth. Each line is the
  canonical JSON of one transaction, chained by hash. Revision files and the
  `head.json` checkpoint are derived and can be rebuilt from the log.
- One exclusive `flock` lock per graph serializes writers; readers take no
  lock and see a consistent snapshot (the last complete log line).
- A write appends the audit intent, then the transaction (the commit
  point), then materializes revision files and the checkpoint. Each step is
  synced (`F_FULLFSYNC` on macOS with `durability="full"`).
- Every write session first recovers: it truncates a torn last line and
  writes missing derived files. Damage inside the log stops writes with
  `backend.corrupt` until repair.

## Tests

| Kind | Where | What |
| --- | --- | --- |
| unit | `tests/unit/` | modules in isolation |
| contract | `tests/contract/` | the storage ports on every backend and both toy ontologies |
| golden | `tests/goldens/` | frozen on-disk formats and compiled artifacts |
| crash | `tests/crash/` | `kill -9` of a child process at every named fault point |
| concurrency | `tests/concurrency/` | real processes: lock timeout, no lost writes, alias uniqueness, reader isolation |
| tamper | `tests/tamper/` | hand-edited files: torn tails, interior damage, one scenario per storage finding code |
| architecture | `tests/test_architecture.py`, `tests/test_source_rules.py` | import contracts with positive controls, source rules, wheel contents |

Crash and concurrency tests spawn processes and are marked `slow`; they run
by default and take a few seconds. Storage tests build transactions with a
test-only builder (`tests/support/txbuilder.py`) driven by the compiled toy
ontologies.

## Development

Requires [uv](https://docs.astral.sh/uv/). uv provisions the Python version
from `.python-version`.

```sh
uv sync                     # create .venv with both distributions + dev tools
uv run pytest               # all tests (includes import contracts and wheel checks)
uv run pytest -m "not slow" # skip distribution builds, crash and process tests
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run lint-imports
GRAPH_CLI_UPDATE_GOLDENS=1 uv run pytest  # rewrite golden files after an intended change
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

### Fixtures

`tests/fixtures/` holds two unrelated toy ontologies (`notes_and_sources`,
`inventory` with a version series), one broken ontology per `model.*` finding
code, and role mappings for role-based contract tests (`tests/support/roles.py`).
Core tests never name a fixture type; a leak test keeps fixture vocabulary out
of the packages. Deterministic test doubles (`FixedClock`, `StepClock`,
`SeededIdProvider`) live in `tests/support/`. Golden files live in
`tests/goldens/` and next to the fixtures they describe.
