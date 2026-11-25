# Function file format: `graph_cli.function/1`

One file per function, `functions/<name>.yaml` in the graph's workspace
folder. Read with the strict YAML loader (no anchors, no duplicate keys,
no unknown keys). This page is the single reference of the format and of
what publishing checks; the guidance on writing good functions is in
`SKILL.md`.

## A complete file

```yaml
schema: graph_cli.function/1
name: members_of_unit
module: organization
summary: People holding a position in a unit or in any unit below it.
use_when: >-
  Who works in a department or team, including its sub-units; headcount of a unit.
params:
  unit:
    type: ref
    class: org:OrgUnit
    summary: The unit to list.
query: |
  MATCH (p:Person)-[:holds]->(q:Position)-[:in_unit]->(u:OrgUnit)-[:part_of*0..5]->(d:OrgUnit)
  WHERE d.id = $unit
  RETURN p.name AS person, q.title AS position, u.name AS unit
  ORDER BY unit, person
returns:
  - column: person
    summary: Name of the holder.
  - column: position
    summary: Title of the position held.
  - column: unit
    summary: The unit the position belongs to.
examples:
  - question: Who works in the infrastructure department, including its teams?
featured: true
```

## Top-level keys

| Key | Required | Meaning |
| --- | --- | --- |
| `schema` | yes | exactly `graph_cli.function/1` |
| `name` | yes | `^[a-z][a-z0-9_]{1,62}$`; equals the file name without `.yaml`; unique in the graph |
| `module` | with several modules | id or prefix of a module of the graph that is not built in; optional when the graph has one module; bare names resolve through its imports; a reader with read limits (a policy grant's `reads` list, see `maintaining-graphs`, `references/give_an_app_a_role.md`) sees the function only while its module has a term that reader sees; a main module that holds none of its own is no such module and does not hide it |
| `summary` | yes | one line, at most 100 characters: what the function returns |
| `use_when` | yes | at most 600 characters: the situations and questions the function answers, in users' words; what search and agents match a request against |
| `params` | no | mapping of parameter name to declaration (below); omit for none |
| `query` | yes | the read-only Cypher query, at most 2,000 characters |
| `returns` | yes | list of `{column, summary}` with an optional type ([result columns](#result-columns)), exactly the query's result columns in order |
| `examples` | yes | at least one `{question, params}`; `params` optional |
| `featured` | no | `true` lists the function in the graph skill (at most 12 are listed); default `false`. The only key that changes the skill: publishing a function that is not featured leaves installed skills current |
| `status` | no | `active` (default) or `deprecated` |
| `replaced_by` | no | name of an active function; only with `status: deprecated` |

Summaries of parameters and columns and example questions have at most 200
characters. A function cannot replace itself.

## Parameters

Each entry of `params` is `name: {type, summary, ...}`; the name matches
`^[a-z][a-z0-9_]{0,31}$`. Keys of a declaration:

| Key | Meaning |
| --- | --- |
| `type` | one of `string`, `integer`, `decimal`, `boolean`, `date`, `datetime`, `enum`, `ref` |
| `summary` | what the value is (required) |
| `class` | CURIE of the class; required with `ref`, not allowed otherwise. The entity must be of this class or a subclass |
| `enum` | CURIE of an enum of the model; required with `enum`, not allowed otherwise. The value is checked against its values |
| `many` | `true` makes the parameter a list; the caller repeats `--param name=...` |
| `default` | makes the parameter optional; a scalar of the declared type, or a list for `many`. A `ref` has no default |

- A value is read by the declared type and never guessed from its look.
- A `ref` takes what every command takes for an entity (full id, alias,
  or 8 to 32 trailing hex digits), never a name. The entity must exist and
  be of the declared class; the query receives its canonical id. Compare a
  `ref` only with `id` or `alias` (`WHERE d.id = $unit`), or use it as a
  pattern property of those.
- Examples carry literals of declared parameters only, never a `ref`: an
  entity id goes stale.
- Every `$name` in the query must be declared and every declared parameter
  must be used. A parameter may not appear in `SKIP` or `LIMIT`: callers
  page with `--limit` and `--offset`.

### Type fit

A parameter compared with a field (`=`, `<>`, `<`, `<=`, `>`, `>=`, `IN`,
`STARTS WITH`, `ENDS WITH`, `CONTAINS`) must fit the field's datatype:

| Parameter | Fits fields of base type |
| --- | --- |
| `string` | `string`, `text`, `iri` |
| `enum` | `string`, `text` |
| `integer`, `decimal` | `integer`, `decimal` |
| `boolean` | `boolean` |
| `date` | `date` |
| `datetime` | `datetime` |
| `ref` | `id` or `alias` only |

A `many` parameter goes with `IN` (`field IN $p`); a single parameter with
a list field goes the other way (`$p IN field`); a single parameter is not
compared with a list field by `=`. The text operators take a single
`string` parameter and a text field.

## Result columns

Each entry of `returns` is `{column, summary}` plus, optionally, a type. The column name is the
name of the result column (`AS name`, or the expression as written). Keys of a column:

| Key | Meaning |
| --- | --- |
| `column` | the name of the result column (required) |
| `summary` | what the column holds (required) |
| `type` | one of `string`, `integer`, `decimal`, `boolean`, `date`, `datetime`, `enum`, `ref`, `entity`, `lang`, `any` |
| `class` | CURIE of a class; only with `ref` or `entity`, optional there (the class of the entity the column holds, left out when it can be of any class) |
| `enum` | CURIE of an enum of the model; required with `enum`, not allowed otherwise |
| `many` | `true` makes the column a list of the type |

The type tells an agent harness what a cell looks like, so it can check and use the answer
without guessing: a `decimal` and a `date` or `datetime` come as text, an `enum` as one of the
values of its enum, a `ref` as an entity id or alias that can go into a `ref` parameter, an
`entity` as the entity object `graph get` shows, a `lang` as `{"lang", "value"}` and `any` as any
JSON value. A value may always be `null` unless the type is inferred as never null (below).

Most columns need no `type`: the type is inferred where the query makes it certain, and only
there:

| Column expression | Type |
| --- | --- |
| `RETURN v` of a node variable bound by a `MATCH` pattern | `entity` of the class its label names (no class for a node without exactly one label); never null for a `MATCH`, nullable for an `OPTIONAL MATCH` |
| `RETURN v.attribute` | the attribute's type, a list when the attribute is `many`, `lang` for language-tagged text, `enum` of its enum; nullable (a marking can hide an attribute from a reader). Left open when the classes the variable can have declare the attribute differently |
| `RETURN r.qualifier` of a one-hop relationship variable of a `MATCH` | the qualifier's type, nullable |
| `RETURN v.id`, `v.alias` | `ref` of the label's class (`alias` can be null) |
| `RETURN v.rev`, `v.tx_seq` | `integer`; `v.created_at`, `v.recorded_at` are `datetime`; `v.type`, `v.record_status`, `v.tx_id`, `v.content_hash`, `v.ontology_version` are `string` |
| `count(...)` | `integer`, never null |
| `collect(x)` | a list of what `x` is (a list of `any` when `x` is open or a list itself), never null |
| a literal | its type, never null |

An `OPTIONAL MATCH` never narrows a variable bound before it, since it keeps the rows when its
pattern fails: such a variable keeps the type, class and nullability it had (one that was a value
from `WITH ... AS` or `UNWIND` stays untyped), and only a name the clause binds for the first time
is typed, as nullable. A `MATCH` filters, so it narrows.

Everything else is not typed by the query, so declare it when you want it typed: `sum` and
`avg`, `min` and `max`, `coalesce`, `CASE`, arithmetic, `size()` and the other functions,
relationships, the validity fields `valid_from` and `valid_to`, and every value that went
through `WITH ... AS` (a node passed on as `WITH v` keeps its type). A declaration may not
contradict an inferred type (`function.returns_type_mismatch` names both; state the same type or
drop the `type`); a declared `any` is accepted as long as `many` matches what the query makes
certain; where the query names no class, a declared `class` is kept.

```yaml
returns:
  - column: level
    summary: Number of steps above the given position.
    type: integer
  - column: owners
    summary: The owners, as references.
    type: ref
    class: org:Person
    many: true
```

## What publishing checks

`graph-admin graph functions publish` (also as part of `graph-admin graph push`) checks
every file against the ontology the graph is pinned to (or will be, in a
push) and reports all problems together as `admin.functions_invalid`. The
`code` of each problem in `details.problems`:

| Rule | Codes |
| --- | --- |
| the file | `function.invalid_yaml`, `function.unsupported_schema`, `function.unknown_key`, `function.missing_key`, `function.invalid_value`, `function.name_mismatch`, `function.invalid_file` (the folder holds anything but `<name>.yaml` files) |
| the module | `function.unknown_module`, `function.builtin_module`, `function.module_required` |
| parameters | `function.unknown_class`, `function.unknown_enum` (also for the `class` and `enum` of a declared column type), `function.invalid_default`, `function.invalid_example`, `function.parameter_undeclared`, `function.parameter_unused`, `function.parameter_paging`, `function.type_mismatch` |
| the query | its own errors (`query.syntax`, `query.unbound_variable`, `term.unknown`, `query.ambiguous_term`, ...) and `function.pattern_impossible`: a relationship pattern whose ends no domain and range of the relation allow (it would match nothing); `details.connecting` shows what connects the types |
| the result | `function.returns_star`, `function.returns_mismatch`, `function.returns_type_mismatch` (a declared column type contradicts what the query makes certain) |
| replacement | `function.replaced_by_invalid` (target missing, deprecated or itself) |
| the namespace | `function.name_taken` (the name is a published action of the graph; functions and actions share one namespace) |
| size | `function.query_too_long` (2,000 characters), `function.too_many_functions` (1,000 per graph) |

Warned, not refused: `admin.function_untyped_columns`, one record per publish (also on a dry run and
in a push that publishes functions). It lists, up to 20, the active functions with a column that is
neither inferred nor declared (`details.functions`: `function`, `columns`; `details.more` counts the
rest). Agents get such a column as any JSON value; declare its `type`.

Warned, not refused: `admin.function_single_ref`, one record per publish (also on a dry run, when
nothing changes and in a push that publishes functions). It lists, up to 20, the active functions
(`details.functions`: `function`, `parameter`, `class`; `details.more` counts the rest) with a
single-value `ref` parameter that the stored query only compares with `=` or an inline map entry
(a parameter that is also returned, ordered or compared another way is left out). A `many: true`
parameter and `IN` would serve a batch in one run. Left out: deprecated functions, the functions
that a published view runs (`touches.functions` of the view catalog; of the pushed one in a push,
a view hands them one entity), functions with more than one single-value `ref` (a question about
a pair; lists would mean a cross product) and functions whose query has a `LIMIT` (a pick per
entity cannot be batched). `graph-admin usage report` shows which functions agents run often.

Not checked: enum literals and `labels()`/`type()` string literals inside
the query text; a mistake there returns no rows instead of an error.

The query is stored resolved: names become prefixed CURIEs, so a published
function reads the same after a later module reuses a local name. Publishing
reads that stored form back before it accepts the function, so a published
function always runs: `function.round_trip_failed` means the stored form
does not resolve to the same query, a defect of graph_cli (write the query
another way and report it; `details.normalized` holds the stored form). Every
ontology release resolves the stored functions again against the new model
(`admin.functions_broken` when one fails). A release that renames terms
publishes again, with the release, the functions whose sources still compile
(bare class and relation names with a `module:` that exists in both versions);
those that name an old CURIE or a renamed attribute are fixed in the same
`graph-admin graph push`, as is a function without `module:` when the release splits the
graph into several modules ([migrations](../../ontology-best-practices/references/migrations.md#functions-and-actions)).

## Published form

Publishing stores the files and a compiled catalog
(`graph_cli.functions/1`) that the app reads: per function the
declaration, the normalized query text, the columns, `touches` (the types
and relations the query names, with their modules), the field uses and a
hash of the entry. A column keeps the type its author declared and nothing
else: inferred types are not stored, so a catalog of functions without
declared types keeps its bytes (and entry hashes), and the types are
inferred again from the stored query whenever they are shown. Agents see
the entry with `graph describe function <function>`, which lists the type of
every column (`type` is `null` for an untyped one), and its output schema in
the tool manifest (`graph describe tools`), rendered for the model the caller
sees.
