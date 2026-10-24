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
The same data comes as JSON from `graph describe commands --json`.

## Output and exit codes

Both CLIs print the `graph_cli.envelope/1` envelope: text by default, one
JSON document with `--json` (or `GRAPH_CLI_OUTPUT=json`). Exit codes follow
the error category (0 ok, 1 refused or invalid, 2 usage, 3 denied, 4
conflict, 5 unavailable, 6 internal error).

### Large answers go to a file

Agent harnesses cut long tool output or put all of it into the agent's
context. So a `graph` JSON envelope larger than `[output] spill_bytes`
(16384 bytes by default; see [Configuration](configuration.md#configtoml))
is written whole, pretty-printed for line-wise reading, to a new file, and
stdout gets the same envelope with `data: null`, the warning
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
| `whoami` | the acting identity (principal, agent, session, project) and what the policy allows: the project scope and, per graph, the level, the actions a deny rule withholds below it, read-only types and write obligations |
| `graphs` | the graphs this project may see and use, with the title that says what each is for and the default language of their values (BCP 47, null when unset) |
| `describe commands` | every command as data (`graph_cli.inventory/1`): arguments, options, examples, error codes, exit codes |
| `describe types`, `describe type T`, `describe fields T`, `describe relations`, `describe lifecycle T` | the selected graph's ontology (`graph_cli.model/1`); the `graph` block names the graph's default language; every term with a summary carries `deprecated` |
| `describe schema` | the ontology as `graph query` sees it: labels with their properties, one relationship pattern per relation (`(:A)-[:rel]->(:B)`), names written as a query needs them (see [Cypher queries](#cypher-queries)); `--compact` leaves out the properties |
| `get REF` | one entity; superseded and retracted entities are shown as recorded, with a warning and the ids that replaced or withdrew them; `--rev N` shows an earlier revision; `--fields` keeps only named parts (see [Fewer fields](#fewer-fields)) |
| `history REF` | one entity's revisions, newest first: time, command, reason, whether your own session wrote it, and the names (not values) of the fields and relations that changed; paged |
| `find` | entities by `--type`, `--where`, `--rel`/`--in-rel` (`rel=<id>,<id>` matches an edge to any listed entity); sorted, paged (`--limit`, `--offset`) with a next-page hint; `--fields` as for `get` |
| `lookup TEXT...` | entities by name: main, alternative and hidden labels, case- and Unicode-insensitive (`--match exact\|prefix`, `--ignore-accents`, `--type`); up to 50 names in one call; ranked candidates, paged per name (see [Names and lookup](#names-and-lookup)) |
| `search TEXT` | entities whose names or text fields match: whole value, inside a word, every word, or a close spelling (`--type`, `--field`); ranked suggestions, paged (see [Search](#search)) |
| `neighbors REF` | connected entities up to three hops (`--rel`, `--direction`, `--depth`); `--fields` as for `get` |
| `query TEXT` | a read-only Cypher query (`-` or `--file` reads it from stdin or a file, `--param name=value` binds `$name`): patterns over several hops, OR and NOT across relations, counts and grouping; rows paged like `find`, `--explain` shows the resolved query (see [Cypher queries](#cypher-queries)) |
| `create TYPE`, `update REF`, `link REF REL TARGET`, `unlink REF ...` | single-op writes; `--set attr=value` is converted by the attribute's datatype, `--set-json attr=<json>` is typed; `update --add`/`--remove` change a list attribute (see [References, filters and writes](#references-filters-and-writes)) |
| `transition REF STATE`, `supersede BY TARGET...`, `retract REF` | lifecycle and provenance; `supersede` and `retract` need `--reason` |
| `apply FILE` | change set documents (one JSON document or JSONL; `-` reads stdin); `--summary` leaves out the entities of each result and keeps `entity_count` and the findings |
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
Types and relations in read arguments (`find`, `lookup`, `search`,
`neighbors`, `describe`) are CURIEs or, as in `graph query`, the local name
when exactly one CURIE has it (`Topic` for `ex:Topic`); an ambiguous local
name is `query.ambiguous_term` with the candidates. Writes and change set
documents take the CURIE: what is recorded stays explicit.
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
ontology_version}` (the version the state was written under). A
next-page command repeats `--as-of tx:<seq>`, also after a time, so it
reads the same state. A seq after the last transaction or a time without
an offset is `usage.invalid_argument`. An earlier state is history, so
`--as-of` also needs `read.history` (see
[Access control: History](access_control.md#history)).

Every write is a change set that commits completely or not at all;
validation findings, refusals and dry runs are described in
[Architecture: write pipeline](architecture.md#write-pipeline).

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

## Fewer fields

`get`, `find` and `neighbors` show every part of an entity by default.
`--fields` (repeatable, comma-separated) keeps only `id`, `alias`, `type`,
`rev` and `record_status` (always: `rev` is what `--expect` needs), `attrs`
with the named attributes (null when an entity has no value), and the
named entity parts: `edges` or a system field such as `tx_seq` or
`recorded_at`. A name no type in scope has is `term.unknown`. A next-page
command repeats `--fields`. `query ... RETURN` is the projection of
`graph query`; `lookup` and `search` answers are compact already.

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
are `term.no_labels`. When nothing starts with the text, the hint is a
`search`.

`lookup` takes up to 50 names, one per argument: a name with spaces is
quoted as one shell argument (`graph lookup 'Example name' EX`); `graph`
splits nothing itself, since names hold spaces and commas. Names with the
same normalized form are looked up once. Every item carries the `text` it
matched, items are in the order of the names and then ranked, and
`data.texts[]` gives `{text, total, exact}` per name, also for one without
a match. `--limit` and `--offset` apply per name; `page.total` and
`page.shown` are sums, and the next-page command repeats every name. Names
without an exact match get one `--match prefix` hint; with `--match prefix`,
names without a match get a `search` hint each (at most five).

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
field whose type is `string` or `text` (or a datatype over them), each
value of a list, on every type or the `--type` families, and `--field`
limits it to named fields. Enum values, lifecycle states, files and edge
values are not searched. Text is compared on a looser key than `lookup`
uses: case, accents and every character that is not a letter or digit are
ignored, so "Light-Refraction" equals "light refraction". A value matches

- `exact`: it equals the text;
- `phrase`: it contains the text, also inside a longer word;
- `words`: it contains each word of the text, in any order;
- `similar` (names and `string` fields only, not `text`): at least 70% of
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

Work is bounded: more than 100 000 candidate nodes, edges or rows end the
query with `query.too_large`, naming the clause; add labels, a property map
or a `LIMIT`. Errors carry `line` and `column`: `query.syntax` (exit 2, with
what was found and expected), `query.unbound_variable` and
`query.parameter_missing` (exit 2), `query.unsupported`,
`query.ambiguous_term`, `query.too_large` and `query.invalid_value` (exit
1).
