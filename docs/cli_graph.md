# `graph`: the application CLI

`graph` is the agent and user surface; it reads and writes instance data of
existing graphs and never creates a data root or a graph (that is
[`graph-admin`](cli_graph_admin.md)).

The complete per-command reference (arguments, options, limits, examples)
and every error code are generated from the command declarations:
[`skills/graph-cli/references/commands.md`](../skills/graph-cli/references/commands.md)
and [`skills/graph-cli/references/errors.md`](../skills/graph-cli/references/errors.md).
The same data comes as JSON from `graph describe commands --json`.

## Output and exit codes

Both CLIs print the `graph_cli.envelope/1` envelope: text by default, one
JSON document with `--json` (or `GRAPH_CLI_OUTPUT=json`). Exit codes follow
the error category (0 ok, 1 refused or invalid, 2 usage, 3 denied, 4
conflict, 5 unavailable, 6 internal error).

## Commands

| Command | What it does |
| --- | --- |
| `whoami` | the acting identity (principal, agent, session, project) and what the policy allows: the project scope and, per graph, the level, the actions a deny rule withholds below it, read-only types and write obligations |
| `graphs` | the graphs this project may see and use, with the title that says what each is for and the default language of their values (BCP 47, null when unset) |
| `describe commands` | every command as data (`graph_cli.inventory/1`): arguments, options, examples, error codes, exit codes |
| `describe types`, `type T`, `fields T`, `relations`, `lifecycle T` | the selected graph's ontology (`graph_cli.model/1`); the `graph` block names the graph's default language |
| `get REF` | one entity; superseded and retracted entities are shown as recorded, with a warning and the ids that replaced or withdrew them; `--rev N` shows an earlier revision |
| `history REF` | one entity's revisions, newest first: time, command, reason, whether your own session wrote it, and the names (not values) of the fields and relations that changed; paged |
| `find` | entities by `--type`, `--where`, `--rel`/`--in-rel` (`rel=<id>,<id>` matches an edge to any listed entity); sorted, paged (`--limit`, `--offset`) with a next-page hint |
| `lookup TEXT` | entities by name: main, alternative and hidden labels, case- and Unicode-insensitive (`--match exact\|prefix`, `--ignore-accents`, `--type`); ranked candidates, paged |
| `neighbors REF` | connected entities up to three hops (`--rel`, `--direction`, `--depth`) |
| `create TYPE`, `update REF`, `link REF REL TARGET`, `unlink REF ...` | single-op writes; `--set attr=value` is converted by the attribute's datatype, `--set-json attr=<json>` is typed |
| `transition REF STATE`, `supersede BY TARGET...`, `retract REF` | lifecycle and provenance; `supersede` and `retract` need `--reason` |
| `apply FILE` | change set documents (one JSON document or JSONL; `-` reads stdin) |
| `file add PATH` | store a file as `files:File` (or `--type` subclass) with its origin; known content is returned, not stored twice (see [Files](files.md)) |
| `file path REF` | the read-only path of a stored file; `--origin` the path it came from on this machine |
| `file check [REF...]` | compare origins with the stored content: `same`, `changed`, `missing`, `root_unknown`, `no_origin` |
| `skills emit --out DIR` | write skill 1 and, with `--graph`, published graph skills into a directory (one directory per skill, no marker) |
| `skills install --scope user\|project` | install or refresh the agent skills (see [Skills](skills.md#installing-skills)) |
| `skills check --scope user\|project` | compare installed skills with what this program installs now; exit 1 unless all are fresh |

## Graph selection

`--graph`, then `GRAPH_CLI_GRAPH`, then the `default` of the nearest
`.graph_cli.toml` up from the working directory. There is no implicit
default graph. The pointer file and what it may contain are described in
[Configuration](configuration.md#project-pointer); the installation policy
decides what a project may see, the pointer only narrows that (see
[Access control](access_control.md)).

The global options `--json` and `--graph` may stand before or after the
command (`graph --graph <graph> find ...` equals `graph find ... --graph
<graph>`). Given twice with different values is a usage error (a
repeatable `--graph`, as in `skills install`, collects both), and a
command that takes no graph (`graphs`, `whoami`) refuses `--graph` in
either position.

## References, filters and writes

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

Every write is a change set that commits completely or not at all;
validation findings, refusals and dry runs are described in
[Architecture: write pipeline](architecture.md#write-pipeline).

## Names and lookup

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
