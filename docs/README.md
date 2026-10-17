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
(reads, writes, `apply`, `describe`, `skills`), `graph-admin`, the
generated `graph-cli` agent skill and per-graph skills (static, or generated
from a template and the live model) exist.

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
| `storage.files.skill_slot` | reading a graph's published skill (`skill/source.json` and `skill/files/<hash>/`) |
| `storage.files.blobs` | stored files by hash: staging, publishing, reading, listing, blob verification and removal |
| `storage.files.maintenance` | `FileStoreMaintenance`: graph creation, manifest switch, release install, skill publish, `verify`, `verify_blobs`, `remove_blobs`, `recover`, `reindex` (admin only) |

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
- Stored files live in `blobs/sha256/<first 2 hex>/<hash>` of the graph
  directory, read-only and never changed. They are staged in
  `blobs/.tmp-*` before the lock and published by one rename under it;
  each transaction lists the hashes its revisions reference (`blobs`), so
  verification and cleanup need no ontology.
- Maintenance effects are published with one rename each: a new graph
  directory (staged as `graphs/.tmp-<id>`), an ontology release directory,
  the manifest, the graph skill's `source.json` (its files are written once
  under their hash before). A failure before the rename leaves only the
  staging state, which readers ignore.

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
| `catalog.config` | `config.toml`: device id and name, runtime mode, data root, identity providers, lock timeout, durability, audit redaction, output default, file roots, file size limit, read signal |
| `catalog.file_roots` | `FileRoots`: the named roots of this machine; the origin of a file and where an origin is here |
| `catalog.resolve` | config path, state directory and data root, without touching the filesystem |
| `catalog.file_catalog` | `FileCatalog`: graphs from `graphs/*/manifest.toml` (no registry file), name or id resolution, release files, `store_for` |
| `catalog.oplog` | the operational log in the state directory (recovery notes), best effort |
| `catalog.activity` | the read signal for `graph-admin view`: Unix datagrams to the viewer sockets in the state directory, fire and forget |
| `catalog.pointer` | the project pointer file `.graph_cli.toml` and graph selection |
| `catalog.wiring` | the composition root of `graph`: config, roots, pointer, actor, services |
| `catalog.identity` | identity providers and the actor (see above) |

Locations (XDG; macOS uses the same paths):

| What | Where |
| --- | --- |
| Config | `$XDG_CONFIG_HOME/graph_cli/config.toml`, else `~/.config/graph_cli/config.toml` |
| Data root | `graph-admin --data-root` (`graph` has no such flag), then `GRAPH_CLI_ROOT` (warns), then config `[paths] data_root`, then `$XDG_DATA_HOME/graph_cli`, else `~/.local/share/graph_cli` |
| State (operational log, viewer sockets) | `$XDG_STATE_HOME/graph_cli`, else `~/.local/state/graph_cli` |

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
| `graphs` | the graphs this project may use, with the title that says what each is for and the default language of their values (BCP 47, null when unset) |
| `describe commands` | every command as data (`graph_cli.inventory/1`): arguments, options, examples, error codes, exit codes |
| `describe types`, `type T`, `fields T`, `relations`, `lifecycle T` | the selected graph's ontology (`graph_cli.model/1`); the `graph` block names the graph's default language |
| `get REF` | one entity; superseded and retracted entities are shown as recorded, with a warning and the ids that replaced or withdrew them |
| `find` | entities by `--type`, `--where`, `--rel`/`--in-rel` (`rel=<id>,<id>` matches an edge to any listed entity); sorted, paged (`--limit`, `--offset`) with a next-page hint |
| `lookup TEXT` | entities by name: main, alternative and hidden labels, case- and Unicode-insensitive (`--match exact\|prefix`, `--ignore-accents`, `--type`); ranked candidates, paged |
| `neighbors REF` | connected entities up to three hops (`--rel`, `--direction`, `--depth`) |
| `create TYPE`, `update REF`, `link REF REL TARGET`, `unlink REF ...` | single-op writes; `--set attr=value` is converted by the attribute's datatype, `--set-json attr=<json>` is typed |
| `transition REF STATE`, `supersede BY TARGET...`, `retract REF` | lifecycle and provenance; `supersede` and `retract` need `--reason` |
| `apply FILE` | change set documents (one JSON document or JSONL; `-` reads stdin) |
| `file add PATH` | store a file as `files:File` (or `--type` subclass) with its origin; known content is returned, not stored twice (see "Files") |
| `file path REF` | the read-only path of a stored file; `--origin` the path it came from on this machine |
| `file check [REF...]` | compare origins with the stored content: `same`, `changed`, `missing`, `root_unknown`, `no_origin` |
| `skills emit --out DIR` | write skill 1 and, with `--graph`, published graph skills into a directory (one directory per skill, no marker) |
| `skills install --scope user\|project` | install or refresh the agent skills (see "Skills") |
| `skills check --scope user\|project` | compare installed skills with what this program installs now; exit 1 unless all are fresh |

