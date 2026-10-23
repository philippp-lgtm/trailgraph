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
| `describe types`, `describe type T`, `describe fields T`, `describe relations`, `describe lifecycle T` | the selected graph's ontology (`graph_cli.model/1`); the `graph` block names the graph's default language; every term with a summary carries `deprecated` |
| `describe schema` | the ontology as `graph query` sees it: labels with their properties, one relationship pattern per relation (`(:A)-[:rel]->(:B)`), names written as a query needs them (see [Cypher queries](#cypher-queries)) |
| `get REF` | one entity; superseded and retracted entities are shown as recorded, with a warning and the ids that replaced or withdrew them; `--rev N` shows an earlier revision |
| `history REF` | one entity's revisions, newest first: time, command, reason, whether your own session wrote it, and the names (not values) of the fields and relations that changed; paged |
| `find` | entities by `--type`, `--where`, `--rel`/`--in-rel` (`rel=<id>,<id>` matches an edge to any listed entity); sorted, paged (`--limit`, `--offset`) with a next-page hint |
| `lookup TEXT` | entities by name: main, alternative and hidden labels, case- and Unicode-insensitive (`--match exact\|prefix`, `--ignore-accents`, `--type`); ranked candidates, paged |
| `neighbors REF` | connected entities up to three hops (`--rel`, `--direction`, `--depth`) |
| `query TEXT` | a read-only Cypher query (`-` or `--file` reads it from stdin or a file, `--param name=value` binds `$name`): patterns over several hops, OR and NOT across relations, counts and grouping; rows paged like `find`, `--explain` shows the resolved query (see [Cypher queries](#cypher-queries)) |
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
literals, `+ - * / %`, comparisons, `IN`, `STARTS WITH`, `ENDS WITH`,
`CONTAINS`, `IS [NOT] NULL`; the aggregates `count` (`count(*)`,
`DISTINCT`), `collect`, `min`, `max`, `sum`, `avg` and the functions
`labels`, `type`, `keys`, `properties`, `coalesce`, `size`, `head`, `last`,
`toString`, `toInteger`, `toFloat`, `toLower`, `toUpper`, `trim`,
`substring`, `split`, `replace`, `abs`, `ceil`, `floor` and `round`. A null
argument makes a function null (`coalesce` excepted). Everything else
(write clauses, `CALL`, `UNION`, named paths, unbounded `*`,
`shortestPath`, comprehensions, map projections, subqueries, `id()`) is
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
