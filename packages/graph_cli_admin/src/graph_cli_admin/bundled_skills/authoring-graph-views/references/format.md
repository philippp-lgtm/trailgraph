# View file format: `graph_cli.view/1`

One file per view, `views/<name>.yaml` in the graph's workspace folder. Read with the strict YAML
loader (no anchors, no duplicate keys, no unknown keys). This page is the single reference of the
format, of what publishing checks, of what a call answers and of the bounds that shape the answer;
the guidance on writing good views is in `SKILL.md`.

A view is either an **entity view** (it has `takes`: the class of the entity it shows; called with a
reference: `graph view REF --name <view>`) or a **set view** (no `takes`; called without one: `graph
view --name <view>`). One format, one command, one answer shape (`graph_cli.view_model/1`); the
flavour decides which keys and section kinds are allowed.

## Complete files

An entity view, the default of its class:

```yaml
schema: graph_cli.view/1
name: unit_overview
module: organization
summary: A unit with its cost center, its positions in a state and who holds them.
use_when: >-
  What a department or team is about: where it sits, what it costs, which positions it has in a
  given state and who holds them.
takes: org:OrgUnit
default: true
params:
  status:
    type: string
    default: active
    summary: Lifecycle state of the positions to list.
max_bytes: 4096
sections:
  - kind: fields
    fields: [name, alt_labels]
    value_chars: 300
  - kind: relations
    relations:
      - org:charged_to
      - {relation: org:part_of, direction: in}
    max_groups: 4
  - kind: neighbors
    id: positions
    title: Positions
    relation: org:in_unit
    direction: in
    where: ["status=$status"]
    order: [title]
    fields: [title, status]
    relations:
      - {relation: org:holds, direction: in, fields: [work_email], max: 5}
    max: 20
  - kind: rows
    id: members
    function: members_of_unit
    args: {unit: $entity}
    max: 10
  - kind: affordances
examples:
  - question: Which active positions does the engineering department have?
    params: {status: active}
```

A set view with a parameter:

```yaml
schema: graph_cli.view/1
name: org_catalog
module: organization
summary: Every unit and position with their keys and relations, for catalog renderers.
use_when: >-
  Render or export the organization as a whole, one listing per kind of thing, for a script or a
  document; not for one entity (use graph view REF).
page_bytes: 65536
params:
  unit:
    type: ref
    class: org:Department
    summary: The department whose vacancies are listed.
sections:
  - kind: list
    id: units
    type: org:OrgUnit
    fields: all
    relations: all
  - kind: list
    id: positions
    type: org:Position
    order: [title]
    fields: [title, status]
    relations:
      - all
      - {relation: org:holds, direction: in, fields: [name], where: ["work_email:exists=true"]}
  - kind: rows
    id: vacancies
    function: vacant_positions
    args: {unit: $unit}
```

## Top-level keys