Graph selection: `--graph`, then `GRAPH_CLI_GRAPH`, then the `default` of
the nearest `.graph_cli.toml` up from the working directory. The pointer
file lists the graphs a project may use (`graphs`), optionally a `default`
and a `project` name for the actor; it cannot move the data root. There is
no implicit default graph.

The global options `--json` and `--graph` may stand before or after the
command (`graph --graph <graph> find ...` equals `graph find ... --graph
<graph>`). Given twice with different values is a usage error (a
repeatable `--graph`, as in `skills install`, collects both), and a
command that takes no graph (`graphs`, `whoami`) refuses `--graph` in
either position.

References in arguments are full ids, aliases or 8-32 trailing hex digits of
an id; change set documents allow full ids, aliases and `@local` names only.
Writes take `--dry-run`, `--reason`, `--idempotency-key` and, on
single-entity commands, `--expect REV`. `find` filters are flags without
shell metacharacters: `--where attr=value` or `--where attr:op=value` with
`op` in `eq ne lt le gt ge contains prefix in exists`, all AND-ed. `apply`
reads and checks every document first (broken input writes nothing), then
commits one transaction per document in order and stops at the first
failure; `data.results` reports each processed document and the exit code is
that of the failure. Its idempotency key is `idempotency_key` in each
document; `apply --idempotency-key` sets it for input with a single document
and refuses JSONL with several or a document with a different key. Reads, `--help`, dry runs and usage errors write
nothing, audit files included. `--as-of` is reserved and refused.

Names come from label roles in the ontology: an attribute of type `string`
(or a datatype over it) may carry `role: label` (the main name, single, at
most one per class), `alt_label` (synonyms, abbreviations) or
`hidden_label` (searchable, never shown), as in SKOS. `describe` shows the
role of each field and the JSON Schema export annotates it as
`x-graph_cli-role`. `lookup` matches normalized text (`kernel.text`: NFKC,
casefold, whitespace collapsed; never stored) and ranks exact before prefix
matches, main before alternative before hidden labels, then shorter names;
a hidden-label match does not echo its value. Labels are not identifiers:
references stay ids, aliases and `@local` names. Types without label roles
are `term.no_labels`.

Commands are declared as data in `graph_cli.app_cli.commands`
(`cli_kit.spec.CommandSpec`); the click tree and the inventory are built from
the same declarations. Query planning (`graph_cli.query.plan`) turns `find`
and `neighbors` flags into the query AST, `graph_cli.query.lookup` plans,
matches and ranks `lookup`; `services.read` and `services.describe` answer
them.

## Maintenance CLI

`graph-admin` is run from the repository checkout. Every command prints the
`graph_cli.envelope/1` envelope: text by default, one JSON document with
`--json` (or `GRAPH_CLI_OUTPUT=json`). Exit codes follow the error category
(0 ok, 1 refused or invalid, 2 usage, 3 denied, 4 conflict, 5 unavailable,
6 internal error).

