# graph_cli

A domain-agnostic shell for knowledge graphs. Two command-line tools with a
strict split between usage and maintenance:

- `graph` (distribution `graph_cli`): the application CLI. Installed on
  `PATH`; reads and writes instance data.
- `graph-admin` (distribution `graph_cli_admin`): the maintenance CLI. Not on
  `PATH`; creates graphs, versions ontologies, archives graphs.

Status: foundation. The kernel (ids, canonical JSON, registries, ports), the
ontology layer (schema IR `graph_cli.ir/1`: loader, validator, compiler,
semver classifier, JSON Schema export), storage (FileStore and InMemoryStore
behind the storage ports), audit, authorization, the catalog and
`graph-admin` exist. The application CLI `graph` has no commands beyond
`--help` and `--version` yet.

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
and storage adapters, `audit` and `access_control` import only `kernel`. The storage maintenance port
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
| `storage.files.manifest`, `storage.files.releases` | the `manifest.toml` codec; reading manifests and installed ontology releases |
| `storage.files.maintenance` | `FileStoreMaintenance`: graph creation, manifest switch, release install, `verify`, `recover`, `reindex` (admin only) |

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
- Maintenance effects are published with one rename each: a new graph
  directory (staged as `graphs/.tmp-<id>`), an ontology release directory,
  the manifest. A failure before the rename leaves only the staging state,
  which readers ignore.

## Audit, authorization and identity

`graph_cli.audit` builds and verifies `graph_cli.audit/1` events. It is
pure: storage appends and chains the events, audit never touches files.

| Module | Content |
| --- | --- |
| `audit.events` | builders for write intents, refusals, maintenance events, graph genesis and anchors; each routes its event to the graph or the installation stream |
| `audit.schema` | the event shape; builders refuse their own output if it misses it |
| `audit.verify` | `verify_stream` over raw lines, `cross_check` (tx log against write intents), `check_maintenance` (release and pin intents against their effects, pin events against the manifest), `check_anchors`, and `verify_graph` / `verify_installation` composing them |
| `audit.finding_codes` | `integrity.audit.*` |

The verifier reads raw lines (`AuditSink.read_lines`) and reports damage as
findings instead of stopping at it. Each damaged line gives at most one chain
finding, so one deleted or edited line does not flood the report. A write
intent without a committed transaction is the benign trace of an interrupted
write (info); a transaction without its intent is an error. Events carry
ids, types, hashes and reason text, never attribute values.

`graph_cli.access_control.authorize()` is the one decision point. It is pure
and answers for one (action, resource) pair: the app entrypoint never gets a
`maint.*` action, then the first matching allow rule of the policy, else
deny, then the obligations (`require_reason`, `max_batch`). Version 1 reads
no policy files; `BuiltinPolicySource` serves the built-in policy.

`graph_cli.catalog.identity` builds the one `ActorContext` per invocation.
The OS user is the verified principal. Agent and session are asserted, from
the `GRAPH_CLI_AGENT_ID`, `GRAPH_CLI_AGENT_TYPE`, `GRAPH_CLI_SESSION_ID` and
`GRAPH_CLI_PARENT_SESSION_ID` variables first, then from the coding agent
harness. Malformed values are dropped with a warning and never block. Only
`catalog` and the admin wiring may read the process environment.

## Catalog and configuration

`graph_cli.catalog` locates everything; both CLIs use the same code.

| Module | Content |
| --- | --- |
| `catalog.config` | `config.toml`: device id and name, runtime mode, data root, identity providers, lock timeout, durability, audit redaction, output default |
| `catalog.resolve` | config path, state directory and data root, without touching the filesystem |
| `catalog.file_catalog` | `FileCatalog`: graphs from `graphs/*/manifest.toml` (no registry file), name or id resolution, release files, `store_for` |
| `catalog.oplog` | the operational log in the state directory (recovery notes), best effort |
| `catalog.identity` | identity providers and the actor (see above) |

Locations (XDG; macOS uses the same paths):

| What | Where |
| --- | --- |
| Config | `$XDG_CONFIG_HOME/graph_cli/config.toml`, else `~/.config/graph_cli/config.toml` |
| Data root | `graph-admin --data-root`, then `GRAPH_CLI_ROOT` (warns), then config `[paths] data_root`, then `$XDG_DATA_HOME/graph_cli`, else `~/.local/share/graph_cli` |
| State (operational log) | `$XDG_STATE_HOME/graph_cli`, else `~/.local/state/graph_cli` |

Unknown config or manifest keys are warnings; an unknown `format_version`
or an invalid value stops the command.

## Maintenance CLI

`graph-admin` is run from the repository checkout. Every command prints the
`graph_cli.envelope/1` envelope: text by default, one JSON document with
`--json` (or `GRAPH_CLI_OUTPUT=json`). Exit codes follow the error category
(0 ok, 1 refused or invalid, 2 usage, 3 denied, 4 conflict, 5 unavailable,
6 internal error).

| Command | What it does |
| --- | --- |
| `init` | create the data root and, if missing, `config.toml` with a device id |
| `graph create NAME --ontology DIR` | create a graph pinned to the first release of an authored ontology |
| `graph list`, `graph show REF` | list graphs; show manifest, head and installed releases |
| `ontology validate DIR` | compile and check an authored ontology |
| `ontology diff DIR --against DIR` / `--graph REF` | classify the changes and check the version bump |
| `ontology release GRAPH DIR` | publish a release into the graph and pin the graph to it |
| `audit verify` | check every chain, tx log, revision file and manifest pin; exit 1 on error findings |
| `audit anchor` | record every graph stream's head in the installation stream |

Every change is audited: an intent in the graph's stream, the effect, then
an installation event anchoring the intent. A release must bump the version
at least as much as the changes require; a new major version on a graph
with data is refused until migrations exist. Verification reports an
interrupted operation as info and anchors only graphs without errors.
`cli_kit` (envelope, error rendering, hints) is shared with the application
CLI; `graph_cli_admin.maintenance_cli.wiring` is the admin composition root
and `graph_cli_admin.services_admin` holds the procedures.

## Tests

| Kind | Where | What |
| --- | --- | --- |
| unit | `tests/unit/` | modules in isolation |
| contract | `tests/contract/` | the storage ports on every backend and both toy ontologies |
| golden | `tests/goldens/` | frozen on-disk formats and compiled artifacts |
| crash | `tests/crash/` | `kill -9` of a child process at every named fault point; interrupted maintenance operations |
| concurrency | `tests/concurrency/` | real processes: lock timeout, no lost writes, alias uniqueness, reader isolation |
| tamper | `tests/tamper/` | hand-edited files: torn tails, interior damage, one scenario per storage and audit finding code |
| CLI | `tests/cli/` | `graph-admin` as a subprocess: commands, envelopes, exit codes, one scenario per admin code |
| architecture | `tests/test_architecture.py`, `tests/test_source_rules.py` | import contracts with positive controls, source rules, wheel contents |

Crash and concurrency tests spawn processes and are marked `slow`; they run
by default and take a few seconds. Storage tests build transactions with a
test-only builder (`tests/support/txbuilder.py`) driven by the compiled toy
ontologies. Storage tests use minimal audit bodies
(`tests/support/drafts.py`); tests that verify audit streams use real events
(`tests/support/audit_events.py`).

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
