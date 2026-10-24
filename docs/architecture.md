# Architecture

## Repository layout

```
packages/
  graph_cli/src/graph_cli/              application distribution
  graph_cli_admin/src/graph_cli_admin/  maintenance distribution
  graph_cli_server/src/graph_cli_server/ server distribution (graph-server)
tests/                                  tests for every distribution
skills/                                 agent skills (skill directories use `-`)
docs/                                   documentation
```

The kernel holds ids, canonical JSON, registries and the ports. The
ontology layer holds the schema IR `graph_cli.ir/1`: loader, validator,
compiler, semver classifier, JSON Schema export and data validation.

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
`graph_cli_admin`. `graph_cli_server` depends on `graph_cli`,
`graph_cli_admin` and `waitress`; of its modules only `admin_api` imports
`graph_cli_admin`; `graph_cli` never imports `graph_cli_server`, and only the server imports `waitress`. Within `graph_cli_admin`, `maintenance_cli` sits above
`services_admin`. Only the CLI layers may import the CLI framework (`click`),
and storage adapters, `audit` and `access_control` import only `kernel`.
`query.cypher` imports only `kernel` and `ontology_layer`, not the flag
planners next to it. The storage maintenance port
(`storage.files.maintenance`) is imported only by `graph_cli_admin`, and the
two storage backends are independent of each other. Authored YAML (ontologies,
policies) is parsed only by `ontology_layer` and `access_control`, through
the kernel's strict loader `kernel.yaml_strict`.

The cross-cutting packages have their own documents:
[Storage](storage.md), [Audit](audit.md) and
[Access control](access_control.md) (behavior; modules below).

## Server

`graph_cli_server` serves the `graph` command surface over HTTP
(`graph-server serve`). `app` is a small WSGI application: its command
routes come from the same declarations as the CLI (every command whose
`remote` is not `client`), `cli_kit.argv` turns a request's JSON parameters
into argv for the one click program, and `cli_kit.runner.run` runs it with an
`AppServices` per request whose `RequestScope` carries the request's actor
(`catalog.identity.request`), pointer, `GRAPH_CLI_GRAPH` and file origins.
`uploads` stages uploaded bytes per token, `openapi` derives the OpenAPI
documents from the inventories, `main` serves the app with `waitress` (one
process, a thread pool). `admin_api` serves the `graph-admin` declarations
the same way under `/v1/admin/`, with an admin `Services` per request whose
`AdminRequest` carries the request's actor (entrypoint `maint`); `main`
plugs it in only with `[server] admin_api`, so the app routes never load
maintenance code. Operating it: [Server](server.md).

The remote mode of `graph` is the other half: `app_cli.remote` gives every
command the server runs a handler that sends its parsed parameters
(`cli_kit.argv.request_args`) and returns the server's answer as
`Result.forwarded`, which `cli_kit.runner` writes as it came;
`app_cli.remote_hooks` adds the client work of the hooked commands
(uploads, downloads, origins), and the skills commands take graph skills
from the server. `maintenance_cli.remote` does the same for `graph-admin`:
it uploads file and directory parameters and writes the files of `graph
skill render` into `--out`.

## CLIs

Commands of `graph` are declared as data in `graph_cli.app_cli.commands`,
those of `graph-admin` in `graph_cli_admin.maintenance_cli.commands`
(`cli_kit.spec.CommandSpec`); for each program the click tree and the
inventory are built from the same declarations. Query planning (`graph_cli.query.plan`) turns `find`
and `neighbors` flags into the query AST, `graph_cli.query.lookup` plans,
matches and ranks `lookup`, `graph_cli.query.search` the same for `search`
(match keys and trigrams in `kernel.text`); `services.read` and `services.describe` answer
them. `graph_cli.query.cypher` holds the read-only Cypher subset: `lexer`
and `parser` (hand-written recursive descent) produce the syntax tree of
`ast`, `resolve` checks it against the model (names to CURIEs, fields,
parameters, scopes) and `render` writes a tree back as Cypher text.
`execute` matches the patterns over the `Snapshot` port and runs the
clauses (projection, aggregation, sorting, paging, `UNWIND`), `evaluate` and
`values` hold the expressions and openCypher's comparisons and null logic;
`services.read` parses before opening the graph, then resolves and runs.
`temporal_knowledge.history` names what changed between two revisions for
`history`.

`cli_kit` (envelope, error rendering, hints; `run` reports the bytes it
wrote, for the access log) is shared with the maintenance CLI; `graph_cli_admin.maintenance_cli.wiring` is the admin composition root
and `graph_cli_admin.services_admin` holds the procedures.

`kernel.timing.Timings` collects the phase timings of one `graph` command
for the access log: started at the first import of `graph_cli`, handed by
the composition root to `cli_kit.runner.run` and the services, which
measure their own phases (monotonic durations only, never wall time).

Every `graph` command is a new process, and its imports are most of a
read's time, so a command imports only what it runs. The composition root
builds the write and file services on first use, `services.read` imports
the Cypher engine in `parse_query` and `query` only, and the command
modules import the write, file and skill modules inside the handlers that
use them. `tests/cli/test_app_startup.py` checks which modules the reads
load. The versions are constants in `graph_cli/__init__.py` and
`graph_cli_admin/__init__.py`, not looked up with `importlib.metadata`;
`tests/test_smoke.py` keeps them equal to the `pyproject.toml` files.

## Catalog

`graph_cli.catalog` locates everything; both CLIs use the same code.

