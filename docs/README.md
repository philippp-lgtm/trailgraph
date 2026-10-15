# graph_cli

A domain-agnostic shell for knowledge graphs. Two command-line tools with a
strict split between usage and maintenance:

- `graph` (distribution `graph_cli`): the application CLI. Installed on
  `PATH`; reads and writes instance data.
- `graph-admin` (distribution `graph_cli_admin`): the maintenance CLI. Not on
  `PATH`; creates graphs, versions ontologies, archives graphs.

Status: foundation. The kernel (ids, canonical JSON, registries, ports), the
ontology layer (schema IR `graph_cli.ir/1`: loader, validator, compiler,
semver classifier, JSON Schema export, data validation), storage (FileStore
and InMemoryStore behind the storage ports), audit, authorization, the
catalog, the write pipeline (`WriteService`), the read services, `graph`
(reads, writes, `apply`, `describe`), `graph-admin` and the generated
`graph-cli` agent skill exist.

## Repository layout

```
packages/
  graph_cli/src/graph_cli/              application distribution
  graph_cli_admin/src/graph_cli_admin/  maintenance distribution
tests/                                  tests for both distributions
skills/                                 agent skills (skill directories use `-`)
docs/                                   documentation
```

## Layers

Dependencies point down only. Enforced by import-linter
(`[tool.importlinter]` in `pyproject.toml`) and checked by the test suite.

| Layer | `graph_cli` subpackage |
| --- | --- |
| CLI | `app_cli` |
| Skill generators | `skills` |
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
| `storage.common` | chained JSONL codec, fault points, commit checks, tx-log replay view (the reference query evaluator is `kernel.query_eval`) |
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
| `catalog.pointer` | the project pointer file `.graph_cli.toml` and graph selection |
| `catalog.wiring` | the composition root of `graph`: config, roots, pointer, actor, services |
| `catalog.identity` | identity providers and the actor (see above) |

Locations (XDG; macOS uses the same paths):

| What | Where |
| --- | --- |
| Config | `$XDG_CONFIG_HOME/graph_cli/config.toml`, else `~/.config/graph_cli/config.toml` |
| Data root | `graph-admin --data-root` (`graph` has no such flag), then `GRAPH_CLI_ROOT` (warns), then config `[paths] data_root`, then `$XDG_DATA_HOME/graph_cli`, else `~/.local/share/graph_cli` |
| State (operational log) | `$XDG_STATE_HOME/graph_cli`, else `~/.local/state/graph_cli` |

Unknown config or manifest keys are warnings; an unknown `format_version`
or an invalid value stops the command.

## Write pipeline

Every change to instance data is a change set (`graph_cli.changeset/1`) and
goes through one path, `graph_cli.services.write.WriteService`; it is the
only caller of `WriteSession.commit`.

| Module | Content |
| --- | --- |
| `kernel.changeset` | change set DTOs, strict parsing (`changeset.malformed` with `details.path`), limits (1000 ops, 8 MiB), the canonical form and its hash, JSON/JSONL input readers |
| `services.refs` | references: full ids, aliases (a renamed alias still resolves, with a warning), `@local` names declared by creates, id abbreviations for CLI arguments |
| `temporal_knowledge.normalize` | applies the ops to a working copy: ids, defaults, initial lifecycle states, text coercion, aliases, supersede and retract, `derived_from` revisions; `WorkingState` reads like a snapshot of the result |
| `ontology_layer.data_validator` | `validate_write` (the post-state of every touched entity) and `check_graph` (every entity, batch rules included) |
| `ontology_layer.evaluate` | guards and rules of the compiled expression AST |
| `services.write` | `WriteService.apply` and `apply_batch` |

A write runs in this order: check the graph state and the pinned ontology
artifact, resolve references, authorize every (action, type) pair, take the
graph lock, re-check state and pin, replay an idempotent retry, check
`expect_seq`/`expect_rev`, normalize, validate, append the audit intent,
commit. Ops apply in order to one working copy, each touched entity gets
one new revision, and a change set commits completely or not at all.
Validation reports findings (`data.*`) instead of stopping at the first
problem; error findings refuse the write (`write.invalid`), warnings ride
along with the result. Refusals are audited: denials in the installation
stream, invalid and conflicting writes in the graph's stream. A dry run
validates against the current state without the lock, without ids and
without any audit or file change. A JSONL batch commits one document per
transaction, carries local names to later documents and stops at the first
failure.

## Application CLI

`graph` is the agent and user surface; it reads and writes instance data of
existing graphs and never creates a data root or a graph. Output, exit codes
and hints are those of `graph-admin` (see below): the
`graph_cli.envelope/1` envelope as text or, with `--json`, as one JSON
document.

| Command | What it does |
| --- | --- |
| `whoami` | the acting identity (principal, agent, session, project) and what the policy allows |
| `graphs` | the graphs this project may use, with the title that says what each is for |
| `describe commands` | every command as data (`graph_cli.inventory/1`): arguments, options, examples, error codes, exit codes |
| `describe types`, `type T`, `fields T`, `relations`, `lifecycle T` | the selected graph's ontology (`graph_cli.model/1`) |
| `get REF` | one entity; superseded and retracted entities are shown as recorded, with a warning and the ids that replaced or withdrew them |
| `find` | entities by `--type`, `--where`, `--rel`/`--in-rel`; sorted, paged (`--limit`, `--offset`) with a next-page hint |
| `neighbors REF` | connected entities up to three hops (`--rel`, `--direction`, `--depth`) |
| `create TYPE`, `update REF`, `link REF REL TARGET`, `unlink REF ...` | single-op writes; `--set attr=value` is converted by the attribute's datatype, `--set-json attr=<json>` is typed |
| `transition REF STATE`, `supersede BY TARGET...`, `retract REF` | lifecycle and provenance; `supersede` and `retract` need `--reason` |
| `apply FILE` | change set documents (one JSON document or JSONL; `-` reads stdin) |

