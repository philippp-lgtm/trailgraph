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
JSON document with `--json` (or `GRAPH_CLI_OUTPUT=json`). Exit codes follow
the error category (0 ok, 1 refused or invalid, 2 usage, 3 denied, 4
conflict, 5 unavailable, 6 internal error).

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
(16384 bytes by default; see [Configuration](configuration.md#configtoml))
is written whole, pretty-printed for line-wise reading, to a new file, and
stdout gets the same envelope with `data: null` (left out of the short
envelope), the warning
`output.spilled {path, bytes, limit}` and, on success, a first hint to ask
for less or to read only the needed part of the file with the agent's file
read or search tool. The hint names no command: a shell program such as
`jq` is what restricted agent harnesses refuse. `ok`,
`error`, `page`, the exit code and the other hints and warnings stay as
they were, so paging still reads from stdout.

The file is `<dir>/<graph>/<command>_<UTC time>_<8 hex>.json` (without a
graph directly in `<dir>`), mode 0600. `<dir>` is `[output] spill_dir`,
else `/tmp/graph_cli_<uid>`: a directory per user, because a fixed name in
the shared `/tmp` could be created by another user first. The directory
must be the user's own and closed to group and others (`graph` creates it
with mode 0700); otherwise, or when the file cannot be written, the answer
is printed whole with the warning `output.spill_failed {bytes, limit,
reason}`. Each spill removes files older than 24 hours from the same
directory. Text output never spills. `GRAPH_CLI_SPILL_BYTES=0` (or
`spill_bytes = 0`) keeps every answer on stdout, for scripts.

## Commands

| Command | What it does |
| --- | --- |
| `whoami` | the acting identity (principal, agent, session, project and, over the server, the roles of the token) and what the policy allows: the project scope and, per graph, the level, the actions a deny rule withholds below it, read-only types, write obligations and, for a caller whose level is below `write`, `writes`: what a write allow list lets it write per type and verb (see [Write allow lists](access_control.md#write-allow-lists)) and, for a caller limited to some types, `reads`: the types it sees (see [Read limits](access_control.md#read-limits)) and, for a graph whose ontology marks fields that the caller does not all hold, `markings`: the markings it holds (`[]` for none; see [Markings](access_control.md#markings)); `writes` names no field the caller cannot see |
| `graphs` | the graphs this project may see and use, with the title that says what each is for and the default language of their values (BCP 47, null when unset) |
| `describe commands` | every command as data (`graph_cli.inventory/1`): arguments, options, examples, error codes, exit codes |
| `describe types`, `describe type T`, `describe fields T`, `describe relations`, `describe lifecycle T` | the selected graph's ontology (`graph_cli.model/1`); the `graph` block names the graph's default language; every term with a summary carries `deprecated`; `describe types` and `describe relations` take `--module M` (id or prefix) and then list only the terms of that module: its types, the relations it declares or has a class at one end of; the answer names the module in `module` |
| `describe modules` | the subject areas of the ontology, one item per module: id, prefix, version, summary, `builtin`, the number of types and relations, and `links` (the modules it connects to, with the number of relations in each direction); the first call on a graph with several modules. When the graph has functions you may read, each module also has a `functions` count of its active functions; likewise an `actions` count when you may run actions |
| `describe module M` | one module (id or prefix): its types, the relations that stay inside it, `links_out` and `links_in` (relations to and from other modules, as `from -rel-> to`, with the module that declares the relation in `module` and the modules at its other ends in `other_modules`) and the rules on its types; an unknown module is `term.unknown` with `did_you_mean`; `describe type` names the owning module in `module`. The text form is made for agents to read and is less than half the size of `--json` (it leaves out cardinalities and the summaries of links, which `describe type` gives); the skills point agents to it. When the graph has functions you may read, a `functions` section lists the module's active functions (`name`, `summary`, `signature`; text: `Functions:`), likewise an `actions` section (text: `Actions:`) when you may run actions |
| `describe search TEXT` | find terms of the ontology when you only know a word: types, relations, fields (under the class that declares them), enum values and lifecycle states, by name, CURIE, summary and description; reads the model, never graph data (that is `search`). Ignores case, accents and punctuation; best first: whole name, name prefix, word of the name, name contains, summary, description. Each hit has `kind` (`type`, `relation`, `attribute`, `enum_value`, `state`, `function`, `action`; a function's or action's `curie` is its name, see [Functions](#functions) and [Actions](#actions)), `curie` (a field, value or state is its owner's CURIE, a dot and its name), `of` (the owner), `module`, `summary`, `deprecated` and `match` (`field`, `how`). `--kind` (repeatable) and `--module M` narrow it; paged like `search` (`--limit`, `--offset`, next-page hint) |
| `describe functions [--module M] [--include-deprecated]` | the named functions of the graph (`graph_cli.model/1`), one item per function: `name`, `module`, `summary`, `signature` (`unit: ref org:OrgUnit, include_vacant?: boolean`; `?` marks an optional parameter) and `status`; deprecated functions only with `--include-deprecated`. See [Functions](#functions) |
| `describe actions [--module M] [--include-deprecated]` | the actions of the graph that you may run (`graph_cli.model/1`), one item per action: `name`, `module`, `summary`, `signature` and `status`; deprecated actions only with `--include-deprecated`; an action whose write rights you lack does not exist for you. See [Actions](#actions) |
| `describe action NAME` | one action completely: summary, `use_when`, `reason`, parameters, preconditions, the compiled operations, the types and relations it touches, the write rights it needs (`needs`), examples and its hash; an unknown or hidden name is `action.not_found` with `did_you_mean`. See [Actions](#actions) |
| `describe function NAME` | one function completely: summary, `use_when`, parameters, the result columns, examples, the types and relations its query touches, the normalized query text (copy it as the start of an ad-hoc query) and its hash; an unknown name is `function.not_found` with `did_you_mean` |
| `describe schema` | the ontology as `graph query` sees it: labels with their properties, one relationship pattern per relation (`(:A)-[:rel]->(:B)`), names written as a query needs them (see [Cypher queries](#cypher-queries)); `--compact` leaves out the properties; `--module M` limits it to the module's relations and labels plus the labels at the other end of its links |
| `get REF` | one entity; superseded and retracted entities are shown as recorded, with a warning and the ids that replaced or withdrew them; `--rev N` shows an earlier revision; `--fields` keeps only named parts and `--full` adds the system fields (see [Fewer fields](#fewer-fields)); `--affordances` adds what applies next (see [What an entity can do next](#what-an-entity-can-do-next)) |
| `history REF` | one entity's revisions, newest first: time, command (and, for a write by `do`, the `action` with its `name` and `hash`), reason, whether your own session wrote it, and the names (not values) of the fields and relations that changed; paged |
| `find` | entities by `--type`, `--where`, `--rel`/`--in-rel` (`rel=<id>,<id>` matches an edge to any listed entity); sorted, paged (`--limit`, `--offset`) with a next-page hint; `--fields` and `--full` as for `get` |
| `lookup TEXT...` | entities by name: main, alternative and hidden labels, case- and Unicode-insensitive (`--match exact\|prefix`, `--ignore-accents`, `--type`, `--for`, `--for-source`); up to 50 names in one call; ranked candidates, paged per name (see [Names and lookup](#names-and-lookup)) |
| `search TEXT` | entities whose names or text fields match: whole value, inside a word, every word, or a close spelling (`--type`, `--field`); ranked suggestions, paged (see [Search](#search)) |
| `neighbors REF` | connected entities up to three hops (`--rel`, `--direction`, `--depth`); `--fields` and `--full` as for `get` |
| `query TEXT` | a read-only Cypher query (`-` or `--file` reads it from stdin or a file, `--param name=value` binds `$name`): patterns over several hops, OR and NOT across relations, counts and grouping; rows paged like `find`, `--explain` shows the resolved query (see [Cypher queries](#cypher-queries)) |
| `run NAME` | run a named function of the graph (`--param name=value`, once per value for a list parameter; `--limit`, `--offset`): the parameters are read by the types the function declares, the answer is `data.function` (`name`, `hash`), `data.columns` and `data.items` with a `page`, like `query`. See [Functions](#functions) |
| `create TYPE`, `update REF`, `link REF REL TARGET`, `unlink REF ...` | single-op writes; `--set attr=value` is converted by the attribute's datatype, `--set-json attr=<json>` is typed; `update --add`/`--remove` change a list attribute (see [References, filters and writes](#references-filters-and-writes)) |
| `transition REF STATE`, `supersede BY TARGET...`, `retract REF` | lifecycle and provenance; `supersede` and `retract` need `--reason`; over a relation with `on_target_inactive: refuse` they are refused while active entities still point at the target (`data.referential.target_referenced` lists them; relink or retract them in the same change set) |
| `do NAME` | run a named action of the graph: a reviewed write the maintainer published (`--param name=value`, once per value for a list parameter; `--dry-run`, `--reason`, `--idempotency-key`, `--graph`). The parameters are read by the types the action declares, a reference takes an id, alias or trailing hex digits, never a name. The service checks the action's preconditions and expands it in the state it finds under the graph lock, then writes one transaction with one audit intent that names the action; `--dry-run` shows every entity it would write. The answer is the write result plus `data.action` (`name`, `hash`). A retry with the same `--idempotency-key` and parameters returns the recorded result; the caller needs the write rights of every op it expands to, and an action it may not run is `action.not_found`. See [Actions](#actions) |
| `apply FILE` | change set documents (one JSON document or JSONL; `-` reads stdin); `--summary` leaves out the entities of each result and keeps `entity_count` and the findings |
| `file add PATH` | store a file as `files:File` (or `--type` subclass) with its origin; known content is returned, not stored twice (see [Files](files.md)) |
| `file path REF` | the read-only path of a stored file; `--origin` the path it came from on this machine |
| `file check [REF...]` | compare origins with the stored content: `same`, `changed`, `missing`, `root_unknown`, `no_origin` |
| `skills emit --out DIR` | write skill 1 and, with `--graph`, published graph skills into a directory (one directory per skill, no marker) |
| `skills install --scope user\|project` | install or refresh the agent skills (see [Skills](skills.md#installing-skills)) |
| `skills check --scope user\|project` | compare installed skills with what this program installs now; exit 1 unless all are fresh |
| `self update` | in the remote mode install the server's build of `graph` when it differs, then refresh the agent skills of the project (`--dir`) and the user, adding the skills of the project's graphs; `--check` only reports, `--force` replaces skills edited by hand (see [Self update](server.md#self-update)) |
| `connect ENDPOINT --code CODE` | set up this machine and a folder (`--dir`) for a server from a one-time enrollment code: the project's token, the remote mode in `config.toml`, a pointer, the program and the project's graph skills; `--label` labels the token, `--force` replaces another endpoint or a stored token (see [Enrollment](server.md#enrollment)) |

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
Types and relations in read arguments (`find`, `lookup`, `search`,
`neighbors`, `describe`) are CURIEs or, as in `graph query`, the local name
when exactly one CURIE has it (`Topic` for `ex:Topic`); an ambiguous local
name is `query.ambiguous_term` with the candidates. Writes and change set
documents take the CURIE: what is recorded stays explicit.
Writes take `--dry-run`, `--reason`, `--idempotency-key` and, on
single-entity commands, `--expect REV`. `find` filters are flags without
shell metacharacters: `--where attr=value` or `--where attr:op=value` with
`op` in `eq ne lt le gt ge contains prefix in exists lang`, all AND-ed. `apply`
reads and checks every document first (broken input writes nothing), then
commits one transaction per document in order and stops at the first
failure; `data.results` reports each processed document and the exit code is
that of the failure. Its idempotency key is `idempotency_key` in each
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
finding (also in `graph check`) carries `connecting`, computed from the
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
again), and the exit code is 0. Expectations (`--expect`, `expect_seq`)
and errors are checked as for any write.

### Earlier revisions under current names

A major ontology release can rename classes, attributes and relations
(`renamed_from`) and migrate the data to the new names (see
[graph-admin: Releasing with a data migration](cli_graph_admin.md#releasing-with-a-data-migration)). Stored
revisions keep the names they were written with; reads show them under the
names of the pinned ontology instead, so an answer never holds a name the
model does not know:

- `get --rev`, the revisions behind `history` (its `changed` names) and every
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
stored. It holds `record_status` and four groups, each cut at 40 entries
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

A superseded or retracted entity has `record_status` and empty groups.
Actions and functions are listed as `describe actions` and `describe
functions` list them (what the caller may not see stays out, nothing is
recorded); transitions and relations are model-level and not filtered by the
caller's write rights, which still apply when writing. `--affordances` with
`--rev` is `usage.invalid_argument`: a past revision has no next step.

## Names and lookup

Names come from label roles in the ontology: an attribute of type `string`
or `lang_string` (or a datatype over one) may carry `role: label` (the main
name, single or one per language, at most one per class), `alt_label` (synonyms, abbreviations) or
`hidden_label` (searchable, never shown), as in SKOS. `describe` shows the
role of each field and the JSON Schema export annotates it as
`x-graph_cli-role`. `lookup` matches normalized text (`kernel.text`: NFKC,
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
or the action, otherwise the name is `function.not_found`); `--for-source <relation>` keeps the types
of its domain. Both options may be given and narrow `--type` further; when
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
- `similar`: the same words but one, and that one a typo of the other (at
  most one edit from 5 letters, two from 10). A changed number never
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

The subset: `MATCH` (several patterns and clauses), `OPTIONAL MATCH`,
`WHERE`, `WITH` (with `WHERE`), `UNWIND`, `RETURN` (`AS`, `DISTINCT`, `*`),
`ORDER BY`, `SKIP`, `LIMIT`; node and relationship patterns with labels
(`:A|B`, `:A:B`), property maps, both directions, type alternatives and
bounded variable length up to five hops (`*1..3`); pattern predicates
(`WHERE NOT (n)-[:r]->()`), label predicates (`n:A`), `CASE`, list and map
literals, the list predicates `all`, `any`, `none` and `single`
(`any(x IN n.tags WHERE x STARTS WITH 'a')`), list comprehensions
(`[x IN list WHERE ... | ...]`), list indexing and slicing (`l[0]`, `l[-1]`,
`l[1..3]`), `+ - * / %`, comparisons, `IN`, `STARTS WITH`, `ENDS WITH`,
`CONTAINS`, `IS [NOT] NULL`; the aggregates `count` (`count(*)`,
`DISTINCT`), `collect`, `min`, `max`, `sum`, `avg` and the functions
`labels`, `type`, `keys`, `properties`, `coalesce`, `size`, `head`, `last`,
`toString`, `toInteger`, `toFloat`, `toLower`, `toUpper`, `trim`,
`substring`, `split`, `replace`, `abs`, `ceil`, `floor` and `round`. A null
argument makes a function null (`coalesce` excepted). Everything else
(write clauses, `CALL`, `UNION`, named paths, unbounded `*`,
`shortestPath`, pattern comprehensions, `reduce`, map projections,
subqueries, `id()`) is
`query.unsupported` with the construct and, where one exists, the
alternative.

Semantics follow openCypher: three-valued null logic, a missing property is
null, comparisons across types are null, a relationship is used once per
`MATCH`. Where graph_cli differs:

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

Work is bounded: more than 100 000 candidate nodes, edges or rows end the
query with `query.too_large`, naming the clause; add labels, a property map
or a `LIMIT`. Errors carry `line` and `column`: `query.syntax` (exit 2, with
what was found and expected), `query.unbound_variable` and
`query.parameter_missing` (exit 2), `query.unsupported`,
`query.ambiguous_term`, `query.too_large` and `query.invalid_value` (exit
1).

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
- `describe modules` and `describe module M` list the functions per module.
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

Functions are shown only to callers who may `read.function` on the graph
(see [Access control](access_control.md#functions)); for everyone else the
graph reads as if it had none. A graph that has no functions answers every
`describe` command exactly as before.

Maintainers write functions as one YAML file each and publish them with
`graph-admin` ([`graph functions publish`](cli_graph_admin.md#commands)). The
file format, the types of parameters and the checks publishing applies are
described once, in the format reference of the `authoring-graph-functions`
admin skill ([`references/format.md`](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/authoring-graph-functions/references/format.md)); the
[generated graph skill](skills.md#blocks) lists a graph's featured
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
  accepts by name.

Run one with `do NAME --param name=value ...`, with `--dry-run` first:

- Each `--param` is read by the type the action declares, as for
  [functions](#functions) (a list parameter repeats its name; a parameter
  may be optional). A `ref` parameter takes a full id, alias or 8-32
  trailing hex digits, never a name, and the entity must exist and be of
  the declared class or a subclass (`entity.not_found`,
  `action.parameter_type`). A missing, unknown or malformed parameter is a
  usage error (exit 2: `action.parameter_missing`,
  `action.parameter_unknown`, `action.parameter_invalid`).
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
expands to is allowed to you, and an action whose writes you may not make
is not shown and is `action.not_found` (see
[Access control](access_control.md#actions)). A graph that has no actions
(or none you may run) answers every `describe` command exactly as before.
When no action fits, use the primitives.

Maintainers write actions as one YAML file each and publish them with
`graph-admin` ([`graph actions publish`](cli_graph_admin.md#commands)). The
file format, the checks publishing applies and the rules for preconditions
are described once, in the format reference of the `authoring-graph-actions`
admin skill ([`references/format.md`](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/authoring-graph-actions/references/format.md)); the
[generated graph skill](skills.md#blocks) lists the featured actions you may
run.