| Module | Content |
| --- | --- |
| `catalog.config` | `config.toml`: device id and name, runtime mode, data root, identity providers, lock timeout, durability, audit redaction, access log, output default, file roots, file size limit, read signal |
| `catalog.file_roots` | `FileRoots`: the named roots of this machine; the origin of a file and where an origin is here |
| `catalog.resolve` | config path, state directory and data root, without touching the filesystem |
| `catalog.file_catalog` | `FileCatalog`: graphs from `graphs/*/manifest.toml` (no registry file), name or id resolution, release files, `store_for` |
| `catalog.oplog` | the operational log in the state directory (recovery notes), best effort |
| `catalog.access_log` | the opt-in access log in the state directory: one line per `graph` command with the bytes delivered and the time per phase, best effort |
| `catalog.activity` | the read signal for `graph-admin view`: Unix datagrams to the viewer sockets in the state directory, fire and forget |
| `catalog.pointer` | the project pointer file `.graph_cli.toml` and graph selection (the policy decides visibility first) |
| `catalog.policy_source` | `FilePolicySource`: the installed policies, read once per command and again by a writer under its lock (`refresh`); the built-in policy without one; fails closed on an invalid file |
| `catalog.wiring` | the composition root of `graph`: config, roots, pointer, actor, services; writes the access log line after the envelope |
| `catalog.identity` | identity providers and the actor (see [Identity](access_control.md#identity)); `catalog.identity.request` builds the actor of a server request from its token and the client's facets |
| `catalog.endpoint`, `catalog.remote` | the remote mode: the server endpoint, the HTTP transport (`http.client`, imported in remote mode only) and the `RemoteSession` of one command |
| `catalog.build` | the build id an installed distribution carries (`_build_id`, written by `scripts/build_id.py`) and the `Graph-Cli-Build` header |
| `catalog.skill_claims` | skill freshness: the claims of the installed skills' markers (both scopes), the `Graph-Cli-Skills` header, and the check a read or write runs when it selects a graph (`SkillGate`); skill 1 is checked by `app_cli.skill_check` before any read or write. The names both this module and `graph_cli.skills` rely on (marker file and schema, skill names, slot kinds) are in `kernel.skill_marker` |
| `catalog.tokens` | bearer tokens of the server API: the `graph_cli.tokens/1` document, hashing, and the server's lock-free, cached store |

`catalog.identity` builds the one `ActorContext` per invocation. Only
`catalog` and the admin wiring may read the process environment.
Locations and keys: [Configuration](configuration.md).

## Access control modules

| Module | Content |
| --- | --- |
| `access_control.authorize` | `authorize()`: ceiling, deny rules, allow rules, obligations |
| `access_control.documents` | the compiled `graph_cli.policy/1` grammar, checked with a path per problem |
| `access_control.compiler` | authored YAML to compiled documents |
| `access_control.policy` | the built-in policy, `policy_set` (the layers of one decision and their hash) |
| `access_control.effective` | level per graph, the actions withheld below it, write obligations, scope and the unknown-project warning, for `whoami` and `policy explain` |

## Write pipeline

Every change to instance data is a change set (`graph_cli.changeset/1`) and
goes through one path, `graph_cli.services.write.WriteService`; it is the
only caller of `WriteSession.commit`.

| Module | Content |
| --- | --- |
| `kernel.changeset` | change set DTOs, the keys of the document, each op and an edge as data (the parser and skill 1's `references/changesets.md` read them), strict parsing (`changeset.malformed` with `details.path`), limits (1000 ops, 8 MiB), the canonical form and its hash, JSON/JSONL input readers |
| `services.refs` | references: full ids, aliases (a renamed alias still resolves, with a warning), `@local` names declared by creates, id abbreviations for CLI arguments |
| `temporal_knowledge.normalize` | applies the ops to a working copy: ids, defaults, initial lifecycle states, text coercion, aliases, supersede and retract, `derived_from` revisions; `WorkingState` reads like a snapshot of the result |
| `ontology_layer.data_validator` | `validate_write` (the post-state of every touched entity, plus write rules with a `count` or `sum` aggregate on the entities at the other end of their edges) and `check_graph` (every entity, batch rules included) |
| `ontology_layer.evaluate` | guards and rules of the compiled expression AST; exact `sum` over related entities; the aggregate values carried by rule findings |
| `services.write` | `WriteService.apply` and `apply_batch` |
| `services.files`, `services.media_types` | `FileService`: add a file (stream, hash, dedupe, origin, one create), `path`, `check`; the built-in media type table |

A write runs in this order: check the graph state and the pinned ontology
artifact, resolve references, authorize every (action, type) pair, take the
graph lock, re-check state and pin, replay an idempotent retry, check
`expect_seq`/`expect_rev`, normalize, validate, publish the stored files
the request staged and the transaction names, append the audit intent,
commit. Ops apply in order to one working copy, each touched entity gets
one new revision unless its content ends up equal to its current revision
(it is unchanged then), and a change set commits completely or not at all;
one that changes nothing stops after validation, without a transaction.
Validation reports findings (`data.*`) instead of stopping at the first
problem; error findings refuse the write (`write.invalid`), warnings ride
along with the result. Refusals are audited: denials in the installation
stream, invalid and conflicting writes in the graph's stream. A dry run
validates against the current state without the lock, without ids and
without any audit or file change. A JSONL batch commits one document per
transaction, carries local names to later documents and stops at the first
failure.