Graph selection: `--graph`, then `GRAPH_CLI_GRAPH`, then the `default` of
the nearest `.graph_cli.toml` up from the working directory. The pointer
file lists the graphs a project may use (`graphs`), optionally a `default`
and a `project` name for the actor; it cannot move the data root. There is
no implicit default graph.

References in arguments are full ids, aliases or 8-32 trailing hex digits of
an id; change set documents allow full ids, aliases and `@local` names only.
Writes take `--dry-run`, `--reason`, `--idempotency-key` and, on
single-entity commands, `--expect REV`. `find` filters are flags without
shell metacharacters: `--where attr=value` or `--where attr:op=value` with
`op` in `eq ne lt le gt ge contains prefix in exists`, all AND-ed. `apply`
reads and checks every document first (broken input writes nothing), then
commits one transaction per document in order and stops at the first
failure; `data.results` reports each processed document and the exit code is
that of the failure. Reads, `--help`, dry runs and usage errors write
nothing, audit files included. `--as-of` is reserved and refused.

Commands are declared as data in `graph_cli.app_cli.commands`
(`cli_kit.spec.CommandSpec`); the click tree and the inventory are built from
the same declarations. Query planning (`graph_cli.query.plan`) turns `find`
and `neighbors` flags into the query AST; `services.read` and
`services.describe` answer them.

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
with data is released only if the data has no error findings under the new
version (`check_graph`), otherwise the refusal lists them. Verification reports an
interrupted operation as info and anchors only graphs without errors.
`cli_kit` (envelope, error rendering, hints) is shared with the application
CLI; `graph_cli_admin.maintenance_cli.wiring` is the admin composition root
and `graph_cli_admin.services_admin` holds the procedures.

## Skills

`skills/graph-cli/` is the agent skill for `graph` (Agent Skills format:
`SKILL.md` with `name` and `description` frontmatter, details under
`references/`). It is generated, never edited by hand:
`graph_cli.skills.app_skill.render_app_skill` turns the app inventory
(`graph describe commands --json`, `graph_cli.inventory/1`) into
`SKILL.md` (workflow, habits, envelope, command overview, exit codes),
`references/commands.md` (arguments, options, limits and examples per
command) and `references/errors.md` (every error code and what to do per
category). The renderer reads nothing but that document and imports only
the kernel (import contract). The frontmatter `metadata` carries the
inventory hash and program version.

Regenerate after a change to a command declaration, then review the diff:

```sh
GRAPH_CLI_UPDATE_GOLDENS=1 uv run pytest tests/skills
```

The tests fail on a stale skill (the committed files must equal a fresh
render of the live inventory), check every generated table against the
inventory and the envelope keys against the renderer (docs-vs-code), and
reject leaks: fixture vocabulary, maintenance commands, actions or codes,
placeholders outside `<graph> <type> <id> <attr> <value> <relation>
<state> <file> <json> <seq>`, paths, ids and non-ASCII text. The skill
names no graph type; graph-specific skills come later.

## Tests

| Kind | Where | What |
| --- | --- | --- |
| unit | `tests/unit/` | modules in isolation |
| contract | `tests/contract/` | the storage ports and the write service on every backend and both toy ontologies; the seeds |
| golden | `tests/goldens/` | frozen on-disk formats and compiled artifacts |
| crash | `tests/crash/` | `kill -9` of a child process at every named fault point, for storage commits and for the write service with a retry; interrupted maintenance operations |
| concurrency | `tests/concurrency/` | real processes: lock timeout, no lost writes, alias uniqueness, reader isolation, conflicting `expect_rev` |
| tamper | `tests/tamper/` | hand-edited files: torn tails, interior damage, one scenario per storage and audit finding code |
| CLI | `tests/cli/` | `graph` and `graph-admin` as subprocesses: commands, envelopes, exit codes, one scenario per app and admin code; the P1 exit criteria through `graph` (failing audit sink, kill -9 then retry, parallel writers, nothing written by reads) |
| skills | `tests/skills/` | the committed `graph-cli` skill: freshness, docs-vs-code, leaks, structure |
| architecture | `tests/test_architecture.py`, `tests/test_source_rules.py` | import contracts with positive controls, source rules, wheel contents |

Crash and concurrency tests spawn processes and are marked `slow`; they run
by default and take a few seconds. Storage tests build transactions with a
test-only builder (`tests/support/txbuilder.py`) driven by the compiled toy
ontologies. Storage tests use minimal audit bodies
(`tests/support/drafts.py`); tests that verify audit streams use real events
(`tests/support/audit_events.py`). Write service tests run on both backends
through `tests/support/services.py` (an in-memory catalog, a builder with
test clock and seeded ids, and a factory for valid creates of any class).

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
GRAPH_CLI_UPDATE_GOLDENS=1 uv run pytest  # rewrite golden files and the generated skill after an intended change
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
code, one broken change set per `data.*` finding code, seed change sets for
both ontologies (`tests/fixtures/seeds/`, applied through the write
service), and role mappings for role-based contract tests
(`tests/support/roles.py`).
Core tests never name a fixture type; a leak test keeps fixture vocabulary out
of the packages. Deterministic test doubles (`FixedClock`, `StepClock`,
`SeededIdProvider`) live in `tests/support/`. Golden files live in
`tests/goldens/` and next to the fixtures they describe.