| Command | What it does |
| --- | --- |
| `init` | create the data root and, if missing, `config.toml` with a device id |
| `graph create NAME --ontology DIR` | create a graph pinned to the first release of an authored ontology; `--language TAG` sets the BCP 47 default language of its values (canonical case, fixed after create) |
| `graph list`, `graph show REF` | list graphs with title and language; show manifest, head, installed releases and the published skill |
| `graph skill publish GRAPH DIR` | publish a skill directory or a template as the graph's skill (`--dry-run`, `--reason`) |
| `graph skill render GRAPH DIR --out OUT` | preview: write what `graph skills install` would install into `OUT/graph-<graph>/` and report what publish would refuse (`--force`); changes nothing in the data root |
| `ontology validate DIR` | compile and check an authored ontology |
| `ontology diff DIR --against DIR` / `--graph REF` | classify the changes and check the version bump |
| `ontology release GRAPH DIR` | publish a release into the graph and pin the graph to it |
| `audit verify` | check every chain, tx log, revision file and manifest pin; exit 1 on error findings |
| `audit anchor` | record every graph stream's head in the installation stream |
| `files verify --graph REF` | check the stored files of a graph: missing (and, with `--rehash`, damaged) files are errors, orphans and staging leftovers info |
| `files gc --graph REF` | remove stored files no transaction names and staging files older than one hour (`--dry-run`, `--reason`), audited as `maint.repair` |
| `view --graph REF` | serve a live view of the graph in the browser until Ctrl+C (`--port`, `--no-open`); no JSON mode |

Every change is audited: an intent in the graph's stream, the effect, then
an installation event anchoring the intent. A release must bump the version
at least as much as the changes require; a new major version on a graph
with data is released only if the data has no error findings under the new
version (`check_graph`), otherwise the refusal lists them. Verification reports an
interrupted operation as info and anchors only graphs without errors.
`cli_kit` (envelope, error rendering, hints) is shared with the application
CLI; `graph_cli_admin.maintenance_cli.wiring` is the admin composition root
and `graph_cli_admin.services_admin` holds the procedures.

### Live view

`graph-admin view --graph <graph>` prints an address on stderr and opens
it: the whole graph as dots and lines, with labels, and a list of recent
activity. Entities light up briefly when something touches them: green
when created (also by `graph file add`), blue when changed, yellow when
read. Writes show up by themselves, from any process, once committed.
Reads show up only when `config.toml` has

```toml
[activity]
signal_reads = true
```

Then `graph get`, `find`, `lookup`, `neighbors`, `file path` and
`file check` send the ids they return to every running view, as Unix
datagrams to sockets in the state directory. Nothing is stored, a read
never fails or waits because of it, and without a running view nothing
is sent. The signal carries ids only, no labels, values or query text.

The server listens on `127.0.0.1` only and answers only requests that
carry the random token in the printed address, so keep the address to
yourself. The graph itself carries labels, types, aliases and record
status only. Clicking a node loads that entity on demand: its attributes
(hidden labels left out), edges in both directions with their
qualifiers, and every revision with the agent, principal and session
that wrote it. Clicking an edge shows its relation, ends and qualifiers
and the history of its source entity. For a stored file (`files:File`)
the panel offers a preview of PDFs and images in the page and the
original as a download; reading the bytes needs `read.get` on the file's
type, as for `file path`. Click an activity entry to jump to its entity;
Escape closes the panel. The search box dims everything whose label,
alias or type does not contain the text (Enter opens the first match);
"hide entities that do not match" removes those instead, and one
checkbox per type hides whole types. Attribute values are not searched.
"scale nodes by number of connections" enlarges nodes in proportion to
their edge count, relative to the most connected visible node.
A Unix socket path is limited to about 100 bytes; with a
very long state directory the view starts without reads and says so.

## Files

A graph whose ontology imports the standard module `files` (`imports:
[{module: files, version: "^1.0"}]`) stores files: scans, documents,
saved web pages, any media type. graph_cli stores, addresses and
references them; it never reads what is in them. An agent reads a stored
file itself and writes what it finds through the normal write commands.

- `files:File` holds `title` (the main label), `content` (type `blob`:
  `sha256:<hex>` of the bytes, unique among active files, immutable),
  `media_type`, `file_name`, `origin` (type `local_path`), `source_url` and
  `retrieved_at`. A graph module may subclass it (`is_a: files:File`) to
  give files domain meaning.
- `files:from_file` points from any entity to a file, with the qualifiers
  `page` and `last_page` (from 1) and `locator` (a section, region or task
  number).
- A file's content never changes. A new version is a new `file add`
  followed by `supersede`. Nothing that a revision ever referenced is
  removed; `graph-admin files gc` removes only files no transaction names.
