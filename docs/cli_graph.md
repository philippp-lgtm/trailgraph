# `graph`: the application CLI

`graph` is the agent and user surface; it reads and writes instance data of
existing graphs and never creates a data root or a graph (that is
[`graph-admin`](cli_graph_admin.md)).

The complete per-command reference (arguments, options, limits, examples)
and every error code are generated from the command declarations:
[`skills/graph-cli/references/commands.md`](../skills/graph-cli/references/commands.md)
and [`skills/graph-cli/references/errors.md`](../skills/graph-cli/references/errors.md).
The change set documents of `apply` are described in
[`skills/graph-cli/references/changesets.md`](../skills/graph-cli/references/changesets.md).
The same data comes as JSON from `graph describe commands --json`; with
`--output-schemas` every command also carries the JSON Schema (2020-12) of its
answer's `data` and the inventory the shared definitions they point to
(`$defs`): validate `data` with `{...output, "$defs": $defs}`. The schemas
are open: new keys may appear within `graph_cli.envelope/1`, so ignore keys
you do not know. A parameter that takes `<name>=<value>` pairs names the
shape of its structured form over the server API in `pairs` (`map`,
`multimap`) and `pair_values` (`text`, `json`); see [Server](server.md#api).

With `[runtime] mode = "remote"` in `config.toml`, `graph` keeps no data on
the machine and sends every command to a `graph-server`; answers, text and
exit codes stay the same (see [Server: remote mode](server.md#remote-mode-of-graph)).

## Output and exit codes

Both CLIs print the `graph_cli.envelope/1` envelope: text by default, one
JSON document with `--json` (or `GRAPH_CLI_OUTPUT=json`). The exit code
follows the error category:
[error codes](../skills/graph-cli/references/errors.md).

### The short envelope

`graph --json` prints the envelope short, since every answer goes into an
agent's context: `schema`, `ok` and `command` always, `graph` with its
`name` and `seq` (and `state` when the graph is not active), and `data`,
`page`, `hints`, `warnings` and `error` only when they are not null or
empty; read a missing key as null or `[]`. The answer itself keeps every
key: the server sends it whole (remote mode prints it short on the
client), the access log records it whole, and `graph-admin --json` prints
it whole. A spilled answer's file holds what stdout would have shown.

### Large answers go to a file

Agent harnesses cut long tool output or put all of it into the agent's
context. So a `graph` JSON envelope larger than `[output] spill_bytes`
(see [Configuration](configuration.md#configtoml)) is written whole,
pretty-printed for line-wise reading, to a new file, and
stdout gets the same envelope with `data: null` (left out of the short
envelope), the warning
`output.spilled {path, bytes, limit}` and, on success, a first hint to ask
for less or to read only the needed part of the file with the agent's file
read or search tool. The hint names no command: a shell program such as
`jq` is what restricted agent harnesses refuse. `ok`,
`error`, `page`, the exit code and the other hints and warnings stay as
they were, so paging still reads from stdout.

The file is `<dir>/<graph>/<command>_<UTC time>_<8 hex>.json` (without a
graph directly in `<dir>`), readable only by the user. `<dir>` is `[output] spill_dir`,
else `/tmp/graph_cli_<uid>`: a directory per user, because a fixed name in
the shared `/tmp` could be created by another user first. The directory
must be the user's own and closed to group and others (`graph` creates it
that way); otherwise, or when the file cannot be written, the answer
is printed whole with the warning `output.spill_failed {bytes, limit,
reason}`. Each spill removes files older than 24 hours from the same
directory. Text output never spills. `GRAPH_CLI_SPILL_BYTES=0` (or
`spill_bytes = 0`) keeps every answer on stdout, for scripts. `graph call`
never spills: its answer is the data a program asked for (see [Tools for
agent harnesses](#tools-for-agent-harnesses)).

## Commands

| Command | What it does |
| --- | --- |
| `start [--graph G] [--check] [--dir D]` | the first call of a session, in a few lines (`--json` is `graph_cli.start/1`): whether this program and the installed skills are current (stale copies are refreshed as `self update` does, the program is never replaced; `--check` only reports), who acts, and per graph you reach its title, your level (and what a deny rule withholds below it), the number of modules, types, functions, actions and views you may use, the number you see but may not use (`actions 5 (+2 not allowed)`) and the commands that search them. See [Starting a session](#starting-a-session) |
| `whoami` | the acting identity (principal, agent, session, project and, over the server, the roles of the token) and what the policy allows: the project scope and, per graph, the level, the actions a deny rule withholds below it, read-only types, write obligations and, for a caller whose level is below `write`, `writes`: what a write allow list lets it write per type and verb (see [Write allow lists](access_control.md#write-allow-lists)) and, for a caller limited to some types, `reads`: the types it sees (see [Read limits](access_control.md#read-limits)) and, for a graph whose ontology marks fields that the caller does not all hold, `markings`: the markings it holds (`[]` for none; see [Markings](access_control.md#markings)); `writes` names no field the caller cannot see |
| `graphs` | the graphs this project may see and use, with the title that says what each is for and the default language of their values (BCP 47, null when unset) |
| `describe commands` | every command as data (`graph_cli.inventory/1`): arguments, options, examples, error codes, exit codes |
| `describe types`, `describe type T`, `describe fields T`, `describe relations`, `describe lifecycle T` | the selected graph's ontology (`graph_cli.model/1`); the `graph` block names the graph's default language; every term with a summary carries `deprecated`; `describe types` and `describe relations` take `--module M` (id or prefix) and then list only the terms of that module: its types, the relations it declares or has a class at one end of; the answer names the module in `module` |
| `describe modules` | the subject areas of the ontology, one item per module: id, prefix, version, summary, `builtin`, the number of types and relations, and `links` (the modules it connects to, with the number of relations in each direction); the first call on a graph with several modules. When the graph has functions, actions or views you see, each module also has a `functions`, `actions` and `views` count of the active ones you may use. What you see but may not use is counted per kind in `not_allowed` (only on a module that has some; see [Entries you may not use](#entries-you-may-not-use)) |
| `describe module M` | one module (id or prefix): its types, the relations that stay inside it, `links_out` and `links_in` (relations to and from other modules, as `from -rel-> to`, with the module that declares the relation in `module` and the modules at its other ends in `other_modules`) and the rules on its types; an unknown module is `term.unknown` with `did_you_mean`; `describe type` names the owning module in `module`. The text form is made for agents to read: it leaves out cardinalities and the summaries of links, which `describe type` gives; the skills point agents to it. When the graph has functions, actions or views you see, a `functions`, `actions` and `views` section lists the module's active ones you may use (`name`, `summary`, `signature`; text: `Functions:`, `Actions:`, `Views:`), and `not_allowed` counts the ones you may not; `--include-not-allowed` lists those too, after the others and marked |
| `describe search TEXT` | find terms of the ontology when you only know a word: types, relations, fields (under the class that declares them), enum values and lifecycle states, by name, CURIE, summary and description, and the graph's functions, actions and views you see (by name, summary and, last, what they are for; one you may not use is found too, marked `allowed: false` with `missing`, after the others of its rank, see [Entries you may not use](#entries-you-may-not-use)); reads the model, never graph data (that is `search`). Ignores case, accents and punctuation; best first: whole name, name prefix, word of the name, name contains, summary, description. Each hit has `kind` (`type`, `relation`, `attribute`, `enum_value`, `state`, `function`, `action`, `view`; a function's, action's or view's `curie` is its name, see [Functions](#functions) and [Actions](#actions); views are the ones you can call, see `describe views`), `curie` (a field, value or state is its owner's CURIE, a dot and its name), `of` (the owner), `module`, `summary`, `deprecated` and `match` (`field`, `how`). `--kind` (repeatable) and `--module M` narrow it; paged like `search` (`--limit`, `--offset`, next-page hint) |
| `describe functions [--module M] [--include-deprecated] [--include-not-allowed]` | the named functions of the graph you may run (`graph_cli.model/1`), one item per function: `name`, `module`, `summary`, `signature` (`unit: ref org:OrgUnit, include_vacant?: boolean`; `?` marks an optional parameter) and `status`; deprecated functions only with `--include-deprecated`; `not_allowed` counts the ones you see but may not run, `--include-not-allowed` lists them too. See [Functions](#functions) |
| `describe actions [--module M] [--include-deprecated] [--include-not-allowed]` | the actions of the graph that you may run (`graph_cli.model/1`), one item per action: `name`, `module`, `summary`, `signature` and `status`; deprecated actions only with `--include-deprecated`; `not_allowed` counts the actions whose write rights you lack (`--include-not-allowed` lists them too). See [Actions](#actions) |
| `describe action NAME` | one action completely: summary, `use_when`, `reason`, parameters, preconditions, the compiled operations, the types and relations it touches, the write rights it needs (`needs`), examples and its hash; an unknown or hidden name is `action.not_found` with `did_you_mean`; an action you may not run is shown marked `allowed: false` with `missing`, without its operations, conditions, examples, needs and hash. See [Actions](#actions) |
| `describe function NAME` | one function completely: summary, `use_when`, parameters, the result columns with their types, examples, the types and relations its query touches, the normalized query text (copy it as the start of an ad-hoc query) and its hash; an unknown name is `function.not_found` with `did_you_mean`; a function you may not run is shown marked `allowed: false` with `missing`, without its query, examples, touched terms and hash (see [Entries you may not use](#entries-you-may-not-use)) |
| `describe views [--module M] [--include-deprecated] [--include-not-allowed]` | the published views of the graph you can call (`graph_cli.model/1`), one item per view: `name`, `module`, `summary`, `takes` (the class an entity view shows; left out for a set view), `default` (only when true), `signature` and `status`; deprecated views only with `--include-deprecated`. A view you cannot see in full (it names a type, attribute, relation or function that is hidden from you) does not exist for you; one you see in full but that needs `read.get`, `read.find` or `read.function` that you lack is counted in `not_allowed` (`--include-not-allowed` lists it) |
| `describe view NAME` | one view completely: summary, `use_when`, `takes`, `default`, parameters, every section with the types, relations, fields and function it reads, examples, `touches` (types, relations, attributes, functions), `hash` and `call`, the command that calls it; `replaced_by` names a view only when you see it; an unknown or hidden name is `view.not_found` with `did_you_mean` over the names you see and a hint to search the views by words; a view you may not use is shown marked `allowed: false` with `missing`, without its sections, examples, touched terms, hash and call command |
| `describe tools [--tool NAME] [--kind K] [--module M] [--featured] [--exclude-deprecated]` | the tool manifest of the graph for an agent harness (`graph_cli.model/1`): one tool per function, action and view you may use, plus a `lookup` tool for the references they take when you may find entities, each with a JSON Schema for its input and its answer, and the error codes it may return with what to do about each; made for programs, not for agents that work in a shell. See [Tools for agent harnesses](#tools-for-agent-harnesses) |
| `describe schema` | the ontology as `graph query` sees it: labels with their properties, one relationship pattern per relation (`(:A)-[:rel]->(:B)`), names written as a query needs them (see [Cypher queries](#cypher-queries)); `--compact` leaves out the properties; `--module M` limits it to the module's relations and labels plus the labels at the other end of its links |
| `get REF` | one entity; superseded and retracted entities are shown as recorded, with a warning and the ids that replaced or withdrew them; `--rev N` shows an earlier revision; `--fields` keeps only named parts and `--full` adds the system fields (see [Fewer fields](#fewer-fields)); `--affordances` adds what applies next (see [What an entity can do next](#what-an-entity-can-do-next)) |
| `view [REF]` | what is known about one entity in one answer, at most 6 KiB: key fields, relations with counts and the first neighbours, files with pages, what applies next; what is cut is counted and named in a hint; `--as-of` reads an earlier state (see [An entity at a glance](#an-entity-at-a-glance)). A graph can publish views: `--name NAME` picks one (`--name card` is always the generated card), an entity view takes the REF of an entity of its class, and without `--name` the default view of the entity's class answers instead of the card; a set view takes no REF and lists entities and function rows in pages of `--page-bytes` bytes, `--cursor` continues a page (`view.cursor_stale` once the graph changed; `--as-of tx:<seq>` pages the state you started with); `--param name=value` gives a view parameter. A set view needs `read.find` as well as `read.get`, one that runs functions `read.function`; a view that names what the caller may not see is `view.not_found`, and one that needs a right the caller lacks is `access.denied`, recorded. Not `graph-admin view`, the live view |
| `history REF` | one entity's revisions, newest first: time, command (and, for a write by `do`, the `action` with its `name` and `hash`), reason, whether your own session wrote it, and the names (not values) of the fields and relations that changed; paged |
| `changes --since POSITION` | what changed in the whole graph after a position (`tx:<seq>`, `tx:<seq>/<entity id>` or an RFC 3339 time with offset), oldest first: per tx its time, command, reason and the entities it changed as ids, types, revisions and the names (not values) that changed; continue with the `next` of the answer while `more`; paged by entity items (`--limit`) |
| `find` | entities by `--type`, `--where`, `--rel`/`--in-rel` (`rel=<id>,<id>` matches an edge to any listed entity); sorted, paged (`--limit`, `--offset`) with a next-page hint; `--fields` and `--full` as for `get` |
| `lookup TEXT...` | entities by name: main, alternative and hidden labels, case- and Unicode-insensitive (`--match exact\|prefix`, `--ignore-accents`, `--type`, `--for`, `--for-source`); several names in one call; ranked candidates, paged per name (see [Names and lookup](#names-and-lookup)) |
| `search TEXT` | entities whose names or text fields match: whole value, inside a word, every word, or a close spelling (`--type`, `--field`); ranked suggestions, paged (see [Search](#search)) |
| `neighbors REF` | connected entities (`--rel`, `--direction`, `--depth`); `--fields` and `--full` as for `get` |
| `query TEXT` | a read-only Cypher query (`-` or `--file` reads it from stdin or a file, `--param name=value` binds `$name`): patterns over several hops, OR and NOT across relations, counts and grouping; rows paged like `find`, `--explain` shows the resolved query (see [Cypher queries](#cypher-queries)) |
| `run NAME` | run a named function of the graph (`--param name=value`, once per value for a list parameter; `--limit`, `--offset`): the parameters are read by the types the function declares, the answer is `data.function` (`name`, `hash`), `data.columns` and `data.items` with a `page`, like `query`. See [Functions](#functions) |
| `create TYPE`, `update REF`, `link REF REL TARGET`, `unlink REF ...` | single-op writes; `--set attr=value` is converted by the attribute's datatype, `--set-json attr=<json>` is typed; `update --add`/`--remove` change a list attribute (see [References, filters and writes](#references-filters-and-writes)) |
| `transition REF STATE`, `supersede BY TARGET...`, `retract REF` | lifecycle and provenance; `supersede` and `retract` need `--reason`; over a relation with `on_target_inactive: refuse` they are refused while active entities still point at the target (`data.referential.target_referenced` lists them; relink or retract them in the same change set) |
| `do NAME` | run a named action of the graph: a reviewed write the maintainer published (`--param name=value`, once per value for a list parameter; `--dry-run`, `--reason`, `--idempotency-key`, `--graph`). The parameters are read by the types the action declares, a reference takes an id, alias or trailing hex digits, never a name. The service checks the action's preconditions and expands it in the state it finds under the graph lock, then writes one transaction with one audit intent that names the action; `--dry-run` shows every entity it would write. The answer is the write result plus `data.action` (`name`, `hash`). A retry with the same `--idempotency-key` and parameters returns the recorded result; the caller needs the write rights of every op it expands to, and an action it sees but may not run is `access.denied` (recorded, naming the right); one that names what the caller may not see is `action.not_found`. See [Actions](#actions) |
| `call TOOL` | run one tool of the [tool manifest](#tools-for-agent-harnesses) with typed JSON input, for agent harnesses: each `--input NAME=<json>` is one member of the object `input_schema` describes (`_limit`, `_reason` and the other controls start with an underscore); it runs what `run`, `do`, `view` or `lookup` runs and answers the same, with the page of a function or a lookup in `data`; `--expect-hash` refuses a tool that is no longer the one the manifest described (`tool.hash_mismatch`); `--idempotency-key` is taken by an action that is not a dry run only; a tool you may not see is `tool.not_found`. See [Tools for agent harnesses](#tools-for-agent-harnesses) |
| `apply FILE` | change set documents (one JSON document or JSONL; `-` reads stdin); `--summary` leaves out the entities of each result and keeps `entity_count` and the findings |
| `file add PATH` | store a file as `files:File` (or `--type` subclass) with its origin; known content is returned, not stored twice; then extract its metadata records (size, type, page count, ...) and answer them in `metadata`, `--no-extract` skips that, and a failure to extract is a warning, never a failed add (see [Files](files.md), [Metadata](files.md#metadata)) |
| `file path REF` | the read-only path of a stored file; `--origin` the path it came from on this machine |
| `file check [REF...]` | compare origins with the stored content: `same`, `changed`, `missing`, `root_unknown`, `no_origin` |
| `skills emit --out DIR` | write skill 1 and, with `--graph`, published graph skills into a directory (one directory per skill, no marker) |
| `skills install --scope user\|project` | install or refresh the agent skills (see [Skills](skills.md#installing-skills)) |
| `skills check --scope user\|project` | compare installed skills with what this program installs now; `skills.check_failed` (exit 1) unless all are fresh |
| `self update` | in the remote mode install the server's build of `graph` when it differs, then refresh the agent skills of the project (`--dir`) and the user, adding the skills of the project's graphs; `--check` only reports, `--force` replaces skills edited by hand (see [Self update](server.md#self-update)) |
| `connect ENDPOINT --code CODE` | set up this machine and a folder (`--dir`) for a server from a one-time enrollment code: the project's token, the remote mode in `config.toml`, a pointer, the program and the project's graph skills; `--label` labels the token, `--force` replaces another endpoint or a stored token (see [Enrollment](server.md#enrollment)) |

## Starting a session

`graph start` is the one call an agent makes first. It is a meta command: the skill check never
refuses or warns on it (it is how a stale copy is found), it writes nothing but skill files and it
never replaces the program.

- **Program and skills.** For every scope that has a skill of this program (the project's, found
  from `--dir` or the working directory, and the user's) it does what `graph self update` does for
  the skills: refreshes stale copies and, in the project scope only, installs the skills of the
  pointer's graphs that are not installed yet (the user scope is refreshed with what it has: the
  project an agent happens to run in decides nothing about what is installed for the user). Each copy is `fresh`, `refreshed` (if you loaded it earlier in the session, load it
  again), `modified` (edited by hand, left alone), `foreign` (not installed by this program, left
  alone) or `unavailable`; with `--check` nothing is written and the copies that would change are
  `stale` or `missing`. In the remote mode the program's build is compared with the server's and a
  different one is named with the hint `graph self update`, and the skills are then only reported
  (`stale`), not written: `self update` replaces the program and renders them again; a checkout has no build to compare
  (`unchecked`). Without an installed skill there is nothing to refresh.
- **Graphs.** One item per graph the project may use (as `graphs`): `name`, `title`, `level` (what
  you can do: `write` or `read` when any write or read action is allowed, `none` when none is, so
  one denied read action does not read as no access), `withheld` (the actions a deny rule takes away
  below it, as `whoami` lists them; the text shows `read; no read.function`, with `+N` for more),
  `skill` (the skill to load, `graph-<name>`, null when the graph has none), `counts`
  (`modules`, `types` and the active `functions`, `actions` and `views` you may use, as
  [`describe modules`](#commands) counts them) and, only when there are some, `not_allowed`: how many
  of each kind you see but may not use ([Entries you may not use](#entries-you-may-not-use)). A
  function, action or view you cannot see is in no number. Nothing is recorded. A graph whose model
  and catalogs are not open to you (no `read.describe`) has `counts: null`. `hidden_count` is the
  number of graphs the pointer file leaves out.
- **`--graph G`** limits the answer to that graph and adds `modules`: each module's id, summary, the
  number of types and the counts per kind, as `describe modules`.
- **In the remote mode** the graphs and counts come from the server and the program and skills are
  judged and refreshed here; the text is rendered from both.

Counts and search hits are what your token may use. The text ends with the search commands to copy
(`describe search TEXT [--kind ...] [--module M] [--limit N]`, `describe module M`) and the rule that
a hit marked not allowed is to be reported to the user, not worked around.

## Graph selection

`--graph`, then `GRAPH_CLI_GRAPH`, then the `default` of the nearest
`.graph_cli.toml` up from the working directory. There is no implicit
default graph. The pointer file and what it may contain are described in
[Configuration](configuration.md#project-pointer); the installation policy
decides what a project may see, the pointer only narrows that (see
[Access control](access_control.md)).

The global options `--json` and `--graph` may stand before or after the
command (`graph --graph <graph> find ...` equals `graph find ... --graph
<graph>`). Given in both positions with different values is a usage error
(a repeatable `--graph`, as in `skills install`, collects both), and a
command that takes no graph (`graphs`, `whoami`) refuses `--graph` in
either position. A non-repeatable option given twice on the same side of
the command keeps the last value.

## References, filters and writes

References in arguments are full ids, aliases or 8-32 trailing hex digits of
an id; change set documents allow full ids, aliases and `@local` names only. A
name given where a reference goes is `usage.invalid_argument` with
`details.expected` `entity_ref` (and `argument`, `actual`, and `for`,
`for_source` or `class` when the slot is known): nothing is resolved, and the
hint is `graph lookup` for the text, with `--for` or `--for-source` the
relation it fills (a link or `--rel` target, a `--in-rel` source) or `--type`.
Types and relations in read arguments (`find`, `lookup`, `search`,
`neighbors`, `describe`) are CURIEs or, as in `graph query`, the local name
when exactly one CURIE has it (`Topic` for `ex:Topic`); an ambiguous local
name is `query.ambiguous_term` with the candidates. Writes and change set
documents take the CURIE: what is recorded stays explicit.
Writes take `--dry-run`, `--reason`, `--idempotency-key` and, on
single-entity commands, `--expect REV`. A reason is written in English,
whatever the graph language: like a commit message it records how the
change was made, not the graph's subject. `find` filters are flags without
shell metacharacters: `--where attr=value` or `--where attr:op=value` with
`op` in `eq ne lt le gt ge contains prefix in exists lang`, all AND-ed. `apply`
reads and checks every document first (broken input writes nothing), then
commits one transaction per document in order and stops at the first
failure; `data.results` reports each processed document and the exit code is
that of the failure. A document may also hand over the desired state of a
source ([Reconciling a source](#reconciling-a-source)). Its idempotency key is `idempotency_key` in each
document; `apply --idempotency-key` sets it for input with a single document
and refuses JSONL with several or a document with a different key. Reads, `--help`, dry runs and usage errors write
nothing, audit files included.

`--set` on a list attribute (`many: true`) replaces the whole list; its text
is one value, so `--set attr=value` leaves a list of one. A text that reads
as a JSON array (`--set attr=["a","b"]`, also in `--add`, `--remove` and the
`_text` keys of change sets) is refused with
`data.structural.cardinality_many`, since it would be stored as one value:
give the array with `--set-json attr=["a","b"]` or each value with its own
`--add`.
`update --add attr=value` and `--remove attr=value` (and `--add-json`,
`--remove-json`; repeatable, one value per flag) change it in place, in
change sets the update keys `add`, `add_text`, `remove` and `remove_text`
(`{attr: [value, ...]}`): `add` appends each value not yet present (equal
as canonical JSON, after conversion by the datatype) and keeps the stored
order, `remove` drops every equal value, and a list left empty is unset.
They apply to the entity as the write finds it, so they need neither a
read before nor `--expect`. On an attribute that holds one value the write
is refused with `data.structural.cardinality_many`. One op names an
attribute in only one of `set`, `set_text`, `unset`, `add`, `add_text`,
`remove`, `remove_text`.

## Reconciling a source

A pipeline that mirrors an external source (a collector, a scheduled job, an
extractor) hands `apply` the **desired state** of declared scopes instead of
ops. graph_cli computes what to create, update, leave and retire **by key**,
on the state under the graph lock, and writes one transaction that names the
source run in the audit. It holds no connector and no credential of the
source and reads no database; the pipeline keeps its matching rules. Agents
write ops; the keys of a reconcile document are listed in the change set
reference of the generated skill (`references/changesets.md`).

```json
{
  "source": {"name": "collector", "version": "3", "run": "2026-11-17T10:00:00Z"},
  "idempotency_key": "collector-2026-11-17T10-00-00Z",
  "ops": [{"op": "create", "type": "ex:Thing", "local": "thing", "attrs": {"name": "a"}}],
  "reconcile": [{
    "type": "ex:Row", "key": "source_key", "under": "host_a/", "complete": true,
    "fields": ["label"], "edges": ["ex:row_of"],
    "items": [{"key": "host_a/one", "attrs": {"label": "x"},
               "edges": [{"rel": "ex:row_of", "target": "@thing"}]}]
  }]
}
```

- **Source and keys.** `source.name` is a logical name (`a-z`, digits, `_`, at
  most 64 characters), never a location; `version` and `run` are optional
  text of at most 128 characters. A scope names a `type` (its subtypes
  belong to it) and a `key`: a unique, single-valued, plain text attribute
  of that type. An item's `key` (1 to 512 bytes of printable text, no
  leading `/`, no blank at either end) is stored as `<source name>/<key>`.
  That is the ordinary uniqueness among active entities, so it holds against
  every writer, and a source can only reach entities under its own name;
  `under` narrows a scope to the keys that start with it. The ontology
  declares the attribute (an optional unique text; the recommended name is
  `source_key`, see the `ontology-best-practices` admin skill), nothing else
  changes in it.
- **Fields and edges.** `fields` lists the attributes the source sets: an
  item sets them, and a listed field an item leaves out is unset on its
  entity. An attribute outside `fields` is never touched, so curated
  attributes can sit beside synced ones. `edges` does the same for outgoing
  relations: the edges the entity has now are compared with the item's by
  relation, target and qualifiers, missing ones are linked and extra ones
  unlinked (unlinks first). An item may use only its scope's fields and
  relations. A target is an id, an alias, an `@local` (of the document's
  `ops`, of an item, or of an earlier document) or
  `{"type": "<type>", "attr": "<unique attribute>", "value": <value>}`: the
  active entity of that type that holds the value, or the item of the
  document that will. An item that restates the stored values changes
  nothing: values are compared as a write would store them (text converted
  by the datatype, the graph language on language-tagged text).
  `valid_from` and `valid_to` of an item apply when its entity is created.
- **Partial and complete.** A scope is partial unless it says
  `"complete": true`: it creates and updates, never retires, so a pipeline
  that forgets the flag leaves stale data, not wrong retires. In a complete
  scope the entities no item names retire, unless `keep` lists their keys
  (what the source did not observe this run). A retire is a retract (the
  default; final, a key that returns is a new entity) or, with
  `"retire": {"transition": "<state>"}`, a lifecycle transition that keeps
  the entity, its id, its incoming edges and its key; `attr` names the
  lifecycle attribute when the type has several, and `revive` is the state
  an entity moves back to when its item returns. An entity already in the
  retire state is neither counted nor retired again.
- **Moves and adoption.** `moves` keep an entity's identity when the source
  changes its key: `{"from": <old key>, "to": <new key>}` gives the entity
  with the old key the new one (the key attribute must not be immutable);
  `{"id": <ref>, "to": <key>}` gives an entity that has no key yet its key,
  how a graph adopts entities written before it had keys. A move that
  already happened (the old key gone, the new one held) is a no-op, so a
  rerun is safe. graph_cli takes the mapping and never guesses it.
- **Guards.** Each refuses the whole document, writes nothing and, as the
  planner finds it under the lock, is an audited `invalid` refusal (exit 1):

  | Guard | Code | Override |
  | --- | --- | --- |
  | a complete scope without items while entities would retire | `reconcile.source_empty` | `allow_empty: true`; a source that failed sends no entry |
  | more retires than 5 and a tenth of the scope | `reconcile.retire_limit` | `allow_retire: N`, N at least the count the refusal names |
  | item count outside `expect` (`min_items`, `max_items`) | `reconcile.shape` | change `expect` |
  | the key attribute is missing, not unique, not single-valued or not plain text, or immutable with `from` moves | `reconcile.key_attribute` | the ontology or the document |
  | two scopes cover the same entities | `reconcile.scope_overlap` | the document |
  | the document's own `ops` touch an entity of a scope or create under its keys | `reconcile.scope_conflict` | the document |
  | a move names no entity, a taken or repeated key, or an item that is not there | `reconcile.move_invalid` | the document |
  | an item's type is no concrete type of the scope or differs from its entity's | `reconcile.type_mismatch` | retire and recreate explicitly |
  | an edge target by value or alias matches no single entity | `reconcile.target_not_found` | the document |

  Malformed input (a duplicate item key, a key outside `under`, an
  attribute outside `fields`, an unknown key) is `changeset.malformed`, exit
  2, nothing audited; a type or relation the caller does not see is
  `term.unknown`, like any unknown term.
- **Order and limits.** The document's own `ops` run first; the plan then
  moves keys, creates, updates, changes edges, revives and retires, one
  transaction for all of it. At most 32 scopes; at most 5,000 ops after
  planning, the document's own included (`changeset.too_large`): hand a large
  first import over as partial scopes in several documents, then one
  complete run. A run that finds nothing to do writes no transaction and no
  audit event.
- **Answer.** The write result has `reconcile`: the `source` and, per scope
  (`type`, `under`, `complete`), `created`, `updated`, `unchanged`, `moved`, `adopted`, `kept`, `retired`,
  `revived` and `retired_keys` (item keys; at most 20 in a write, all of them
  in a dry run). `--dry-run` shows the plan and writes nothing; `--summary`
  keeps the block. A retry that replays a recorded run has no `reconcile`
  block, because the transaction holds the expanded ops only. The locals of
  matched items are bound like those of created ones.
- **Retries and concurrency.** An idempotency key matches the whole request
  (the document and its source), not the resulting change: a retry with the
  same key replays the recorded result even when the state moved on, and
  another request with the key is `conflict.idempotency_key_reuse`. The plan
  runs under the lock, so a second run that overlaps the first finds its
  result and writes nothing.
- **Rights and audit.** What a document declares it may do is authorized
  before anything is planned, and its concrete ops again under the lock
  ([Access control](access_control.md#reconcile-documents)). The source is
  recorded as `request.source` ([Audit](audit.md)) and shown by `history` and
  `changes`; it is a claim, not an authentication
  ([Security model](security_model.md#documents-from-a-source)).

## Language-tagged values

A field of type `lang_string` or `lang_text` holds text in a named
language, as RDF language-tagged strings do: `{"lang": "de", "value":
"Steuerung und Kontrolle"}`, the tag in BCP 47 form (`de`, `en`, `de-CH`,
`zh-Hant`). Other text fields stay plain strings; `describe fields` shows
the base type.

- **Text form** (`--set`, `--add`, `--remove`, the `_text` keys of change
  sets, `find --where`): `"text"@tag` or `text@tag`, the latter when the
  tag's language part has two or three letters, so `user@example` stays
  text; the quoted form also holds quotes and `@` (`"a@b"@en`).
- **Without a tag** (text or a JSON string) a value takes the graph
  language that `graph graphs` shows (`graph-admin graph create
  --language`), and it is stored with it, so the stored value always names
  its language. A graph without a language refuses an untagged value
  (`data.structural.datatype_mismatch`). A tag is stored in canonical case
  (`DE-ch` becomes `de-CH`).
- **One main name per language**: a main label (`role: label`) of type
  `lang_string` may hold several values, at most one per language
  (`data.structural.language_repeated`); alternative and hidden names may
  repeat a language.
- **Comparisons use the text.** `find --where attr=text` matches the text
  in any language, `--where 'attr=text@en'` text and language;
  `contains`, `prefix`, `lt`..`ge` and `--sort` use the text (then the
  tag). `--where attr:lang=de` keeps entities with a value in that
  language; `de` also selects `de-CH`, `*` any language (RFC 4647 basic
  filtering). `--remove attr=text` without a tag removes the text in every
  language. A `unique` field compares text and language together; rules
  and duplicate warnings compare the text.
- **`lookup` and `search`** match the text and give the tag in
  `match.lang` (null for a plain string). `--lang de` keeps only names and
  text in that language (a plain string counts as the graph language) and
  shows the main label in it; otherwise the label is in the graph
  language, else in the language of the match.
- **`graph query`** sees a map: `n.<attr>.value` and `n.<attr>.lang`.

## Earlier states

The reads `get`, `history`, `find`, `lookup`, `search`, `neighbors` and `query` take
`--as-of` and then answer from the graph as it was: `tx:<seq>` reads the
state after that transaction (`tx:0` is the empty graph; the `tx_seq` of
an entity or of a `history` item names one), an RFC 3339 time with an
offset (`2030-01-01T12:00:00Z`, `+02:00`) reads the state after the last
transaction recorded at or before it. Everything comes from that state:
references (an alias as it was bound then), `current_rev`, `superseded_by`
and the status warnings of `get`, the revisions `history` lists, and the
entities and edges the other reads match. An entity created later is
`entity.not_found`. Names are resolved with the graph's current ontology.

The envelope's `graph.seq` is the seq that was read, and every such answer
carries the warning `temporal.as_of_read` with `{as_of, seq, head_seq,
ontology_version}` (the version the state was written under, that is of the
revisions its last transaction wrote). A
next-page command repeats `--as-of tx:<seq>`, also after a time, so it
reads the same state. A seq after the last transaction or a time without
an offset is `usage.invalid_argument`. An earlier state is history, so
`--as-of` also needs `read.history` (see
[Access control: History](access_control.md#history)).

Every write is a change set that commits completely or not at all;
validation findings, refusals and dry runs are described in
[Architecture: write pipeline](architecture.md#write-pipeline).

A `data.referential.domain_violation` or `data.referential.range_violation`
finding (also in `graph-admin graph check`) carries `connecting`, computed from the
ontology alone: `direct` lists the relations that accept the two types in
either direction, and only when there are none, `paths` lists up to three
chains of two relations over an intermediate class (each hop is `{relation,
from, to}`). Relations with an open domain or range, such as the provenance
relations, and deprecated relations are never listed; an abstract class is
never the intermediate one. The text output adds a line `connect with: a -rel->
b`.

A write that leaves an entity as it was (its content after all ops of the
document equals its current revision: `--add` of a value already there,
`--set` to the current value, an `add` and a `remove` that cancel out)
gives it no new revision. Its entry in `data.entities` has `changed:
false` and the current `rev`, and the answer, a dry run too, carries the
warning `entity.unchanged {ids, count}`. A document that changes no
entity commits no transaction: `tx_id`, `seq` and `event_id` are null, no
audit event and no idempotency key are recorded (a retry is evaluated
again), and the exit code is 0. <!-- claim: cli_graph.unchanged_write_records_nothing --> Expectations (`--expect`, `expect_seq`)
and errors are checked as for any write.

### Earlier revisions under current names

A major ontology release can rename classes, attributes and relations
(`renamed_from`) and migrate the data to the new names (see
[graph-admin: Releasing with a data migration](cli_graph_admin.md#releasing-with-a-data-migration)). Stored
revisions keep the names they were written with; reads show them under the
names of the pinned ontology instead, so an answer never holds a name the
model does not know:

- `get --rev`, the revisions behind `history` and `changes` (their `changed` names) and every
  revision of an `--as-of` state are mapped: renamed classes, relations and
  attributes, a `retype` of the migration, and enum values, states and
  qualifiers its `map_values`, `map_qualifier_values` and `rename_qualifier`
  steps re-keyed. What the other steps changed (`fill`, `drop`, `drop_edges`,
  `drop_qualifier`, `convert`) is not mapped: such a revision shows what it
  stored, for example a plain string where the model now has a
  `lang_string` (filters compare its text).
- A mapped revision keeps its `rev`, `tx_seq` and `ontology_version`; its
  `content_hash` is recomputed over what is shown, and `--full` adds
  `names_mapped: true`. Current revisions are never mapped: after a major
  release they fit the pinned ontology, whatever their `ontology_version`.
- An `--as-of` state whose names changed since is answered over its mapped
  revisions: `find`, `lookup`, `search`, `neighbors` and `query` name current
  terms and match the old revisions under them (read limits judge the
  mapped type), and `temporal.as_of_read` says so ("its names are mapped
  to those of <version>") with `names_mapped: true` in its details.
- In `history`, the revision a migration wrote carries `migration: {from,
  to, summary, renames, steps}`: the versions, the release's summary, and
  the renames and steps that changed this entity (as `graph-admin graph
  check --ontology` previews them, without counts). Its `changed` lists
  only what the mapping does not explain.
- An old name on input is never accepted: `term.unknown` says what it is
  called now ("inv:Item was renamed to inv:Product in 2.0.0", each step for
  a name renamed in several releases), with `details.renamed: [{from, to,
  steps: [{to, in, class?, how?}]}]`. A name whose new term the caller cannot
  see gets no hint.

While a release's migration is committed but the pin is not switched yet (a release
running, or interrupted), reads use the target version's names; `graph.migration_incomplete`
warns when no release is running, which means it crashed, and every write is refused with
`write.migration_incomplete` until a maintainer runs the release again ([graph-admin: An
interrupted release](cli_graph_admin.md#releasing-with-a-data-migration)). Nothing helps from
the app side: ask the maintainer.

## Changes since a position

`graph changes --since <position>` answers the question of a consumer that keeps
something derived from the graph (an index, a cache, a report): what changed
after the point it has read up to. It lists the transactions after a position,
oldest first, and per transaction the entities it changed, as ids, types,
revisions and the names of what changed, never values; a consumer reads
values with `get` or `find` (`find --where tx_seq:gt=<N>` selects what changed
after a transaction). It needs `read.history`, like `history` (see
[Access control: History](access_control.md#history)), and has no `--as-of`:
the position is the start, the head of the graph is the end.

**Positions.** `--since` is `tx:<seq>`, `tx:0` for the start of the graph; every
change through that transaction is taken as read. `tx:<seq>/<entity id>` is a
cursor inside a transaction that did not fit a page: the transaction is
delivered through that entity, the others of it follow in ascending id order.
The id of a cursor only sorts, nothing looks it up. An RFC 3339 time with an
offset is the position after the last transaction recorded at or before it,
and the answer names it as `tx:<seq>` in `since`; a time without an offset, a
malformed position and a seq beyond the graph's last are
`usage.invalid_argument` (the last one with `details.head_seq`).

**Paging.** A page holds at most `--limit` entity items (default and
maximum in the [command reference](../skills/graph-cli/references/commands.md)); whole transactions are taken while their items fit. A transaction with
more items than the limit is cut into entries marked `partial: true` that
continue on the next page. At most 1,000 transactions are read per call, so a
page can be empty and still have `more: true` when the caller sees nothing of
the transactions it went through; `next` has moved on. Keep calling with `next`
until `more` is false. A write that commits between two pages is on the next
page.

**The answer.** `data` has these keys, in this order:

| Key | Meaning |
| --- | --- |
| `since` | the position asked for, a time as `tx:<seq>` |
| `since_tx_id` | the id of the transaction at that seq, `null` for `tx:0` |
| `head` | the graph's seq |
| `next` | the position after this page: the `--since` of the next call |
| `next_tx_id` | the id of the transaction at the seq of `next`, `null` for `tx:0` |
| `more` | `true` when another call may return more |
| `view` | `null` without read limits, else a digest of what the caller may see |
| `items` | one entry per transaction in which the caller sees a change |

An entry has `tx_seq`, `tx_id`, `recorded_at`, `command`, `reason`, `source`
and `own_session` as `history` shows them, `action` (`name`, `hash`) for a write
by `do`, `migration` (below), `partial` and `entities`. Each item of `entities` has
`id`, `type`, `rev` (1 is the creation), `record_status`, `changed` (the names
of `history`, `null` for a creation) and `targets`: the ids of the entities at
the other end of the relations the revision added, removed or changed, since
their view of the graph changed too (at most 50, then `targets_more` counts the
rest). The text form prints a header line per transaction and a line per
entity.

**Migrations.** A data migration of a major ontology release is always an
entry, with `migration: {from, to, summary}` (`summary` when the release had a
migration file) and the entities of it that the caller sees, possibly none:
names may have changed, and an entity may have left the caller's view by a
retype. A consumer re-reads what it holds when it meets one.

**Gaps.** The `tx_seq` of entries are not consecutive: a transaction that
changed nothing the caller sees has no entry. A gap is no loss.

**Consumer contract.**

1. Start with `--since tx:0`. After each page store `next`, `next_tx_id` and `view`,
   and loop while `more`.
2. Start again from `tx:0` when `view` differs from the stored one, when
   `since_tx_id` differs from the stored `next_tx_id` (the graph was restored
   or rebuilt: the log you read is gone) or when `--since` is refused with
   `head_seq`.
3. On an entry with `migration`, re-read every entity you hold or rebuild.
4. Take values with `get` or `find`; a `get` of an id you hold that answers
   `entity.not_found` means the entity is gone for you.
5. A projection built from one caller's feed holds what that caller sees: serve it
   only to callers with the same `view` on the same graph.

A caller with [read limits or markings](access_control.md#read-limits) gets the
feed of the entities and attributes it sees, built like `history`: no hidden id,
type or attribute name appears, a transaction with nothing visible has no entry
(the gaps), and a revision whose only change was hidden has an empty `changed`.
[Security model](security_model.md#type-read-limits) lists what stays visible.
Consumers poll the command; the server offers no long poll or stream for it
([Server: API](server.md#api)).

## Fewer fields

`get`, `find` and `neighbors` show an entity's content by default: `id`,
`alias`, `type`, `rev`, `record_status`, `attrs`, its `edges` (relation,
target, and qualifiers and pinned revision when set) and `valid_from` and
`valid_to` when set. `--full` adds how it was stored: `created_at`,
`recorded_at`, `tx_seq`, `ontology_version`, `content_hash`, the validity
also when null, and the edge ids (`unlink --edge-id`); the edges
`neighbors` walked carry their ids then too. A node that `graph query`
returns is shown as without `--full`. Nothing an agent works with needs
the rest: `--expect` takes `rev`, and `graph history` lists the
transactions for `--as-of`.
`--fields` (repeatable, comma-separated) keeps only `id`, `alias`, `type`,
`rev` and `record_status` (always: `rev` is what `--expect` needs), `attrs`
with the named attributes (null when an entity has no value), and the
named entity parts: `edges` or a system field such as `tx_seq` or
`recorded_at`, also without `--full`. A name no type in scope has is
`term.unknown`. A next-page command repeats `--fields` and `--full`. `query ... RETURN` is the projection of
`graph query`; `lookup` and `search` answers are compact already.

## What an entity can do next

`get REF --affordances` adds an `affordances` object to the answer; without
the flag the answer is unchanged. It is computed from the current revision
of the snapshot (with `--as-of`, the snapshot of that point) and nothing is
stored. It holds `record_status` and the groups below, each cut at 40 entries
(`<group>_more` counts the rest):

- `transitions`: per lifecycle attribute, the transitions out of the current
  state; `allowed` is false with `blocked_by` (guard id and summary) when a
  guard fails. A terminal state has none.
- `relations`: the relations the entity's class can start (`out`) or end
  (`in`), with `count` of active edges, `min`, `max` and `room` (null when
  unbounded; `missing` when below `min`). Relations at their maximum are
  only named in `full`.
- `actions`: the visible actions with a `ref` parameter that covers the
  entity's class (subclasses included), one entry per such parameter (`as`).
  `applies` is judged from the preconditions on that parameter; the
  preconditions on other parameters are named in `unchecked`. `applies: true`
  means no known obstacle, not a promise: `graph do --dry-run` is the check.
- `functions`: the readable functions with such a parameter (`as`), without
  a state judgment.
- `views`: only when the graph has views you can call: the entity views that
  take the entity's class (subclasses included), by name, except the one
  `view REF` already shows (the default view of the class) and deprecated ones;
  at most five, the rest counted in `views_more`. A graph without such views
  has no `views` key, so its answers are unchanged.

A superseded or retracted entity has `record_status` and empty groups.
Actions and functions are listed as `describe actions` and `describe
functions` list them (what the caller may not see stays out, nothing is
recorded); transitions and relations are model-level and not filtered by the
caller's write rights, which still apply when writing. `--affordances` with
`--rev` is `usage.invalid_argument`: a past revision has no next step.

## An entity at a glance

`view REF` answers "what is relevant about this entity?" in one call, where
the same question is otherwise a walk of `get` and `neighbors` calls to learn
what the neighbours are called. It is a state, not a record: it shows `rev`,
but the revision for `--expect`, the system fields and the edge ids stay with
`get`, which is unchanged. The card is computed from the snapshot the caller
reads (with `--as-of`, that of the point), by the same code for every graph;
nothing is stored or cached, and a caller who may not see a type, an
attribute or a relation sees no trace of it in counts, names, labels or
files. `--rev`, `--fields`, `--full` and a size option do not exist: the
size is fixed. `--as-of` needs `read.history`, as for `get`.

The JSON `data` (`graph_cli.view_model/1`, `view_model` in `describe commands
--output-schemas`) is the contract; the text form is one rendering of it. It
holds `view` (`name: card`, `origin: generated`), `entity` (id, alias, type,
`rev`, `record_status`, `label`, and `superseded_by` or `retracted_by` as for
`get`), `sections` and `bytes`. A section has a `kind`; a consumer ignores a
kind it does not know. No key has an empty value: a part with nothing in it is
left out (no `fields` section for an entity without values; the `affordances`
section of an entity that cannot do anything is only its `kind`). The sections,
in order:

- `fields`: at most eight items, in this order: the main label (`role:
  label`, in the graph's language), the alternative labels (`alt_label`, at
  most three values), the lifecycle states (`state`), then the attributes
  marked `required` and those marked `unique`, by name (not hidden labels,
  not the hash of a stored file). Every other attribute with a value is only
  named in `other` (twelve names, then `other_more`), to be read with
  `get --fields`. A string is cut at 100 characters (`chars` is its length),
  a list at five elements (`more`).
- `relations`: one group per relation and direction with at least one edge
  the caller sees, out before in; within a direction the relations that
  place an entity (at most one edge) and the required ones come first, then
  by name. Relations of the `core` module (what an entity replaced, withdrew
  or was derived from) are groups like any other; `files:from_file` is not
  (see `files`). A group has `count`, `items` (the first three neighbours by
  name, not by importance, each with `ref`, `label`, `type` when the group
  mixes types, `state`, `record_status` when not active, `field`: its first key
  field, that is its first `required` (else `unique`) attribute with a text value
  by name, cut at 60 characters, read from what the caller sees; and the edge's
  `qualifiers`), `more`, and `states` (the neighbours per lifecycle state)
  when they share one lifecycle attribute and some are not shown; `missing`
  when the edges are below the relation's minimum. At most eight groups;
  the other relations are named in `more_groups` (relation names without
  direction; they are not read, so an in-relation whose sources are all
  inactive may be named there although it would have no group). The count of
  an in-group counts active sources. A group of more than a hundred edges is
  read from the first hundred in stored order (`sampled`), and an in-group then
  shows the number of in-edges as `count` with `approx: true`.
- `files`: the stored files an entity was taken from (`files:from_file`),
  five at most ordered by title and page, with `page`, `last_page` and
  `locator`, never a path or a hash; for a stored file itself the variant
  `cited_by`: `count` of citing entities, `page_min`, `page_max`,
  `pages_cited` and the first five citations by page (`ref`, `label`,
  `page`); more than fifty citations are read from the first fifty by page
  (`sampled`, `approx: true`), and the page statistics then use every edge.
  It does not say what is on a page.
- `affordances`: what `get --affordances` lists, by name: per lifecycle
  attribute the states it may move to (`allowed`) and those it may not
  (`blocked`, with the ids of the failing guards), the actions that apply
  and how many are blocked, the functions and the other entity views of the
  entity's class (`views`; absent when there are none), at most five names per
  list and the rest counted (`actions_more`, `functions_more`, `views_more`),
  and `detail`, the command that shows everything. Judged as `get --affordances` judges
  (`applies` means no known obstacle). A superseded or retracted entity has
  none.

The whole data stays below 6 KiB: the caps above, then, if a card is still
larger (long labels, ids instead of aliases), a fixed order of trimming
(the `field` of the neighbours, neighbours per group to one, trailing groups to
`more_groups`, values to 40 characters, trailing fields to `other`, file
references), after which `cut` is true. Whatever is left out is counted, never dropped silently, and an
envelope hint names the command that reads it: `neighbors REF --rel
<relation>` for neighbours, `get REF --fields <names>` for attributes. The
same input gives the same bytes. The text form puts each group, the files
and what applies next on one line:

```text
PRJ-0007  Example project  (ex:Project, rev 4, active)
  name: Example project
  status: active [state]
  other fields: description, notes
ex:owned_by ->   1  TEAM-0002 Example team
ex:has_task ->  12  first 3 by name: TASK-0031 Draft the spec [open]; TASK-0032 Review the spec [open]; TASK-0040 Ship [done]  (+9; 3 open, 9 done)
files: FILE-0003 "Project brief" p. 5-7
can: status -> paused; do close_project; run project_tasks
detail: graph get PRJ-0007 --affordances
```

`graph view` is not `graph-admin view`: that one starts the live view, which
composes its entity panel by itself ([Live view](live_view.md#using-the-view)).

## Authored views and set views

A maintainer can publish **views** next to a graph's functions and actions: named compositions of
reads with the answer shape of the card (`graph_cli.view_model/1`). The files are in
`graph_cli.view/1`, written with the `authoring-graph-views` admin skill
([format reference](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/authoring-graph-views/references/format.md),
the one home of the keys, limits, checks and bounds); this section is what a caller sees.

Finding them: `describe views` lists the views you can call (name, module, summary, the class an
entity view `takes`, `default`, signature), `describe view NAME` shows one completely and ends with
the command that calls it, `describe search TEXT --kind view` finds one by what it is for, and
`get REF --affordances` and the card name the other entity views of the entity's class (`views`). A
graph whose skill is generated says in its `start` block that the graph has views.

**Entity views** take a class (`takes`, subclasses included) and are called with a REF:
`view REF --name NAME`. Without `--name`, `view REF` shows the *default* view of the entity's class
if the graph published one (the nearest class on the entity's class chain that you can see wins),
else the generated card; `--name card` is always the generated card. An entity view has the card's
limit of 6 KiB, trimmed in a fixed order, and its sections are chosen by the maintainer: fields,
relations, `neighbors` (the neighbours over one relation as rows with chosen fields, filtered and
ordered), files, affordances and `rows` (the result of a published function for this entity). A REF
of a class the view does not take is `view.class_mismatch`; a view that needs a REF without one is
`view.takes_entity`.

**Set views** take no entity and list rows of a whole set, for scripts and renderers: `view --name
NAME`, with a REF `view.takes_no_entity`. Their `list` sections give every active entity of a class
(or of several, with the concrete `type` per row) with attributes as `get` shows them, never cut, and
the relations as neighbour keys (`ref`: the alias, else the id), in the order of `find` (the targets of an out-relation in edge order, up to 500 per list unless the view says less). The answer
is a **page** of whole rows, at most `--page-bytes` bytes (by default the view's own size, else the program's default in the [command
reference](../skills/graph-cli/references/commands.md); a row larger than the page goes alone with `oversize: true`). A page that does not reach the
end has `next` and `more: true`; `--cursor` with the same `--name` and `--param` gives the next page,
and the pages put together are the one-page answer. After a write the cursor is `view.cursor_stale`
(start again, or `--as-of tx:<seq>` pages the state you started with); after another view, other
parameters, a functions publish, an ontology release or a change of what you may see it is
`view.cursor_invalid`. `--cursor` and `--page-bytes` with an entity view or the card are
`usage.invalid_argument`. The pages of a set view are also the paging of its tool for harnesses
([Tools for agent harnesses](#tools-for-agent-harnesses)).

`--param name=value` (repeatable) gives a view parameter, read by its declared type as for `run`; a
missing or unknown one is `view.parameter_missing` or `view.parameter_unknown`. `--as-of` reads the
sections at that state (it needs `read.history`); a `rows` section cannot run then and answers alone
with `view.section_unavailable`.

A section that fails, for example a `rows` section whose function no longer holds, answers alone as
`{kind, id, function, error}` with the warning `view.section_failed`; the rest of the view still
comes. A count that could not read every edge says `approx: true` and how many were `unread`. A view
published against an earlier ontology release that no longer reads the same refuses a call that
names it with `view.stale`; plain `view REF` then answers with the generated card and the warning
`view.default_stale`. A deprecated view answers with `view.deprecated`.

Every call reads your own snapshot and model: what you may not see is not a row, a neighbour, a
count or a name, and a view that names anything you may not see is not found at all
([Views](access_control.md#views)). Nothing is stored or cached by the tool; the answer is a function
of the view (`view.hash`), the parameters, the page size, the cursor, the graph's `seq` and what you
may see, which is the key a consumer that caches pages can use, and `changes` says when to drop them.
The call is logged with the view's name and hash, never a parameter value or an id.


## Names and lookup

Names come from label roles in the ontology: an attribute of type `string`
or `lang_string` (or a datatype over one) may carry `role: label` (the main
name, single or one per language, at most one per class), `alt_label` (synonyms, abbreviations) or
`hidden_label` (searchable, never shown), as in SKOS. `describe` shows the
role of each field and the JSON Schema export annotates it as
`x-graph_cli-role`. `lookup` matches normalized text (NFKC,
casefold, whitespace collapsed; never stored) and ranks exact before prefix
matches, main before alternative before hidden labels, then shorter names;
a hidden-label match does not echo its value. Labels are not identifiers:
references stay ids, aliases and `@local` names. Types without label roles
are `term.no_labels`. When nothing starts with the text, the hint is a
`search`.

`lookup` takes up to 50 names, one per argument: a name with spaces is
quoted as one shell argument (`graph lookup 'Example name' EX`); `graph`
splits nothing itself, since names hold spaces and commas. Names with the
same normalized form are looked up once. Every item carries the `text` it
matched, items are in the order of the names and then ranked, and
`data.texts[]` gives `{text, total, exact, excluded}` per name, also for one
without a match. `--limit` and `--offset` apply per name; `page.total` and
`page.shown` are sums, and the next-page command repeats every name. Names
without an exact match get one `--match prefix` hint; with `--match prefix`,
names without a match get a `search` hint each (at most five).

Names are not unique, and the same name often belongs to entities of
different types (a department and a cost center, a subject and a topic).
`--for <relation>` keeps the candidates whose type is in the relation's
range, subtypes included, because the entity is about to be its target;
`--for <function>.<param>` (or `<action>.<param>`) does the same with the class
of a function's or action's `ref` parameter (it needs the right to run functions
or the action, otherwise the name is `function.not_found`, also for one you see but
may not use, [Entries you may not use](#entries-you-may-not-use)); `--for-source <relation>` keeps the types
of its domain. A view's `ref` parameter is `--for view:<view>.<param>` and the
entity of a view that shows one entity is `--for view:<view>._entity`, with the
class the view takes: a view needs its kind in front of the name because it may
share its name with a function (a plain `<name>.<param>` is an action or a
function, never a view). The kind may be written for functions and actions too
(`function:<function>.<param>`, `action:<action>.<param>`), which then look in
that catalog only. A view you do not see or may not use is `view.not_found`,
like the function and action forms; an unknown kind is `usage.invalid_argument`,
a parameter the entry does not have is `<kind>.parameter_unknown` (`_entity` of a
set view too). Both options may be given and narrow `--type` further; when
no type is left the answer is `usage.invalid_argument`. Nothing is picked
for the caller: every fitting candidate is listed, and matches of other
types are only counted (`data.texts[].excluded`). `data.fit` names the
options and the fitting types (null for a relation that takes any type).
When the options leave no exact match although others matched, a hint
gives the command without them; when a name has several exact candidates
and no option was given, a hint names `--for`.

A name with more than one candidate gives every candidate `facts`: up to
three of its links over relations that allow one target per entity (its
parent, owner, unit; required relations first), each `{rel, id, alias,
label}` with the target's main name. They tell the candidates apart without
another call. A name with one candidate has `facts: null`.

A write that gives an active entity a name (a value of a label-role field)
that another active entity of the same type family already carries, as
main, alternative or hidden name, gets the warning finding
`data.semantic.possible_duplicate`, in the dry run too. The comparison is
looser than `lookup`'s, because a warning should rather ask once too often:

- `same`: equal apart from case, accents, punctuation and word breaks
  (`E-Mail`, `email`, `E Mail`).
- `similar`: the same words but one, and that one a typo of the other (one
  or two edits, by the word's length). A changed number never
  counts, so `Part 1` and `Part 2` stay apart, and neither does a short
  word.

The family is the type that declares the field and its subtypes; other
types may share a name. `actual` lists up to five candidates, `same` ones
first (`id`, `alias`, `type`, `attr`, `role`, `name`, `match`; `name` is
null for a hidden label). The warning never refuses the write: reuse the
candidate, or give the new entity a name that tells them apart. Only names
the entity did not carry before count, and a value that a `unique` field
already refuses is not reported twice.

A write that puts a deprecated term to use gets the warning finding
`data.structural.deprecated_term`, in the dry run too: it creates an
entity of a deprecated type (path `type`), sets or changes a value of a
deprecated field or of one whose datatype, enum or lifecycle is deprecated
(`attrs.<attr>`), or adds an edge of a deprecated relation
(`edges.<relation>`). The message carries the term's summary, which names
its replacement; `actual` is the term. Values and edges the entity already
had are not reported again. The warning never refuses the write.

## Search

`graph search TEXT` answers "is there anything about this": it reads every
field whose type is `string`, `text`, `lang_string` or `lang_text` (or a
datatype over them; a language-tagged value by its text), each
value of a list, on every type or the `--type` families, and `--field`
limits it to named fields. Enum values, lifecycle states, files and edge
values are not searched. Text is compared on a looser key than `lookup`
uses: case, accents and every character that is not a letter or digit are
ignored, so "Light-Refraction" equals "light refraction". A value matches

- `exact`: it equals the text;
- `phrase`: it contains the text, also inside a longer word;
- `words`: it contains each word of the text, in any order;
- `similar` (names and short fields only, not `text` or `lang_text`): at least 70% of
  the text's letter triples occur in it, and at least half of each word's,
  so a typo or a compound still matches and a missing word does not.

Results are ranked by field first (names before other `string` fields
before `text` fields, so a close name comes before a phrase in a long
description), then by kind, then main before alternative before hidden
name, then closeness, then the shorter value. Each item names its best match (`attr`, `role`, `kind`,
`score` in percent, an `excerpt` of up to 160 characters, null for a
hidden name) and every field that matched. Results are suggestions, not
references: check one with `get` before writing to it. Abbreviations are
found only when an entity carries them as an alternative name. Types or
fields without text are `term.no_text_fields`.

## Cypher queries

`graph query` answers a read-only subset of Cypher over one snapshot of the
graph, with the same access rules and the same data as `find`. Use it when
a question needs several hops, OR or NOT across relations, or counts;
`find`, `neighbors` and `lookup` stay the simple path for one type, one hop
or a name. Writes are refused (`query.unsupported`, hint `apply`).

How the graph looks to Cypher:

| Cypher | graph_cli |
| --- | --- |
| node | the current revision of an entity; only `active` entities unless `--include-inactive` |
| label | the entity's type and every supertype, so `(:T)` includes subtypes, as `find --type` |
| node property | an attribute or a system field (`id`, `alias`, `type`, `rev`, `record_status`, `created_at`, ...) |
| relationship | an edge in its stored direction; its type is the relation, its properties are the qualifiers and `edge_id` |

A label or relationship type is the local name when exactly one CURIE has
it (`(:Type)`), else the CURIE in backticks (``(:`ex:Type`)``), which always
works; an ambiguous local name is `query.ambiguous_term` with the
candidates. `labels(n)` and `type(r)` return names the same way: the local
name when it is unique, else the CURIE. Unknown labels, relationship types
and fields are `term.unknown` with did-you-mean, as for `find`.

The supported clauses, patterns, expressions and functions are listed in
the generated [`references/query.md`](../skills/graph-cli/references/query.md).
Everything else, such as write clauses, `CALL` or `UNION`, is
`query.unsupported` with the construct and, where one exists, the
alternative. A variable-length relationship needs an upper bound within the
limit the reference names. A null argument makes a function null (`coalesce`
excepted).

Semantics follow openCypher: three-valued null logic, a missing property is
null, comparisons across types are null, a relationship is used once per
`MATCH`. Where graph_cli differs:

- A `--param` value that parses as JSON is bound as JSON, with one
  exception: a number (or a JSON list of numbers) that the query compares
  only with text fields is bound as the text as written, so `--param n=2.10`
  matches the text `"2.10"` and not the number 2.1. That holds when every
  use of the parameter is `=`, `<>`, `IN`, `STARTS WITH`, `ENDS WITH`,
  `CONTAINS` or an inline map against fields whose base type is text
  (string, text, IRI, enum, lifecycle state); any other use, such as
  `RETURN $n`, `<` or a numeric field, keeps the number. Fields of
  anonymous nodes and relationships are not looked up. `--explain` shows the
  bound text. A number written in the query is never rewritten: comparing it
  with a text-only field warns `query.number_for_text`, naming the variable,
  field and literal; quote the literal.
- Numbers with a fraction are exact decimals, and so are decimal
  attributes; JSON output shows them as strings, as `get` shows decimal
  attributes. There is no NaN: a division by zero is `query.invalid_value`.
- There are no floats: `toFloat` returns a decimal, and `abs`, `ceil`,
  `floor` and `round` return an integer for an integer, else a decimal.
  `round` rounds half away from zero (`round(-2.5)` is `-3`) and takes an
  optional number of decimal places.
- The variable of a list predicate or comprehension must be a new name
  (openCypher allows shadowing). Indexing and slicing take lists only.
- `keys(n)` and `properties(n)` list the system fields that have a value,
  then the set attributes by name (a relationship: `edge_id`, then the
  qualifiers). `n.id` is the entity id (`id()` is refused).
- Inactive entities are hidden at both ends of every pattern, so an edge
  to a superseded entity does not match (`find --rel` and `neighbors`
  follow it).
- Rows without `ORDER BY` come in a fixed order (by id), so results are
  reproducible.
- `WHERE` after `WITH DISTINCT` sees only the columns, like `ORDER BY`.
- `NOT n.attr = 1` is null on a missing attribute and drops the row; `find`'s
  negation keeps it.

Output: `data.columns` in `RETURN` order and `data.items`, one object per
row keyed by column; a node is shown as `get` shows an entity, a
relationship as the edge with its `source`. `--limit` and `--offset` page
the rows after the query's own `SKIP`/`LIMIT` (`page.total` is exact, with
a next-page hint). Text output is a table with entities as `alias type`.
`--explain` returns `data.plan` (the resolved clauses as Cypher with CURIEs,
the variables each binds with their possible types, the columns, the
parameters used and the fields with their base types) and runs nothing.

When a published function you may run covers the query (it takes the same
kind of reference and returns what the query returns), the answer carries up
to two hints, best first and before the next-page hint, also with
`--explain`: the function's summary and a `graph run` command. The hint says
whether the parameter takes one value or a list; a function that takes one
value per run is named as such. A hint is never a rewrite of the query.

Work is bounded: more than 100,000 candidate nodes, edges or rows end the
query with `query.too_large`, naming the clause; add labels, a property map
or a `LIMIT`. Errors carry `line` and `column`: `query.syntax` (exit 2, with
what was found and expected), `query.unbound_variable` and
`query.parameter_missing` (exit 2), `query.unsupported`,
`query.ambiguous_term`, `query.too_large` and `query.invalid_value` (exit
1).

## Entries you may not use

A function, action or view is one of three things for a caller (the rules are in
[Access control](access_control.md#functions-actions-and-views-you-may-not-use)):

- **allowed**: you see everything it names and hold the rights it needs;
- **not allowed**: you see everything it names but lack a right (`read.function` for a function,
  `read.get` for a view, `read.find` for a set view, `read.function` for a view that runs functions,
  a write right for an action);
- **hidden**: it names something you cannot see; it does not exist for you.

Lists and counts say what you may use. A list adds `not_allowed`, the number of entries you see but
may not use, when there are any, and `--include-not-allowed` (on `describe functions`, `actions`,
`views` and `module`) lists them after the others. `describe search` and `describe function|action|view
NAME` show such an entry marked `allowed: false` with `missing`, the rights you lack (a list of
`{action, resource}`; what it holds is in
[Access control](access_control.md#functions-actions-and-views-you-may-not-use)). A search ranks it after the allowed hits of the same rank and
matches it on its name, summary, `use_when` and parameter names only. The description gives what the
entry is for and how it is called (summary, `use_when`, signature, parameters without default values),
not its query, operations, conditions, sections, examples, touched terms, needs or hash. An entry you
may use has neither key. Listing, counting and searching record nothing. Calling a not-allowed entry is
a recorded access: `run`, `do` and `view` answer `access.denied` naming the right you lack (exit 3).
Tell the user instead of working around it; a maintainer can give your token a role that holds the
right.

## Functions

A graph can carry named read functions: curated, typed queries that the
maintainer published for the questions people ask again and again. They
are found and read, never changed, with the `describe` commands:

- `describe functions` lists them, `--module M` narrows the list to one
  module, `--include-deprecated` adds the deprecated ones (a deprecated
  function names its `replaced_by` in `describe function`).
- `describe function NAME` shows the definition. An unknown name is
  `function.not_found` (exit 1) with the closest names in `did_you_mean`
  and the hint `graph describe search <words> --kind function`.
- `describe search TEXT --kind function` ranks functions like the other
  terms: the name (read as words, `members_of_unit` has three), the summary,
  then, in the last tier, what the function is for (`use_when`, the example
  questions, the parameter names; `match.field` is `use_when`) and the
  local names, inverse labels and summaries of the types and relations its
  query touches (`match.field` is `touches`). A question in plain words
  therefore finds the function that answers it.
- `describe modules` and `describe module M` count and list the functions per module.
- A function you see but may not run (no `read.function`) is counted as `not_allowed`, shown by `describe function NAME` and found by `describe search`, marked `allowed: false` with `missing`; see [Entries you may not use](#entries-you-may-not-use).
- `run NAME --param name=value ...` runs a function. Each `--param` is read
  by the type the function declares (`string`, `integer`, `decimal`,
  `boolean`, `date`, `datetime`, `enum`, `ref`), never guessed from its
  look, and a list parameter repeats its name once per value; a parameter
  with a default may be left out. Over the server API the same values come
  as `{"param": {"name": ["value"]}}`. A missing, unknown or malformed
  parameter is a usage error (exit 2: `function.parameter_missing`,
  `function.parameter_unknown`, `function.parameter_invalid`).
- A `ref` parameter takes what every other command takes for an entity (full
  id, alias or 8-32 trailing hex digits), never a name; the entity must exist
  and be of the declared class or one of its subclasses. A name goes through
  `lookup` first. An entity that does not exist is the usual
  `entity.not_found`; an entity of another class is `function.parameter_type`
  (exit 1, details `expected` and `found`, hint `graph lookup <text> --type
  <class>`).
- The answer is that of `query`: `data.function` (`name`, `hash`),
  `data.columns` and `data.items`, `--limit` and `--offset` page the rows
  with a next-page hint, the text form is a table. A run reads the current
  state only (no `--as-of`, no `--explain`) and is bounded like a `query`.
- A deprecated function runs with the warning `function.deprecated`, which
  names `replaced_by`. The stored query is checked against the graph's
  ontology on every run; if it no longer resolves (a pin that moved without
  the catalog being checked) the run is refused with `function.stale`: tell
  the maintainer.

A function is usable only by callers who may `read.function` on the graph
(see [Access control](access_control.md#functions)); for everyone else the
functions are *not allowed*, not absent ([Entries you may not use](#entries-you-may-not-use)).
A graph that has no functions shows nothing about them in any `describe`
command.

Maintainers write functions as one YAML file each and publish them with
[`graph-admin graph functions publish`](cli_graph_admin.md#commands). The
file format, the types of parameters and the checks publishing applies are
described once, in the format reference of the `authoring-graph-functions`
admin skill ([`references/format.md`](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/authoring-graph-functions/references/format.md)); the
[generated graph skill](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/authoring-graph-skills/SKILL.md#the-template) lists a graph's featured
functions.

## Actions

A graph can carry named write actions: reviewed, typed writes that the
maintainer published for the changes people make again and again ("a person
takes over a vacant position", "an employee leaves"). An action is a
template of change set ops with typed parameters and, where it matters,
preconditions on the state it will change. It writes exactly what the
primitives (`create`, `link`, `unlink`, `transition`, ...) would write, in
one step.

Find one before writing by hand:

- `describe search TEXT --kind action` ranks actions like functions (name,
  summary, then `use_when`, the example requests, the parameter names and
  the terms of the types and relations it touches). An unknown or hidden
  name is `action.not_found` (exit 1) with the closest visible names in
  `did_you_mean` and the hint `graph describe search <words> --kind action`.
- `describe actions [--module M] [--include-deprecated]` lists them;
  `describe action NAME` shows the definition: parameters, preconditions,
  the compiled operations, the write rights it needs and whether it
  requires a `--reason`. `describe modules` and `describe module M` count
  and list the actions per module.
- `lookup TEXT --for <action>.<param>` finds the entities a `ref` parameter
  accepts by name ([Names and lookup](#names-and-lookup)).

Run one with `do NAME --param name=value ...`, with `--dry-run` first:

- Each `--param` is read by the type the action declares, as for
  [functions](#functions) (a list parameter repeats its name; a parameter
  may be optional). A `ref` parameter takes a full id, alias or 8-32
  trailing hex digits, never a name, and the entity must exist and be of
  the declared class or a subclass (`entity.not_found`,
  `action.parameter_type`). A missing, unknown or malformed parameter is a
  usage error (exit 2: `action.parameter_missing`,
  `action.parameter_unknown`, `action.parameter_invalid`).
- A list parameter may stand for the target of an edge a `create` writes
  (one edge per value); the rules are in the format reference linked below.
- The preconditions are checked on the entities' current state under the
  graph lock, right before the write, so nothing can change between check
  and commit. A failing one refuses the action with
  `action.precondition_failed` (exit 1), and the answer lists every failing
  precondition with its summary, the parameter, the entity and, for a
  count, the actual value. Nothing is written.
- Some actions remove every edge of one relation at an entity (`unlink ...
  all: true` in the file, for example the current holder of a position).
  The `--dry-run` answer lists each edge it would remove, as any dry run
  lists the entities it would write; read it before running without it.
  When there is nothing to remove and nothing else to write the action is
  refused with `action.nothing_to_do` (nothing is recorded).
- An action marked `reason: required` is refused without `--reason`
  (`action.reason_required`, exit 2).
- `--idempotency-key` makes a retry safe. The key matches the request (the
  action and its hash, the parameters, the reason), not the resulting
  change: a retry with the same key and request replays the recorded result
  even when the state has changed since, and the same key with another
  request is `conflict.idempotency_key_reuse`.
- The expansion is one transaction (all or nothing) with one audit intent
  that names the action (`request.action`, see [Audit](audit.md)). The
  answer is the write result of any write plus `data.action` (`name`,
  `hash`). `--dry-run` writes and audits nothing.
- A deprecated action runs with the warning `action.deprecated`, which names
  `replaced_by`. The stored definition is checked against the graph's
  ontology on every run; if it no longer resolves the run is refused with
  `action.stale`: tell the maintainer.

An action needs no right of its own: it is allowed when every write it
expands to is allowed to you. An action whose writes you may not make is
not listed but counted as not allowed, and `do` of it is `access.denied`
(see [Entries you may not use](#entries-you-may-not-use) and
[Access control](access_control.md#actions)). A graph that has no actions
shows nothing about them in any `describe` command.
When no action fits, use the primitives.

Maintainers write actions as one YAML file each and publish them with
[`graph-admin graph actions publish`](cli_graph_admin.md#commands). The
file format, the checks publishing applies and the rules for preconditions
are described once, in the format reference of the `authoring-graph-actions`
admin skill ([`references/format.md`](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/authoring-graph-actions/references/format.md)); the
[generated graph skill](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/authoring-graph-skills/SKILL.md#the-template) lists the featured actions you may
run.

## Tools for agent harnesses

An agent harness (programmatic tool calling, an agent SDK, a CI job) builds the tools of a graph from
one document instead of rebuilding schemas from `describe` entry by entry. `describe tools` answers
it, over the server API like every `describe` command. Agents that work in a shell need none of this:
they keep `describe`, `run`, `do` and `view`.

**The manifest** is a `graph_cli.model/1` document with these keys, in this order: `tools_format`
(the shape of a tool, see the compatibility policy below), `version` (a hash of everything listed:
it changes when any tool or error entry listed changes), `tools` and `errors`. The options narrow
the list: `--tool NAME` (repeatable) keeps the named tools, `--kind` one kind of tool, `--module M`
one module (the `lookup` tool belongs to none), `--featured` the featured functions and actions (a view
and the `lookup` tool have no such flag and stay),
`--exclude-deprecated` leaves out the tools of deprecated entries, which are listed by default with
their `status` and `replaced_by` so that a tool a harness allows does not vanish when its entry is
deprecated. A `--tool` that names no tool you may use is `tool.not_found` (exit 1) with the close
names in `did_you_mean` and every unknown name in `missing`. Tools come functions first, then
actions, then views, each by name, and the `lookup` tool last.

**Rendered for you.** The manifest is built per request from the entries you may use and the model
you see. An entry you may not use is absent (the other `describe` commands can mark such an entry, the
manifest leaves it out), an entry that names something hidden from you does not exist for you, and the enum values and classes
in the schemas are those of your model, so a caller with type read limits or markings gets no tool,
value or class that names what it may not see
([Entries you may not use](#entries-you-may-not-use)).

Each tool has these keys, in this order:

| Tool key | Holds |
| --- | --- |
| `name` | the tool name, see below |
| `kind` | `function`, `action`, `view` or `lookup` |
| `entry` | the name of the function, action or view; null for `lookup` |
| `description` | the summary, a deprecation line, `use_when` and the examples of the entry: a function's and a view's with the arguments they give in the form of the input (an example never gives a reference, the line says to supply it), an action's request |
| `input_schema` | the JSON Schema (2020-12) of the arguments, closed |
| `output_schema` | the JSON Schema of the answer (`data`), open |
| `annotations` | `read_only`; `dry_run` (the tool has `_dry_run`); `reason` (`required` or `optional` for an action, else null); `paging` (`offset` for a function, `cursor` for a set view, else null) |
| `status`, `replaced_by`, `featured`, `module` | as `describe function`, `action` and `view` show them; `replaced_by` names the replacing tool, only when you may use it |
| `hash` | the hash of the name, description, both schemas and annotations: it changes when an ontology release changes an enum's values although the entry's `hash` does not |
| `entry_hash` | the `hash` of the entry, the one `run`, `do` and `view` record in the audit; null for `lookup` |
| `errors` | the codes the tool may return, sorted |

**Names.** A tool is named by the kind of its entry and the entry's name: `run_NAME` for a function,
`do_NAME` for an action, `view_NAME` for a view; the name `lookup` is the lookup tool (below). Functions
and actions share one namespace and views have their own, so a view may be named like the function it runs: the prefix keeps the tools apart,
and a name stays when other entries are published. A name is a valid identifier and short enough for
the model APIs that limit tool names; a longer entry name keeps its first characters and ends in `_`
and eight hex digits of the hash of the name. A harness may rename a tool (it knows `kind` and
`entry`).

**Input.** `input_schema` is a closed object (`additionalProperties: false`) with one property per
parameter, in declaration order. A parameter with a default or an optional one is not required; a
property that is not required and has no default also accepts `null`, which means "not given". A
`many` parameter is an array of at least one value. The type decides the schema:

| Parameter type | Schema of one value |
| --- | --- |
| `string` | a string |
| `integer` | an integer within the safe range |
| `decimal` | a string with the grammar of a decimal, so that no precision is lost |
| `boolean` | a boolean |
| `date`, `datetime` | a string with `format` `date` or `date-time` and a `pattern` (a timestamp needs an offset) |
| `enum` | a string, `enum` the values of the enum as you see them; the summaries the ontology gives are in `description` |
| `ref` | a string with the grammar of a reference (a full id, an alias or trailing hex digits, never a name) and `x-graph-cli: {class}`, the class; `description` says so and, when the `lookup` tool is listed, names it with the `for` that finds the entity by name |

A `pattern` is stricter than the parser it describes: every text it accepts binds. The properties
that start with an underscore are the **controls**, the arguments that are not parameters of the
entry (a parameter name cannot start with one, so they never clash with authored names). They map to
the options of the commands:

| Control | Tool | Meaning |
| --- | --- | --- |
| `_limit`, `_offset` | function | `run --limit` and `--offset`: the rows of a page and where it starts |
| `_reason` | action | `do --reason`; required when the action demands a reason or a `require_reason` obligation of your policy applies to the writes it makes |
| `_dry_run` | action | `do --dry-run` |
| `_entity` | entity view | the reference of the entity to show (required), of the class the view takes |
| `_cursor`, `_page_bytes` | set view | `view --cursor` and `--page-bytes`: the next page and the page size |

**The `lookup` tool** finds the entity a name stands for, so that a harness can fill a `ref` input of
another tool: a reference takes an id, an alias or trailing hex digits, never a name. It is listed when
some tool you may use has a `ref` input of a class that has a name to look up (the class or a subclass
has a label attribute you see) and you hold `read.find`; without that right the references stay and
their descriptions name no tool (until a narrower lookup exists, a caller without `read.find` works with
the ids it has), and a graph in which nothing has a name has no `lookup` tool. Its input is closed like that of every tool: `text` (a
name, or a list of names; at most the number `lookup` takes), `for` and `limit` (candidates per name, as
`lookup --limit`). `for` is one of the slots the tool serves, written `<tool>.<input>`: `run_NAME.param`,
`do_NAME.param`, `view_NAME.param` or `view_NAME._entity` for the entity of an entity view; the enum lists
the `ref` inputs of every tool you may use that a lookup can serve, so a hidden class, view or function
is no slot. The tool and the descriptions of the references are rendered over every tool you may use,
whatever `--tool`, `--kind` or `--module` select, so a selection that leaves `lookup` out still keeps
descriptions that name it: add `--tool lookup` to an allow list. The tool runs `lookup --for` for that slot with the kind written out
([Names and lookup](#names-and-lookup): `function:NAME.param`, `action:NAME.param`, `view:NAME.param`), keeps
the candidates of the class the input takes and counts the others in `data.texts[].excluded`; its answer
is the answer of `lookup` and `page`, the paging, as for a function, with the `for` in its `fit` naming
the slot as the tool writes it (`run_NAME.param`). A harness passes the `id` or `alias`
of a candidate as the reference; a name with several candidates carries `facts` that tell them apart.

**Calling a tool.** `graph call TOOL` runs a tool with exactly the object its `input_schema`
describes, whatever the kind of the tool:

```sh
graph call run_<function> --input <name>=<json> --input _limit=<json> --json
graph call do_<action> --input <name>=<json> --input _dry_run=true --json
graph call view_<view> --input _entity='"<id>"' --json
graph call lookup --input text='"<value>"' --input for='"run_<function>.<param>"' --json
```

Each `--input` is one member of the object, its value a JSON value (a string is written with its
quotes), repeated for more members. Over the server the same call is `POST /v1/commands/call` with
`args.tool` and `args.input`, the object itself:
`{"args": {"tool": "run_x", "input": {"since": "2026-01-01", "titles": ["a"]}}}`. The call answers
what `run`, `do` or `view` answers for the same arguments: the same service runs, binds the
parameters by the same types, resolves references the same way, authorizes, audits and refuses the
same. What differs:

- **The tool name** is looked up among the entries you see. One you do not see is `tool.not_found`
  with the close names in `did_you_mean`; one you see but may not use gives what the command gives,
  `access.denied`, recorded like that of the command.
- **The `lookup` tool** is called like the others, with `text`, `for` and `limit`. It is there only for a
  caller that holds `read.find` while some tool takes a reference: with that right missing it is
  `access.denied`, as for `lookup`, with no reference input at all it is `tool.not_found`. A `for` that is
  none of the slots (a name without its kind prefix, a slot of a tool you cannot use) is
  `usage.invalid_argument` with the close slots in `did_you_mean`, and so is any input other than `text`,
  `for` and `limit`; a missing `text` or `for` is `usage.missing_argument`. It is a read: it takes no key.
- **`null` is "not given"** for every parameter and control that is not required: a typed client
  sends `null` for an argument it leaves out, and a default applies. For a required parameter it is
  as good as missing (`function.parameter_missing`, `action.parameter_missing` or
  `view.parameter_missing`). A number written with a fraction or an exponent (`5.0`) is an integer
  when it is a whole number, so a client that writes every number as a float still binds `integer`
  parameters and controls; for any other type, and for a number with a fraction, it is refused.
- **Unknown input is refused**: a parameter the entry does not have is `function.parameter_unknown`
  (`action.` or `view.` for those kinds), a control the tool does not have (`_reason` of a function)
  or one with a value of the wrong type is `usage.invalid_argument`, and the entity of an entity view
  is required (`usage.missing_argument`).
- **A function's `page`** is part of `data` (for `run` it is in the envelope next to it), so the
  answer is the one object `output_schema` describes. A call never writes its answer to a file, so
  `data` is never `null` on success: how much comes back is bounded by the controls of the tool
  (`_limit` for a function, `_page_bytes` for a set view), not by the output setting of the program.
  A function's next page starts at `page.next_offset`; the next page of a set view is the `next` of
  the answer as `_cursor` while `more` is true, and `view.cursor_stale` says a write landed between
  two pages: start again from the first.
- **`--expect-hash HASH`** refuses with `tool.hash_mismatch` (a conflict, not retryable) when the
  `hash` the manifest gives your tool is not HASH, before anything runs: the harness built its tool
  from an older manifest and fetches it again. A tool you may not use is refused as `access.denied`
  first, and a tool you cannot see is `tool.not_found`.
- **Idempotency.** Only an action that is not a dry run writes, and only that call takes
  `--idempotency-key` (over the server also the `Idempotency-Key` header, as for `do`); the remote
  `graph` generates a key when it gets none, so a lost answer can be repeated. A `call` and a `do`
  with the same key and the same bound parameters are one request: either replays the recorded
  result of the other (`idempotent_replay`), and other content under the key is
  `conflict.idempotency_key_reuse`. Every other call, a dry run included, is a read: it repeats
  safely and refuses a key with `usage.invalid_argument`.
- **The audit** records what the command does: the command it ran (`do`, `run` or `view`), the hash
  of the entry and `request.via`, `call` ([Audit](audit.md)); the access log names the command it
  ran as well, so the calls of a harness count like any other call of a function.

**Answers.** `output_schema` describes `data`, and the schemas stay open like every output schema of
`describe commands`: a consumer ignores keys it does not know, so an answer can grow without
breaking one that validates. A function answers what `run` answers and `page`, the paging of the
rows: `function`, `columns`, `items` and `page`, with one property per column of the function in a
row, each typed where its type is known (below) and else any JSON value marked
`x-graph-cli: {typed: false}`. An action answers the write result of `do`, as a dry run does. A
view answers the view model, with the sections that view declares (Views as tools, below).
Every schema is self-contained: no `$ref`, an object at its root.

**Views as tools.** A view tool's `output_schema` is the view model of that one view: `view` (its
`name`, `origin` and `hash`), the entity header for an entity view, `sections` and `bytes`, and only
the keys of its flavour. Each section the view declares has one schema per shape it may take, named
by its `kind` and `id`: the sections of the generated card and of an authored view (`fields`,
`relations`, `files` and its `cited_by` variant for a stored file, `affordances`, `neighbors`,
`list`), a `rows` section, the form a `rows` section takes when its function gave an `error` instead
of rows, and the stub `{kind, id, cut: true}` that a section shrinks to when an entity view is
trimmed to its limit. The rows of a `rows` section have one property per column of the function the
section runs, typed like the columns of the function's own tool (Typed columns, below): a view is
a tool only when you may run the functions of its `rows` sections. An entity view cuts long text
cells and ends them in an ellipsis (`cells_cut` counts them); no enum value, reference or date is
long enough to be cut, so only a decimal cell loses its `pattern` there, and a set view cuts nothing
and keeps it. The `hash` of a view tool (and its schema) changes when the column types of a function
its sections run change, by a functions publish or a program upgrade as for a function tool, so a
harness that passes `--expect-hash` is told to fetch the manifest again for a view as well. The
`view` command's own schema (`describe commands --output-schemas`) has the same sections for every
view, with the cells of a `rows` section any value.

**Paging a set view.** A set view tool pages by cursor (`annotations.paging` is `cursor`). The first
call leaves `_cursor` out. A page that does not reach the end has `more: true` and `next`: pass that
`next` as `_cursor` of the next call, with the same other arguments (the parameters and
`_page_bytes`), until `more` is absent. `view.cursor_stale` means a write landed between two pages
(`on_error` `restart`): start again from the first page. An entity view has no pages and takes no
`_cursor`.

**Typed columns.** A column of a function has a type when the query makes it certain (a node, an
attribute, a count, a list of values, a literal) or the maintainer declared one
([result columns](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/authoring-graph-functions/references/format.md#result-columns)).
The property of the column in a row carries its `description` (the column's summary) and the schema
of the cells; a column whose values can be null accepts `null` as well, and a list is an array.
`describe function` lists the same type per column (`type` is null for an untyped column). The cells
are what `run` shows:

| Column type | Schema of one cell |
| --- | --- |
| `string` | a string |
| `integer`, `boolean` | an integer, a boolean |
| `decimal` | a string in plain notation (`0.0000001`, `1000`: the grammar of a decimal parameter, so the cell can be passed back); a value is shown this way unless its plain text would run to thousands of characters; then it keeps the exponent notation, which the schema accepts but a decimal parameter does not |
| `date`, `datetime` | a string with `format` `date` or `date-time` |
| `enum` | a string, `enum` the values of the enum as you see them |
| `ref` | a string with the grammar of a reference, the class in `x-graph-cli: {class}` when the column has one; it can go into a `ref` parameter |
| `entity` | the entity object `get` shows, `attrs` typed by the attributes of the class that you see (an entity of a subclass may carry more) |
| `lang` | `{"lang", "value"}`, a language-tagged text |
| `any` | any JSON value |

Like the enum values, the attributes of an entity column are those of your model: what a type limit
or a marking hides is not in the schema, and a function whose result names a hidden class or enum
does not exist for you, like one that takes it as a parameter.

**Compatibility policy.** The manifest shape (`tools_format`), the tool names, the controls and the
open output schemas are a contract that a harness's allow list and stubs depend on. Within a
format a change is additive: new tools, keys, error codes and optional arguments. A change a
consumer can break on raises `tools_format`: removing or renaming a key, a tool name or a control,
tightening an input schema, or changing what an `on_error` value means. A harness sends only what
`input_schema` lists, and ignores what it does not know of an answer.

The output schema of a function tool may become more precise within a format: a column that was
untyped gets a type, or a type becomes exact, because the types of the columns are worked out from
the stored query by the program that serves the manifest and not stored with the published
function. The tool `hash` follows such a change, so it can change on a server upgrade without any
publish; a harness that passes `--expect-hash` then gets `tool.hash_mismatch` and fetches the
manifest again, as for any other change of the tool.

**Errors.** `errors` maps every code any listed tool may return to its registry facts
(`category`, `retryable`, `summary`) and `on_error`, the one move a harness makes next:

| `on_error` | Meaning |
| --- | --- |
| `retry` | the same call again, later: the code is retryable |
| `fix_input` | the arguments are wrong or name something that does not fit: correct them and call again |
| `check_state` | the arguments are fine and the graph's state decides (`action.precondition_failed`, `action.nothing_to_do`): read the details, do not repeat the call unchanged |
| `restart` | start again from the first page (`view.cursor_stale`: a write landed between two pages) |
| `refresh` | the tool definitions are out of date: fetch the manifest again (`tool.not_found` for a tool that has gone since, `tool.hash_mismatch`) |
| `stop` | nothing the harness can change helps: report it (`access.denied`, a stale entry, a graph that is archived) |

A precondition failure is `action.precondition_failed`, and `error.details.failed` lists each failing
precondition with its `precondition`, `parameter`, `entity`, `summary` and, for a count, `actual`;
the arguments were fine, so the call is not repeated unchanged: read what failed and change the
graph or the plan. A dry run (`_dry_run`) raises the same errors as the real run and writes and
audits nothing, so a harness can check a call before it makes it. Each tool lists the codes of its
command (`run`, `do` or `view`) that a call can raise, `tool.not_found`, `tool.hash_mismatch` and
the codes of the transport; the `not_found` codes of the three kinds of entry are left out, because
a call of an entry you cannot see is `tool.not_found`. A large manifest goes to a file on the command
line like any large answer ([Large answers go to a file](#large-answers-go-to-a-file)); a harness
fetches it over the server API.