| Key | Required | Meaning |
| --- | --- | --- |
| `schema` | yes | exactly `graph_cli.view/1` |
| `name` | yes | `^[a-z][a-z0-9_]{1,62}$`; equals the file name without `.yaml`; unique among the views; `card` is reserved (the generated card). Views have their own namespace: a view may be named like a function or an action |
| `module` | with several modules | id or prefix of a module of the graph that is not built in; optional when the graph has one module; bare names resolve through its imports; a reader whose rights hide every term of the module does not see the view |
| `summary` | yes | one line, at most 100 characters: what the view shows |
| `use_when` | yes | at most 600 characters: the situations and questions the view answers, in users' words; what search matches a request against |
| `takes` | no | the class (CURIE or local name) an entity view shows, subclasses included; leave it out for a set view |
| `default` | no | `true` makes the view what `graph view REF` shows for entities of its class (entity views only). At most one default per class: the alphabetically first wins and a later one is `view.default_taken`; a default of a subclass does not clash with one of its superclass. A default cannot be deprecated and cannot have a required parameter (it has no `ref` parameter either: a `ref` has no default), because `graph view REF` cannot give one |
| `params` | no | mapping of parameter name to declaration; the grammar of function parameters (below) |
| `max_bytes` | no | entity views only: 1024 to 6144 (the card's hard limit); lowers the limit of the answer |
| `page_bytes` | no | set views only: 1024 to 262144; the default page size of the view (the call's `--page-bytes` wins) |
| `sections` | yes | 1 to 32 sections (below) |
| `examples` | no | list of `{question, params}`: questions a user would ask, `params` literals of declared parameters (never an entity reference); search reads the questions |
| `status` | no | `active` (default) or `deprecated`; a deprecated view still answers, with the warning `view.deprecated`, and is left out of `describe views` (unless `--include-deprecated`) and of the affordances; `describe search` still finds it and marks it deprecated |
| `replaced_by` | no | name of an active view; only with `status: deprecated`. It may name a view of the other flavour or of another class; a view cannot replace itself |

Summaries of parameters and example questions have at most 200 characters.

## Parameters

The same declaration as for functions (`type`, `summary`, `class`, `enum`, `many`, `default`); the
name matches `^[a-z][a-z0-9_]{0,31}$` and `entity` is reserved.

| Key | Meaning |
| --- | --- |
| `type` | one of `string`, `integer`, `decimal`, `boolean`, `date`, `datetime`, `enum`, `ref` |
| `summary` | what the value is (required) |
| `class` | CURIE of the class; required with `ref`, not allowed otherwise |
| `enum` | CURIE of an enum of the model; required with `enum`, not allowed otherwise. A parameter that takes a fixed set names a model enum (`enum: org:Status`); there is no inline list of values. A lifecycle state is a plain `string` |
| `many` | `true` makes the parameter a list; the caller repeats `--param name=...` |
| `default` | makes the parameter optional; a scalar of the declared type, or a list for `many`. A `ref` has no default |

Every declared parameter must be used and every `$name` must be declared (`view.parameter_unused`,
`view.parameter_undeclared`). The caller passes `--param name=value`, read by the declared type
(`view.parameter_missing`, `view.parameter_unknown`, `view.parameter_invalid`); a `ref` takes what
every command takes for an entity (id, alias, trailing hex digits) and must be of the declared class
(`view.parameter_type`).

### Where a parameter may stand

A `$name` stands for a **whole value**, never part of one:

- in a `where` entry, alone after the `=`: `status=$status`, `due_on:le=$before`;
- as the whole value of an entry of `args` of a `rows` section: `{unit: $unit}`;
- `$entity`, the entity an entity view shows, only as a value of `args` of a `rows` section of an
  entity view (a set view has no entity).

Anywhere else, and inside a text or a list, it is `view.placeholder_position`. Because of that, **a
literal cannot contain `$` followed by a lowercase name**: write it another way.

### Type fit

A parameter in a `where` entry must fit the attribute it is compared with:

| Parameter | Fits attributes of base type |
| --- | --- |
| `string` | `string`, `text`, `iri` (language-tagged text too) |
| `enum` | `string`, `text` |
| `integer` | `integer`, `decimal` |
| `decimal` | `decimal` only: a decimal does not fit an `integer` attribute |
| `boolean`, `date`, `datetime` | the same type |
| `ref` | the field `id` only (`where: ["id=$thing"]`); not `alias`, which not every entity has |

The operator decides the shape: `in` takes a `many` parameter, `contains`, `prefix` and `lang` a
`string` parameter (text operators are not checked against the attribute's type beyond that, as
`find` accepts them), `exists` takes no parameter (`attr:exists=true`). A misfit is
`view.type_mismatch`. An `enum` parameter may not feed a `string` parameter of a function.

## Sections

Every section has `kind`, `id` (`^[a-z][a-z0-9_]{0,62}$`, defaults to the kind, unique in the view,
required when a kind repeats) and `title` (optional, at most 100 characters, for renderers). A view
has at most one `files` and one `affordances` section (`view.duplicate_section`, as for a repeated
id).

| Kind | Flavour | Reads |
| --- | --- | --- |
| `fields` | entity | attributes of the entity |
| `relations` | entity | the entity's relations as groups with counts and the first neighbours (the card's overview) |
| `files` | entity | the stored files the entity was taken from, or the citations of a stored file |
| `affordances` | entity | what applies next, by name: transitions, actions, functions and the other views of the class |
| `neighbors` | entity | the neighbours over one relation as rows, filtered and ordered |
| `list` | set | every active entity of one or more classes as rows |
| `rows` | both | the result of a published function |

An entity view takes `fields`, `relations`, `files`, `affordances`, `neighbors` and `rows`; a set
view takes `list` and `rows`. The other flavour's kind is `view.flavour_mismatch`.

### `fields` (entity)

| Key | Meaning |
| --- | --- |
| `fields` | a list of attribute names (at most 32, each of the entity's class or a subclass), or `all`; left out: the card's choice (label, states, required and unique attributes). `all` is every attribute the reader sees that has a value, key fields first, never a `blob` |
| `value_chars` | strings are cut at this length (default 100, at most 2,000); `chars` on the item gives the original length; lists show five elements |
| `max` | items shown (default 8, at most 32); a list shows all its names, so `max` below the list length is `view.invalid_value`; the rest of `all` goes into `other` |

### `relations` (entity)

| Key | Meaning |
| --- | --- |
| `relations` | a list of `relation` or `{relation, direction}` (`out` default, `in`); left out: every relation, as the card does. Each relation must start (`out`) or end (`in`) at the entity's class: `view.relation_mismatch` |
| `max_groups` | groups shown (default 8, at most 8); the rest is named in `more_groups` |
| `max_items` | neighbours named per group (default 3, at most 10) |

### `files` and `affordances` (entity)

No keys beyond `id` and `title`; they read as the card does.

### `neighbors` (entity)

| Key | Meaning |
| --- | --- |
| `relation` | the relation to follow (required) |
| `direction` | `out` (default) or `in` |
| `where` | at most 8 filters of at most 200 characters each, in the `find --where` grammar (`attr[:op]=value`, `op` in `eq ne lt le gt ge contains prefix in exists lang`, all AND-ed), planned against the class at the far end; `$name` as the whole value |
| `order` | at most 8 sort keys of at most 200 characters each, in the `find --sort` grammar (`attr` or `attr:desc`); left out: by label, then id |
| `fields` | a list of attribute names (at most 64) or `all`; default none (key and label only) |
| `relations` | per-row neighbour lists (below) |
| `max` | rows shown (default 10, at most 50); the cut is counted in `more` |

### `list` (set)

| Key | Meaning |
| --- | --- |
| `type` | one class or a list of up to 16 (CURIEs or local names), subclasses included; each row names its concrete `type` when more than one class can occur |
| `where`, `order`, `fields`, `relations` | as for `neighbors`; the default order is `find`'s (by id); the per-row lists differ in two defaults (below) |

A `list` has no `max`: it is paged, never cut.

### `rows` (both)

| Key | Meaning |
| --- | --- |
| `function` | name of a published function (required) |
| `args` | mapping of function parameter to a literal, `$param` or `$entity` (at most 32). Every required parameter of the function must be bound (`view.argument_missing`), each argument must fit the parameter's type (`view.type_mismatch`), a `ref` parameter is bound by `$param` or `$entity` and never by a literal (`view.literal_reference`), `$entity` fits a `ref` parameter whose class is `takes` or a superclass of it, and a name the function does not declare is `view.unknown_argument` |
| `max` | entity views only: rows shown (default 10, at most 50); a set view pages the rows |

A deprecated function behind a `rows` section is not refused: it is a warning
(`view.function_deprecated`) on publish and on every call.

### Per-row neighbour lists

`relations` of a `list` or `neighbors` section is `all` or a list (at most 16) of `all` and mappings:

| Key | Meaning |
| --- | --- |
| `all` | every out-relation the reader sees, as neighbour keys only (once per list) |
| `relation` | a relation (required in a mapping); at most once per direction |
| `direction` | `out` (default) or `in` |
| `fields` | attributes of the neighbours (a list or `all`) |
| `where`, `order` | as above, on the neighbours; left out `order`: by label, then id, except out-lists of a `list` row, which follow the edges as `find` shows them |
| `max` | neighbours listed (default 50, at most 500; in a `list` section default 500); the cut is counted in `more` |

A `list` row replaces a `find --type` read, so its lists are not trimmed or reordered by default: the
out-lists (also those of `all`) keep the edge order of `find` unless `order` says otherwise, and `max`
defaults to the largest value. In-lists, `neighbors` rows and entity views order by label, then id and
keep the default of 50 (the byte guard trims entity views).

There is no deeper nesting: a second hop is a join by key across sections, or a function.

## What a call answers

`graph view` answers `graph_cli.view_model/1`: `view` (`name`, `origin`, and `hash` for an authored
view: the hash of the compiled entry; with the parameters, the page size, the cursor, `graph.seq` and
the reader (what it may see) it is the key of a consumer's cache), `entity` (entity views), `sections` (each with its `kind`, its `id` and
`title`), `bytes`, and for a set view `next` and `more`. Nothing is cached or stored: every call reads
the reader's own snapshot, and the same input gives the same bytes.

A row of `list`, `neighbors` and the per-row lists is lean: `ref` (the alias, else the id: the stable
key to join by), `label`, `type` when the classes mix, `record_status` when not active, `qualifiers` of
the edge, `fields` (the attribute values as `get` shows them), `relations` (groups of `relation`,
`direction`, `count`, `items`, `more`). A section has `count` and, where rows were cut, `more`.

- **`approx` and `unread`.** A count that could not read every edge (a `where` on a very large
  neighbourhood, or a spent load budget) is a lower bound and says so: `approx: true` and `unread`,
  the number of edges not read; `sampled` is the number read.
- **`cells_cut`.** A `rows` section cuts strings of a cell at 200 characters and lists at five
  elements in entity views; `cells_cut` counts the cells that were shortened.
- **`sections_cut` and stubs.** Past every other trim an entity view drops section titles, shortens
  labels, then replaces whole sections, last first, by a stub `{kind, id, cut: true}`; `sections_cut`
  counts stubs that were folded away. Only the header is never trimmed. `cut: true` says the answer
  was trimmed at all.
- **A failing section answers alone.** `{kind: rows, id, function, error: {code, message}}` and the
  warning `view.section_failed`; the rest of the view still comes and `ok` stays true. Codes of the
  function (`function.*`, `query.*`) and `view.section_unavailable` end up here; the warning comes
  with every page that runs the section.
- **`--as-of`.** The sections read the state of that point (it needs `read.history`); a `rows`
  section answers alone with `view.section_unavailable`, because a function reads the current state.

### What an agent harness gets of a view

`graph describe tools` lists each view you may use as a tool with the JSON Schema of its answer, built
from the view's own sections: one entry per declared section (by `kind` and `id`) in each shape it can
take, so the sections you declare are the contract. The rows of a `rows` section are typed by the
columns of its function, so declare `type` on the function's `returns` columns where the query alone
does not make one certain (see `authoring-graph-functions`); an untyped column is any value there. A
view is a tool only for a caller who may run the functions of its `rows` sections, so its rows are
typed like those functions' tools. An entity view cuts long text cells; only a decimal cell can be
long enough, so its schema there names no pattern; set views cut nothing.

### Bounds

| Bound | Value |
| --- | --- |
| entity view answer | at most 6,144 bytes (`max_bytes` lowers it); the guard trims in a fixed order: the attributes of `neighbors` rows and their lists, per-row lists to one item, rows of `neighbors` and `rows` to three then one, the card's own stages (key fields of relation neighbours, group items, groups, field values, fields, files), then the rows of `neighbors` and `rows` (the counts stay), titles, labels, stubs, stubs folded into `sections_cut` |
| loaded revisions of an entity view | one budget of 1,000 shared by every section; a group reads at most 500 edges |
| set view page | `--page-bytes`, else `page_bytes`, else 12,288; 1 to 262,144, else `usage.invalid_argument`; cut at whole rows; a page always holds one row (a row larger than the page goes alone with `oversize: true`; values are never cut in set views) |
| loads per page | 20,000 plus the one row that crosses the budget, so a row reads the same on every page it could be on |
| `rows` budget | each `rows` section of a view gets 100,000 divided by the number of its `rows` sections of the query budget: a function that runs under `graph run` can fail inside a view with `query.too_large` in the section |
| `rows` result | the complete result of the function, in its own order, run once per page that holds the section; give the function an `ORDER BY` so the pages concatenate |

### Paging a set view

A page that does not reach the end has `next` and `more: true`; `graph view --name <view> --cursor
<next>` with the same `--param` gives the next page (the page size may change between pages). A
section continued from the page before carries `from`, the offset of its first row. The cursor is
opaque. It names the state it was issued for and a key over the view, its parameters, the published
functions, the pinned ontology and what the reader sees:

- after a write the cursor is `view.cursor_stale` (exit 1; repeat the call without `--cursor`, or
  pass `--as-of tx:<seq>` to page the state you started with);
- after another view, other parameters, a functions publish, an ontology release, a policy change or
  a garbled cursor it is `view.cursor_invalid` (exit 2).

## What publishing checks

`graph-admin graph views publish` (also as part of `graph-admin graph push`) checks every file against
the ontology the graph is pinned to (or will be, in a push) and the functions the graph will have, and
reports all problems together as `admin.views_invalid`, with one exception: a file with a problem of
shape (the rows "the file", "flavour" and "sections" below) shows only those; its problems with terms
and functions come once the shape is fixed. Other files are checked in full. The `code` of each problem in
`details.problems`:

| Rule | Codes |
| --- | --- |
| the file | `view.invalid_yaml`, `view.unsupported_schema`, `view.unknown_key`, `view.missing_key`, `view.invalid_value` (also every limit above its maximum), `view.name_mismatch`, `view.reserved_name`, `view.invalid_file` (the folder holds anything but `<name>.yaml` files) |
| flavour | `view.flavour_mismatch`: in a set view `default`, `max_bytes`, `max` of a `rows` section and the kinds `fields`, `relations`, `files`, `affordances`, `neighbors`; in an entity view `page_bytes` and the kind `list` |
| sections | `view.duplicate_section`, `view.placeholder_position` |
| the module | `view.unknown_module`, `view.builtin_module`, `view.module_required` |
| terms | `view.unknown_class`, `view.unknown_enum`, `view.unknown_attribute`, `view.unknown_relation`, `view.ambiguous_term` (a local name two terms have), `view.relation_mismatch` |
| filters | `view.invalid_where`, `view.invalid_order` (the planner's error code is in `details.error`) |
| parameters | `view.invalid_default`, `view.invalid_example`, `view.parameter_undeclared`, `view.parameter_unused`, `view.type_mismatch` |
| functions | `view.unknown_function`, `view.unknown_argument`, `view.argument_missing`, `view.literal_reference` |
| replacement and default | `view.replaced_by_invalid` (target missing, deprecated or itself), `view.default_taken` |
| size | `view.too_many_views` (1,000 per graph) |
| warning | `view.function_deprecated`; `catalog.granted_entry_removed` and `catalog.granted_entry_changed` (one record per view that a catalog grant of the installation policy names and the publish removes or changes) |

The published form is the compiled catalog (`graph_cli.view_catalog/1`): per view the normalized
definition (CURIEs, defaults filled), the authored source, `touches` (types, relations, attributes and
functions it names, which is what hides a view from a reader who lacks one of them) and a hash.
`graph describe view <view>` shows the entry.

### Release and function checks

The invariant: the published views always resolve against the ontology the graph is pinned to and the
functions it has.

- `graph-admin ontology diff <dir> --graph <graph>` warns `admin.views_affected` with the views a
  candidate release would break.
- `graph-admin ontology release` and `graph-admin graph push` refuse with `admin.views_broken` when a
  published view would no longer resolve (a removed or renamed attribute, relation or class it names,
  a changed function). The fix is one `graph-admin graph push` with the new ontology version and the
  repaired or deleted view files together. A release that renames terms publishes again, with the
  release, the views whose sources still compile; the others are fixed in the same push.
- `graph-admin graph functions publish` refuses with `admin.views_broken` when a published view runs a
  function the new catalog no longer has, binds a parameter it no longer has, or binds one of another
  type. A function a view runs cannot be removed without the view; push both in one step.
- `graph-admin graph check <graph>` reports a published catalog that no longer resolves.

When a view that was published against another release no longer reads the same, a call that names it
refuses with `view.stale`, naming the code of the cause only ("tell the maintainer"); plain `graph view
REF` on a class whose default view is stale answers with the generated card and the warning
`view.default_stale`. A reader whose rights limit what it sees does not see a stale view at all
(`view.not_found`): the problems of the check may name terms it must not see.

## Who sees a view

A view is visible to a reader exactly when everything it names is: its `takes` and `type` classes,
every attribute, relation and enum it names, and every function a `rows` section runs.
A view the reader does not see is not found everywhere: `view.not_found` (never "denied"), absent from
`describe views`, `describe search`, the affordances and the default resolution (the default of the next
class up the chain, else the card). A view the reader sees in full but may not use, because a right is
missing (`read.get` for every view, `read.find` for a set view, `read.function` for a view that runs
functions), is not hidden: `describe view` and `describe search` show it marked `allowed: false` with
`missing`, lists count it as not allowed, and `graph view --name` of it is a recorded `access.denied`. Rows, neighbours and counts come from the reader's own snapshot:
hidden entities are not rows, edges to them are not neighbours, hidden sources are not counted, and a
masked attribute is absent from `fields: all`. `replaced_by` is shown only when the reader sees that
view.

## Calling errors

| Code | Meaning |
| --- | --- |
| `view.not_found` | no such view for this reader (`did_you_mean` over the visible names) |
| `view.takes_entity` | an entity view needs a reference |
| `view.takes_no_entity` | a set view takes no reference |
| `view.class_mismatch` | the entity is not of the class the view takes (`expected`, `found`) |
| `view.section_unavailable` | a `rows` section under `--as-of` |
| `view.cursor_invalid`, `view.cursor_stale` | see paging |
| `view.deprecated` | warning: the view is deprecated (`replaced_by` when visible) |
| `view.parameter_type` | a `ref` parameter of the wrong class |