- Web sources need no stored copy: an `iri` attribute plus a `date` for
  the retrieval. A saved copy is a stored file with `source_url` and
  `retrieved_at`. graph_cli fetches nothing from the network.

Origins are machine-neutral: `{"root": <name>, "path": <relative path>}`
against named roots that each machine maps in `config.toml`. The longest
root that contains the file wins; without one the file gets no origin
(warning `file.no_root`). An absolute path is never recorded.

```toml
[roots]
notes = "~/path/to/synced/notes"   # absolute or ~/ (HOME)
[files]
max_bytes = 104857600              # per file, 1 B to 1 GiB; 100 MiB by default
```

Roots are added by hand. A root that is not configured on a machine makes
`file path --origin` fail with `file.root_unknown`; `file check` reports
`root_unknown`. Paths are compared as given after resolving symlinks:
there is no case folding on case-insensitive file systems and no Unicode
normalization, so a name stored in NFD on one machine may not resolve on
another. The media type comes from a built-in table of magic bytes and
extensions (`--media-type` overrides it), so it is the same on every
machine.

Audit events name stored files by hash and size only: never a file name,
an origin path or content. Reading a file is not audited (reads never
are). File bytes, names and origins are instance data like any attribute
value: they live in the graph's `blobs/` and transaction log.

