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
examples/                               example graphs
docker/, Dockerfile, compose.yaml       the server image and its deployment files
scripts/                                build, CI and benchmark scripts
```

The kernel holds ids, canonical JSON, registries and the ports. The
ontology layer holds the schema IR `graph_cli.ir/1`: loader, validator,
compiler, semver classifier (with the rename checks), the migration file format (`ontology_layer.migration`),
JSON Schema export and data validation.

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
| Named read functions and write actions; source planners; file extractors | `kinetic`, `sources`, `extractors` (independent of each other) |
| Query model | `query` |
| Schema IR | `ontology_layer` |
| Kernel | `kernel` |

`graph_cli_admin` depends on `graph_cli`; `graph_cli` never imports
`graph_cli_admin`. `graph_cli_server` depends on `graph_cli`,
`graph_cli_admin` and `waitress`; of its modules only `admin_api` and `enroll`
import `graph_cli_admin`; `graph_cli` never imports `graph_cli_server`, and
only the server imports `waitress`. Within `graph_cli_admin`, `maintenance_cli`
sits above
`services_admin`. Only the CLI layers may import the CLI framework (`click`),
and storage adapters, `audit` and `access_control` import only `kernel`.
`query.cypher` imports only `kernel` and `ontology_layer`, not the flag
planners next to it. `kinetic` imports only `kernel`, `ontology_layer` and
`query`; the layers above may use it. `sources` imports only `kernel` and
`ontology_layer`. `extractors` imports only `kernel` and the standard library; of its modules only the PDF extractor
imports the one third-party parser (`pypdf`, deferred into the child process), and no other
package may import it; it knows nothing of graphs. The storage maintenance port
(`storage.files.maintenance`) is imported only by `graph_cli_admin`, and the
two storage backends are independent of each other. The S3 client
(`storage.s3`) imports only `kernel` and the standard library. Authored YAML (ontologies,
policies, function files) is parsed only through the kernel's strict loader
`kernel.yaml_strict`, by `ontology_layer`, `access_control` and `kinetic`.

The cross-cutting packages have their own documents:
[Storage](storage.md), [Audit](audit.md) and
[Access control](access_control.md) (behavior; modules below).

## Server

`graph_cli_server` serves the `graph` command surface over HTTP
(`graph-server serve`). `app` is a WSGI application: its command
routes come from the same declarations as the CLI (every command whose
`remote` is not `client`), `cli_kit.argv` turns a request's JSON parameters
(by API or inventory name, structured forms included) into the argv of their
command-line form for the one click program, and `cli_kit.runner.run` runs it with an
`AppServices` per request whose `RequestScope` carries the request's actor
(`catalog.identity.request`), pointer, `GRAPH_CLI_GRAPH` and file origins.
`uploads` stages uploaded bytes per token, `dist` reads the wheels the
server hands out under `/v1/dist/` once at start, `openapi` derives the OpenAPI
documents from the inventories with output schemas, `http_status` maps an
error to its HTTP status for the routes and the documents, `problems` turns a
failed envelope into RFC 9457 problem details and answers the error registry, `main` serves the app with `waitress` (one
process, a thread pool). `admin_api` serves the `graph-admin` declarations
the same way under `/v1/admin/`, with an admin `Services` per request whose
`AdminRequest` carries the request's actor (entrypoint `maint`); `main`
plugs it in only with `[server] admin_api`. `enroll` serves the enrollment
exchange (`POST /v1/enroll`) with the same admin `Services` because it writes
the token files under the registry lock, audited; `main` always plugs it in,
so it is the one maintenance code the app routes load without the admin API.
Operating it: [Server](server.md).

The remote mode of `graph` is the other half: `app_cli.remote` gives every
command the server runs a handler that sends its parsed parameters by API
name (`cli_kit.argv.request_args`, `api_args`) and returns the server's answer as
`Result.forwarded`, which `cli_kit.runner` writes as it came;
`app_cli.remote_hooks` adds the client work of the hooked commands
(uploads, downloads, origins), and the skills commands take graph skills
from the server. `maintenance_cli.remote` does the same for `graph-admin`:
it uploads file and directory parameters and writes the files of `graph
skill render` and `graph pull` into their directories. `workspace sync`
runs on the client and calls the handlers of `graph list` and `graph pull`
the mode selects, so it needs no route of its own.

## CLIs

Commands of `graph` are declared as data in `graph_cli.app_cli.commands`,
those of `graph-admin` in `graph_cli_admin.maintenance_cli.commands`
(`cli_kit.spec.CommandSpec`); for each program the click tree and the
inventory are built from the same declarations. A declaration also names the
JSON Schema of its answer's `data` (`output`); shapes several commands share
are definitions with one home each, next to what produces them:
`cli_kit.output_schemas` (the envelope's parts, the inventory and the
helpers to write schemas), `app_cli.output_schemas` (entity, write result,
actor, model parts) and `graph_cli_admin.maintenance_cli.output_schemas`
(ontology pin, diff and release, finding counts, policy document, token,
enrollment). The admin skills are package data of `graph_cli_admin`
(`bundled_skills/`), read by `maintenance_cli.admin_skills` and installed
with `graph_cli.skills.install`, whose scan sees only one owner's skills
(`Owner`: marker kinds and names). Query planning (`graph_cli.query.plan`) turns `find`
and `neighbors` flags into the query AST, `graph_cli.query.lookup` plans,
matches and ranks `lookup`, `graph_cli.query.search` the same for `search`
(match keys and trigrams in `kernel.text`); `services.read` and `services.describe` answer
them; `services.schema_search` ranks the terms of the model for `describe search` (no graph data).
`graph_cli.query.cypher` holds the read-only Cypher subset: `lexer`
and `parser` (hand-written recursive descent) produce the syntax tree of
`ast`, `resolve` checks it against the model (names to CURIEs, fields,
parameters, scopes) and `render` writes a tree back as Cypher text.
`execute` matches the patterns over the `Snapshot` port and runs the
clauses (projection, aggregation, sorting, paging, `UNWIND`), `evaluate` and
`values` hold the expressions and openCypher's comparisons and null logic;
`services.read` parses before opening the graph, then resolves and runs.
`temporal_knowledge.history` names what changed between two revisions for
`history`; `temporal_knowledge.changes` holds the pure core of `changes`
(positions, the one read of a raw tx body, edge targets, the paging of a page).

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
load. The versions are constants in the `__init__.py` of each package, not
looked up with `importlib.metadata`;
`tests/test_smoke.py` keeps them equal to the `pyproject.toml` files.

## Catalog

`graph_cli.catalog` locates everything; both CLIs use the same code.

| Module | Content |
| --- | --- |
| `catalog.config` | `config.toml`: device id and name, runtime mode, data root, identity providers, lock timeout, durability, audit redaction, access log, output default, file roots, file size limit, the file store (`[files] store`, the shape of `[files.s3]`), read signal |
| `catalog.object_store` | the object store settings (`ObjectStoreSettings`) from `[files.s3]` and the environment, handed to the catalog and, through the admin wiring, to the maintenance port; storage never reads either |
| `catalog.file_roots` | `FileRoots`: the named roots of this machine; the origin of a file and where an origin is here |
| `catalog.file_cache` | `StateFileCache`: this machine's file cache `<state>/files/<graph id>/<hex>`, the `FileCache` port of `services.files` (read-only copies, unique partial files, hash check, use time, 24-hour expiry), shared by `file path` in local mode and the remote client's download |
| `catalog.resolve` | config path, state directory and data root, without touching the filesystem |
| `catalog.file_catalog` | `FileCatalog`: graphs from `graphs/*/manifest.toml` (no registry file), name or id resolution, release files, `store_for` (after the manifest check of `storage.files.store_format`); the app's catalog (`follow_migration`) pins a graph whose migration is committed but not pinned to the migration's target version, so reads see the names the data has |
| `catalog.registries` | the union of every component's finding codes (the catalog sits above every owner; codes are unique across owners) |
| `catalog.spill` | the files for JSON answers too large for stdout (`[output] spill_bytes`): where they go and their cleanup; `cli_kit.runner` decides when an answer spills |
| `catalog.oplog` | the operational log in the state directory (recovery notes), best effort |
| `catalog.access_log` | the opt-in access log in the state directory: one line per `graph` command with the bytes delivered and the time per phase, best effort |
| `catalog.activity` | the read signal for `graph-admin view`: command, reader, session, entity and edge ids as Unix datagrams to the viewer sockets in the state directory, fire and forget |
| `catalog.pointer` | the project pointer file `.graph_cli.toml` and graph selection (the policy decides visibility first) |
| `catalog.policy_source` | `FilePolicySource`: the installed policies, read once per command and again by a writer under its lock (`refresh`), or again whenever a file was replaced (`watch`, for the long-running view); the built-in policy without one; fails closed on an invalid file |
| `catalog.wiring` | the composition root of `graph`: config, roots, pointer, actor, services; writes the access log line after the envelope |
| `catalog.identity` | identity providers and the actor (see [Identity](access_control.md#identity)); `catalog.identity.request` builds the actor of a server request from its token and the client's facets |
| `catalog.endpoint`, `catalog.remote` | the remote mode: the server endpoint, the HTTP transport (`http.client`, imported in remote mode only) and the `RemoteSession` of one command |
| `catalog.build` | the build id an installed distribution carries (`_build_id`, written by `scripts/build_id.py`) and the `Graph-Cli-Build` header |
| `catalog.compat` | the compatibility id of a program (wire level, inventory with answer schemas, agent skills; computed by `app_cli` and `graph_cli_admin`, cached per build) and the `Graph-Cli-Compat` header |
| `catalog.dist` | the programs the origin hands out (`/v1/dist/` manifest), fetching a wheel and installing it as a uv tool into the environment the program runs from (`graph self update`, `graph-admin self update`) |
| `catalog.skill_claims` | skill freshness: the claims of the installed skills' markers (both scopes), the `Graph-Cli-Skills` header, and the check a read or write runs when it selects a graph (`SkillGate`); skill 1 is checked by `app_cli.skill_check` before any read or write. The names both this module and `graph_cli.skills` rely on (marker file and schema, skill names, slot kinds) are in `kernel.skill_marker` |
| `catalog.tokens` | bearer tokens of the server API: the `graph_cli.tokens/1` document, hashing, and the server's lock-free, cached store |
| `catalog.enrollments` | enrollment codes: the `graph_cli.enrollments/1` document, codes, lifetimes, the constant-time lookup and default token names |

`catalog.identity` builds the one `ActorContext` per invocation. Only
`catalog` and the admin wiring may read the process environment.
Locations and keys: [Configuration](configuration.md).

## Source planners

`graph_cli.sources` holds the pure planners for data that external sources hand over;
like `kinetic` it needs no storage and no services, and the layers above use it.

| Module | Content |
| --- | --- |
| `sources.reconcile` | `plan_reconcile` turns the `reconcile` part of a change set document into ops over a `Snapshot` and the model: key namespace of each scope, moves and adoptions, field and edge diff after the coercion a write applies (`ontology_layer.stored_values`), retire and revive, the guards that refuse a document (`reconcile.*` codes), op order and the per-scope counts; `expanded` appends the plan to the document's own ops |

## File extractors

`graph_cli.extractors` holds versioned extractors of type-specific file metadata and the
isolated run of them. It needs no storage and no services; the layers above use it. An
extractor is a deterministic function of the file's bytes and the media type the file was
recorded with, and its `version` is part of its result's cache key.

| Module | Content |
| --- | --- |
| `extractors.media_types` | the built-in media type table: the type a file is recorded with at add (magic bytes of the first 16 bytes, else the extension) |
| `extractors.detect` | the type decided from the bytes alone (signatures, SQLite, zip and OpenDocument, gzip, a text test with the encoding) and `compatible`, which says whether the recorded type can be true of the detected one |
| `extractors.model` | `Extractor` (name, version, module, record class, `accepts`, `run`), `ExtractionResult` (`ok` with values and parts, or `failed` with a stable problem code), `Transient`, and the problem codes of the limits |
| `extractors.format`, `pdf`, `image`, `table`, `sqlite` | the extractors: detected media type, size and mismatch with the recorded type; page count; pixel size; delimiter and shape of CSV and TSV; the schema of a SQLite database (opened read only and immutable, only its schema read) |
| `extractors.text` | the one rule for a name that comes from a file: one printable line, cut by characters and bytes, with a fallback |
| `extractors.registry` | the static registry by name (code, not configuration) |
| `extractors.child` | the child process entry point: one JSON request on standard input, one JSON answer on standard output, CPU and address space limits applied before the file is touched |
| `extractors.runner` | `run_extractor` starts the child with an empty environment and a wall-clock timeout and maps how it ended (answer, time limit, crash, killed from outside); `materialized` writes streamed bytes to a private temporary file for graphs whose files have no local path |

## Named read functions and write actions

`graph_cli.kinetic` holds the pure part of named read functions and of named
write actions: documents in, documents out, no storage and no services. The two
kinds share the parameter grammar and binding (`kinetic.params`).

| Module | Content |
| --- | --- |
| `kinetic.params` | what functions and actions share, parametrized by kind: `ParamDef` and the parameter grammar, the strict-YAML `Parser` base with its readers, the `Problem` type of every check, parameter values from CLI text or JSON by the declared type with defaults and typed placeholders (an action may declare an optional parameter without default) |
| `kinetic.format` | `graph_cli.function/1`: the typed definition of one function file, strict YAML loading with a `Problem` per defect (shape, names, lengths) |
| `kinetic.compile` | `compile_catalog` checks the function files of a graph against a compiled ontology and writes the catalog document `graph_cli.functions/1`; `check_catalog` runs the same checks over a published catalog against another model |
| `kinetic.entries` | the entries of a published catalog as the app shows them: signature text, the summary and detail documents of `describe functions` and `describe function` |
| `kinetic.bind` | the function-kind entry points of the binding in `kinetic.params` |
| `kinetic.action_format` | `graph_cli.action/1`: the typed definition of one action file (parameters, preconditions as rule-language conditions, a template of change-set ops with `$param` placeholders and the one dynamic form `unlink ... all: true`), strict YAML loading with a `Problem` per defect |
| `kinetic.action_compile` | `compile_actions` checks the action files of a graph against a compiled ontology (names, parameters, ops, that the template assembles into a change set, preconditions, limits) and writes the catalog document `graph_cli.action_catalog/1` with the compiled ops, preconditions, `touches` and the `write.*` pairs the ops need; `check_actions` runs the same checks over a published catalog against another model |
| `kinetic.action_entries` | the entries of a published action catalog as the app shows them: the summary and detail documents, `find_action` |
| `kinetic.action_needs` | `action_needs`: what an action's ops need on concrete types against a model, with the attributes and relations they write (declared classes expanded to their non-abstract subtypes, any of which meets a need); pure, no policy |
| `kinetic.affordances` | which published actions and functions take a given entity as a parameter, judged from the entries the actor may see, for `get --affordances` (`actions_for`, `functions_for`; pure) |
| `kinetic.card` | the entity card of `graph view`, generated or authored: a `ViewSpec` derived from the ontology (`generated_spec`) or from a published entity view (`spec_of`, with `where` and `order` planned by `query.plan` for the bound parameters), `render` over a `Snapshot` and the reader's model into a `ViewModel` (`fields`, `relations`, `files`, `affordances`, `neighbors`, `rows` and, for set views, `list` sections, bounded loads, a byte limit with a deterministic trim that works section by section in a fixed order; pure, nothing stored) |
| `kinetic.view_rows` | the row builder of views: an entity as a lean row (reference, label, requested attributes, per-row neighbour lists with their own `where`, `order` and `max`), read through the reader's `Snapshot` under a shared budget of loads; the result type of a `rows` section |
| `kinetic.view_page` | one page of a set view: `render_set` fills a page with whole rows in section order (type listings read through the reader's `Snapshot` with the `find` order, rows of published functions) up to a byte limit and a budget of loads, `Cursor` (`v1.<seq>.<section>.<offset>.<key>`) with `cursor_key`, `decode_cursor` and the stale and invalid checks; pure, same input and same bytes |
| `kinetic.view_values` | the text and value helpers the card and the row builder share (cut strings and lists, main label, label order, lifecycle state) |
| `kinetic.view_format` | `graph_cli.view/1`: the typed definition of one authored view file (an entity view that takes a class or a set view that takes none; sections `fields`, `relations`, `files`, `affordances`, `neighbors`, `list` and `rows`; parameters in the function grammar; `where` and `order` in the `find` grammar with whole-value `$name` placeholders), strict YAML loading with a `Problem` per defect (shape, flavour, limits, placeholders only as whole values) |
| `kinetic.view_compile` | `compile_views` checks the view files of a graph against a compiled ontology and the function catalog the graph will have (terms, relation ends, `where` and `order` planned with the `find` planner, parameter and argument types, one default view per class) and writes the catalog document `graph_cli.view_catalog/1` with the normalized definition, the authored source, `touches` and the hash; `check_views` runs the same checks over a published catalog against another model and function catalog; `deprecated_function_uses` lists the warnings |
| `kinetic.view_entries` | the entries of a published view catalog as the app shows them: the summary and detail documents, `find_view` |
| `kinetic.cover` | which published functions cover an ad-hoc query, for the hint and the access log of `query`: structural signatures of query and function, compared (pure, no ranking model) |
| `kinetic.expand` | over a `Snapshot`: `check_preconditions` evaluates the preconditions on the current state (every failure in one refusal), `expand` turns bound parameters into a `ChangeSet` and replaces `all: true` by one exact unlink per current edge |

The checks use the query engine without data: `query.cypher.parameters` lists the
`$parameters` of a parsed query, `resolve` runs with a typed placeholder per
parameter, and `ontology_layer.connecting` supplies the hint of an impossible
relationship pattern.

The layers above use it in these places: `services.read` and `services.describe`
load the published catalogs next to the model (`describe functions|function`,
`describe actions|action`, `run`, the search kinds `function` and `action`,
`lookup --for`); `services.actions` and `services.write` run an action for the
`do` command of `app_cli` (the visible action, binding, then expansion and
preconditions under the graph lock); `skills` receive the
compiled catalog as one more document and render the `functions` block and
pages (the actions only as far as the installing reader may run them);
`graph_cli_admin.services_admin.functions` and `.actions` publish a folder of
function or action files into the graph's slot (one maintenance operation,
audited as `change.functions` or `change.actions`); and `services_admin.release` and `sources` check the
published catalogs against the model a release gives the graph, so a catalog
that stays published always resolves against the pinned ontology. The slot
layout is in [Storage](storage.md#filestore), its verification in
[Audit](audit.md#slot-verification), the file formats in the authoring skills of
the admin skills ([Skills](skills.md#admin-skills)).

## Access control modules

| Module | Content |
| --- | --- |
| `access_control.authorize` | `authorize()`: ceiling, deny rules, allow rules, obligations |
| `access_control.documents` | the compiled `graph_cli.policy/1` grammar, checked with a path per problem, `fine_types` (the types some rule names terms of) and `read_limited` (whether some rule decides the visibility of types) |
| `access_control.compiler` | authored YAML to compiled documents |
| `access_control.policy` | the built-in policy, `policy_set` (the layers of one decision and their hash) |
| `access_control.terms` | what the compiler needs of a graph's model (`TermModel`: per type its attributes, relations, the ranges of its required relations and lifecycle flags) and what it finds out (unknown types and terms, unusable writes, creates blocked by a hidden range); the resources of attributes and relations (`term_resource`) and which types and terms installed rules name (`named_terms` and `named_read_types`, for the checks of an ontology release) |
| `access_control.effective` | level per graph, the actions withheld below it, write obligations, scope and the unknown-project warning, `write_allowance` (what a write allow list lets a caller write, per type and verb), `visibility` (the concrete types a caller sees, one `read.get` decision per type) and `read_allowance`, for `whoami` and `policy explain` |

## Read views of limited callers

A caller with [read limits](access_control.md#read-limits) reads through views
that are opened in one place, `ReadService.open` (and `_snapshot_as_of` for
`--as-of`), and handed to every read command in `GraphRead`. A graph without
read rules gets the raw snapshot and model, so nothing is filtered and nothing
is paid.

| Module | Content |
| --- | --- |
| `services.visibility` | `Visibility`: the `ReadView` per actor and graph (one `access_control.effective.visibility` decision per type, one `held_markings` probe per marking the pin uses, no decision recorded), cached per policy hash, pin and actor facts; `VisibleSnapshot`, a `Snapshot` that answers for a hidden entity as for a missing one, strips every revision it hands out and sets `Query.visible`; `filter_model`, the model without hidden types and what depends on them |
| `services.visible_catalog` | which functions, actions and views a limited caller sees: those whose terms and parameter classes all exist in the filtered model and that still compile against it |
| `services.view_call` | the storage-free parts of a `graph view` call: the default view of an entity's class, the key a page cursor is tied to (view, parameters, function catalog, pin, what the reader sees), the share of the query budget each `rows` section gets, and the lazily run `rows` results |
| `kernel.query_eval`, `kernel.records`, `kernel.read_view` | `Query.visible` and `NeighborQuery.visible`, honoured in one place for every store (subjects, relation ends, traversal, totals); `Query.attributes` and `NeighborQuery.attributes`, a per-type allow list of attribute names applied before matching and ordering when the query names a masked attribute; `strip_revision`, which drops the edges into hidden entities and the attributes outside the allow list from a revision in one copy and recomputes its content hash for revisions handed out; `ReadView`, the hashable view key (types and attributes) the caches and the skill claim digest use |
| `temporal_knowledge.migrate` | `plan_migration`: the new revision of every current revision (all record statuses) that a major release's renames and migration steps change, as a pure function of the old and new model, the derived renames, the migration file and a snapshot; no-op detection by content hash, per-step counts and samples, an overlay for `check_graph` and the blobs of the migrated revisions; `WorkingState` answers `incoming` from a reverse-edge index built on first use, so a large overlay is judged in linear time |
| `services.migration`, `kernel.migration_marker` | `build_migration_tx`: the `AuthorizedTx` of a migration plan (changeset `graph_cli.migration/1` with no ops and a `migration` record, no aliases, no idempotency key), committed only by `MaintenanceSession.migrate`; `MigrationMarker` and `marker_committed`, the note of a migration that is not finished and the identity check of whether its tx is in the log |
| `services.migration_state` | the app's view of a migration that is committed but not pinned (`GraphEntry.migration`, set by the app's catalog only while the pin is not switched; a marker file left after the switch is no state): `refuse_write` (`write.migration_incomplete`, for `WriteService` before and under the lock) and `read_warning` (`graph.migration_incomplete` when no writer holds the lock, which means the release crashed) |
| `services.names` | read-time mapping: `NameMaps` builds the `NameMap` of a graph at its pin from the pin history and the installed release files, `MappedSnapshot` hands out past revisions under the pin's names; a server builds its read service per request and passes every one the same `NameMapCache` (complete maps by graph and pin, bounded, thread safe) |
| `graph_cli_admin.services_admin.migration` | the migration of a release for the maintenance tools: parse and judge `migration.yaml` against the old and new model (`migration.invalid`, `admin.migration_unexpected`, `admin.migration_from_mismatch`), plan the data change with the stamp of its tx, judge the migrated overlay (`admin.migration_required`), describe it for the answers; `services_admin.release` writes it, `check` and `sources` use it |
| `temporal_knowledge.name_map` | `NameMap`: the renames and re-keyings between consecutive pinned versions (`Hop`s from the two compiled models and the installed `migration.yaml`) composed from a revision's version to the pin; `MappedSnapshot` and the mapped revisions are built on it in `services.names` |
| `app_cli.rename_hints` | `term.unknown` for an old name says what it is called now (message and `details.renamed`), around every command; the old name is never accepted |
| `graph_cli_admin.services_admin.catalog_follow` | published function and action catalogs across a release with renames: `follow` decides nothing to do, republish (same sources, new compiled document) or refuse; `write` is the catalog switch shared with `graph <kind> publish` and `graph push` |
| `graph_cli_admin.services_admin.renamed_terms` | what a release that renames terms does to policy and catalogs: `hazards` (deny rules that would apply to nothing, `admin.policy_renamed_term`) and `references` (where functions, actions and policy name each renamed term) |
| `ontology_layer.data_validator` | `CallerView`: on the write path, validation judges the whole graph and the view only decides what a finding may say (concealed findings, unknown type and relation answers over the filtered model) |

The write path keeps two snapshots for such a caller: the raw one is the truth
(normalizing, the conflict check, validation, the `before` hashes) and a
`VisibleSnapshot` of it serves what the caller reaches (references, action
expansion, preconditions, file lookups). `access_control.authorize.authorize_unlisted_type`
decides the answer to a create of a hidden type, so a limit never shows in the
refusal.

## Write pipeline

Every change to instance data is a change set (`graph_cli.changeset/1`) and
goes through one path, `graph_cli.services.write.WriteService`; it is the
only caller of `WriteSession.commit`. The one other tx, a data migration, is built by
`services.migration` and committed by `MaintenanceSession.migrate`.

| Module | Content |
| --- | --- |
| `kernel.changeset` | change set DTOs, the keys of the document, each op and an edge as data (the parser and skill 1's `references/changesets.md` read them), strict parsing (`changeset.malformed` with `details.path`), limits (operations and bytes per document), the canonical form and its hash, JSON/JSONL input readers |
| `services.refs` | references: full ids, aliases (a renamed alias still resolves, with a warning), `@local` names declared by creates, id abbreviations for CLI arguments; the (action, type) pairs and the attributes and relations an op is decided on; `entity_params`, the existence and class check of `ref` parameters shared by `run` and `do` |
| `services.term_model`, `services.action_rights` | the app side of term rules: a compiled model condensed for the policy compiler and for the checks of an ontology release; which types a policy decides terms of (`fine_types`, cached per policy hash) and the any-of rule that lists an action and authorizes an empty expansion |
| `temporal_knowledge.normalize` | applies the ops to a working copy: ids, defaults, initial lifecycle states, text coercion, aliases, supersede and retract, `derived_from` revisions; `WorkingState` reads like a snapshot of the result |
| `ontology_layer.data_validator` | `validate_write` (the post-state of every touched entity, plus write rules with a `count` or `sum` aggregate on the entities at the other end of their edges) and `check_graph` (every entity, batch rules included) |
| `ontology_layer.connecting` | the relations and two-hop paths that connect two types, from the compiled model alone; used for the `connecting` of domain and range findings |
| `ontology_layer.evaluate` | guards and rules of the compiled expression AST; exact `sum` over related entities; the aggregate values carried by rule findings |
| `services.write` | `WriteService.apply` and `apply_batch`; a request may carry an action plan (a published action and its bound parameters), which `apply` expands and checks on the locked snapshot; authorizes each op on its type and, for types a rule names terms of, on the attributes and relations it writes |
| `services.reconcile_rights` | the declared needs of the scopes of a reconcile document as needs of write actions (`NeedOptions`), decided by the any-of rule of `services.action_rights` before the lock |
| `services.actions` | `ActionService.do`: finds the visible action, binds and class-checks the parameters, hands the plan to `WriteService` |
| `services.managed` | the managed classes: every op whose subject is a metadata record or part is refused (`data.semantic.managed_class`) unless the request's source has the kind `extractor` and the entity's key starts with the source's name; judged on the raw stored state and the pinned model, in the write path and in dry runs, before planning for a reconcile scope |
| `services.file_metadata`, `file_metadata_plan` | `FileMetadataService.extract`: the run over a graph or named files (which extractors are enabled, which results are current, newer or stale), the child process per file and extractor, one reconcile document per (file, extractor) written through `WriteService` as the caller, the cleanup of records of inactive files; the plan part is pure over a snapshot, the model and the registry |
| `services.files` | `FileService`: add a file (stream, hash, dedupe, origin, one create), `path` (the backend's file, else a copy in the injected `FileCache`), `open` (the one streaming read of a stored file, for the server's download and, through `open_stored`, the live view), `check`; the type recorded at add comes from `extractors.media_types` |

A write runs in this order: check the graph state and the pinned ontology
artifact, resolve references, authorize every (action, type) pair, take the
graph lock, re-check state and pin, replay an idempotent retry, check
`expect_seq`/`expect_rev`, normalize, validate, publish the stored files
the request staged and the transaction names, append the audit intent,
commit. Ops apply in order to one working copy, each touched entity gets
one new revision unless its content ends up equal to its current revision
(it is unchanged then), and a change set commits completely or not at all;
one that changes nothing stops after validation, without a transaction.
A write by `graph do` hands the action to the same pipeline as a plan: under the
lock the template is expanded on the locked state and the expansion authorized, then
come the idempotency check (a replay matches the request digest, not the expansion)
and the preconditions, and the write continues as for any change set.
A document with a `reconcile` part runs the same way: its declared needs are authorized
before the lock, then under the lock the idempotency key is looked up (before planning),
`sources.reconcile` plans the scopes on the state the caller sees, and the write continues
on the expanded change set.
Validation reports findings (`data.*`) instead of stopping at the first
problem; error findings refuse the write (`write.invalid`), warnings ride
along with the result. The `connecting` of a domain or range finding
(described in [the `graph` CLI](cli_graph.md)) is computed once per type pair
and only for findings that are kept. Refusals are audited: denials in the installation
stream, invalid and conflicting writes in the graph's stream. A dry run
validates against the current state without the lock, without ids and
without any audit or file change. A JSONL batch commits one document per
transaction, carries local names to later documents and stops at the first
failure.