Backups of the data root contain the stored files, so they may contain
personal data (scans of documents). After a restore, run
`graph-admin audit verify` (a missing stored file is an error there) and
`graph-admin files verify --graph <graph> --rehash` (damaged bytes). Keep
the data root on a local file system, as for the rest of the store.

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
<state> <file> <json> <seq> <dir>`, paths, ids and non-ASCII text. The
skill names no graph type.

### Graph skills

A graph skill (`graph-<graph>`, `_` becomes `-`) says what one graph holds
and how to use it. The maintainer writes a skill directory and publishes it
into the graph's runtime area. Its kind follows the content:

- `generated`: `SKILL.md` is a template. A line consisting only of
  `<!-- graph_cli:block <name> -->` is replaced by a block generated from
  the graph's live model and manifest; the frontmatter, all other prose and
  extra files (`references/*.md`) are curated and kept. `graph` renders the
  template at every `skills emit|install|check` with the model of the
  graph's current pin, so after an ontology release an installed copy is
  `stale` and `graph skills install` refreshes it; no republish is needed.
- `static`: no marker. The files are the skill, installed as they are; a
  skill published against another ontology release than the pin gives the
  warning `skills.source_behind_ontology`.

| Block | Renders |
| --- | --- |
| `start` | how to tell the graph is available (`graph graphs --json`, its name and title, `--graph <graph>`); read the `graph-cli` skill first |
| `types` | one row per entity type (types with an alias prefix; without any, every concrete type): CURIE and alias example, summary and supertypes, fields (enum values inline, `required`, `many`, `unique`, write-once, defaults), states and transitions |
| `relations` | the relations from or to those types: from -> to, cardinality, characteristics, qualifiers, inverse label |
| `constraints` | transition guards, write-once fields, rules with severity |
| `names` | the alias format (`ITEM-0001`) and the id forms that work; which fields `graph lookup` matches per type (label roles); use a candidate only on exactly one exact match, else ask |
| `model` | a pointer to `references/model.md` and `graph describe type` |

Each block may be used once; an unknown block name is refused. Every
generated skill also gets `references/model.md` (every type of the model,
imported ones included, with all fields, lifecycles, relations and rules),
so a template may not contain a file of that name. A stamp after the
frontmatter names the graph, the ontology release and the template hash;
the program version and the time are not in it, so a new program with the
same render stays `fresh`. Blocks use only the model and the manifest,
never graph data (no counts, no records). Curated examples may name real
aliases.

Publishing checks the directory (`graph_cli.skills.tree`): the name equals
the directory and the frontmatter `name`, regular UTF-8 files only, no
dotfiles, `SKILL.md` at most 500 lines and the tree at most 256 KB, and no
maintenance vocabulary (`graph-admin`, `maint.*`, `admin.*`, the data root
option and variable) or local paths. A template is also rendered against
the current pin exactly as `graph` will render it, and its curated text is
checked against the model and the app inventory (`graph_cli.skills.names`):
every CURIE with a model prefix names a type, relation, enum, lifecycle or
datatype; every `graph <command>` in code names a command and only its
flags; aliases with a graph prefix have the live format (`ITEM-1` is
refused), and in code an alias with a prefix the graph does not use is
reported. Any problem refuses (`admin.skill_invalid`, `details.problems`
with `rule` `template`, `term`, `command`, `alias` next to the tree rules).
Publishing is audited as `maint.graph_configure` and records the ontology
version it was checked against; the same files against the same version
publish nothing. `graph` reads published skills with `read.describe`, so a
project only gets the skills of the graphs its pointer allows. When the
model or the program changes later, `graph` checks the curated text again
at every render and warns with `skills.source_unknown_names` (the skill is
still installed, its model part is right); a template it cannot render (a
block from a newer publisher) is `skills.source_invalid`.

The authoring loop: edit the template, preview, read, publish, install.

```sh
uv run graph-admin graph skill render <graph> <dir> --out <preview>
uv run graph-admin graph skill publish <graph> <dir> --dry-run
uv run graph-admin graph skill publish <graph> <dir> --reason "<why>"
graph skills install --scope project --dir <project>
```

`render` writes `<preview>/graph-<graph>/` exactly as `graph skills
install` would (without the marker), with this checkout's program, and
reports what publish would refuse (exit 1, the preview is written anyway);
it changes nothing in the data root. A non-empty preview directory is
replaced only with `--force`.

### Installing skills

`graph skills install` copies skill 1 and graph skills into an agent skill
directory. There is no default target:

- `--scope project [--dir DIR]`: `DIR/.claude/skills/` (`DIR` defaults to
  the working directory; `DIR/.claude/` must exist).
- `--scope user`: the user skill directory, `$CLAUDE_CONFIG_DIR/skills/`
  when that variable is set, else `~/.claude/skills/` (the configuration
  directory must exist).

Skill 1 is always installed, graph skills named with `--graph`, and every
graph skill installed there before is refreshed; one whose graph is gone,
no longer visible or unpublished is reported as `unavailable` and left
alone. Each installed directory carries the marker `.graph_cli_skill.json`
(program, source, file hashes); a directory without it, or with files that
differ from it, is replaced only with `--force`. Unrelated skill
directories are never touched. Each skill is swapped in with renames from a
staging directory next to `skills/`, so an agent never sees a half copy.
`--dry-run` shows the plan. `graph skills check` reports each skill as
`fresh`, `stale`, `modified`, `unexpected`, `foreign`, `missing` or
`unavailable` and names the program that checked (`data.program.executable`).
A generated graph skill is `stale` after a new program, a republish or an
ontology release changed its render; the marker's `source.model` names the
ontology release it was rendered with.

Installing `graph` itself is not a `graph` command. A recipe that keeps the
tool's dependencies on the workspace lock (`uv tool install` ignores
`uv.lock`):

```sh
uv export --frozen --package graph-cli --no-hashes --no-emit-workspace \
  --no-dev --no-header -o <file>
uv tool install --reinstall --constraints <file> ./packages/graph_cli
graph skills install --scope project --dir <dir> --graph <graph>
graph skills check --scope project --dir <dir>
```

Run `install` and `check` with the installed `graph`, not with `uv run
graph` from the checkout: `check` compares against the program that runs
it.

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
| skills | `tests/skills/` | the committed `graph-cli` skill: freshness, docs-vs-code, leaks (also of every `skills emit` output, static and generated graph skills), structure |
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
service), role mappings for role-based contract tests
(`tests/support/roles.py`) and graph skills for the `inventory` test graph
(`tests/fixtures/skills/`: a static one, a template with curated text, and
`blocks/` with every block once, rendered for both toy ontologies into
`tests/goldens/skills/`).
Core tests never name a fixture type; a leak test keeps fixture vocabulary out
of the packages. Deterministic test doubles (`FixedClock`, `StepClock`,
`SeededIdProvider`) live in `tests/support/`. Golden files live in
`tests/goldens/` and next to the fixtures they describe.
