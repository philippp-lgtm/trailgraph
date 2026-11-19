---
name: authoring-graph-views
description: Write, check and publish the views of a graph_cli graph - one YAML file per view (format graph_cli.view/1) that composes sections (fields, relations, neighbours, type listings, published functions) into one bounded answer, either for one entity (an entity view) or for a whole set (a set view with paging), found with graph describe views and called with graph view. Use when agents walk many commands to learn about one kind of entity, when a script or renderer reads a whole graph type by type, when a view is refused on publish or blocks an ontology release, or when views are added, changed or deprecated. Not for one recurring query (authoring-graph-functions), not for the text of the graph skill (authoring-graph-skills) and not for the workspace workflow (maintaining-graphs).
---

# Authoring graph views

A view is a curated, named composition of reads with a fixed answer shape. The maintainer writes it
once; agents find it by what it shows (`graph describe views`, `graph describe search "<words>" --kind
view`) and call it with `graph view`. It reads the caller's own snapshot, so nothing is stored, cached
or visible beyond what the caller may see. The graph checks the file against the ontology and the
published functions when it is published and again on every ontology release, so a published view
always resolves. Publishing and releasing are in the `maintaining-graphs` skill; this skill is about
the view files. `references/format.md` is the reference of the file format (`graph_cli.view/1`), of what
a call answers and of everything publishing checks.

## Two flavours

- **Entity view** (`takes: <class>`): what matters about one entity of that class, in one answer of at
  most 6 KiB: chosen fields, relations, neighbours with their fields, the result of a function for this
  entity. Called with a reference: `graph view REF --name <view>`. With `default: true` it is what
  plain `graph view REF` shows for the class; `--name card` is always the generated card.
- **Set view** (no `takes`): rows of a whole set, for a script, a renderer or an export: one listing
  per class with every attribute and the relations as neighbour keys. Called without a reference:
  `graph view --name <view>`; the answer comes in pages of whole rows (`--page-bytes`, `--cursor`).

## When to write a view

Write an **entity view** when agents keep asking about one kind of entity and the generated card
(`graph view REF`) misses what they need: other fields than the card's choice, the neighbours with
their attributes instead of names only, a filtered or ordered neighbour list, a function's result
attached. Keep it small and make it the `default` only when it is what almost every reader wants first.

Write a **set view** when a consumer reads a whole type: a catalog renderer, an export, a report that
would otherwise make one `graph find` call per type and join the results itself. A set view answers in
one call, from one state of the graph, with stable keys (`ref`), `find`'s order and the out-relations
in `find`'s edge order.

Choose the nearest tool instead when:

- The question is one recurring query with a handful of columns: that is a function
  (`authoring-graph-functions`); a view can still show it through a `rows` section.
- One entity, one hop: `graph neighbors` and `graph get` answer it.
- The consumer needs a computed value along a chain of unbounded depth, or conditions across several
  relations: write a function; a view's filters are the `find` grammar on one relation.

## Where it lives

In the maintainer workspace, in the folder of the graph: `views/<name>.yaml`, one file per view, next
to `ontology/`, `skill/`, `functions/` and `actions/`. `graph-admin workspace sync` pulls the published
ones and `graph-admin graph push` publishes them again, last (after the functions they run). A view
belongs to a module (`module:`; optional when the graph has one module). Views have their own
namespace: a view may carry the name of a function; `card` is reserved.

## Writing one

1. **Name** it for what it shows (`project_overview`, `org_catalog`): lowercase words joined by `_`,
   the file name is the name plus `.yaml`.
2. **`summary`** (one line, at most 100 characters) and **`use_when`** (at most 600): what the view
   shows and for which questions or consumers, in the words users use; no entity names, ids or sample
   values. Search ranks on these, so a vague text means a view nobody finds.
3. **`takes`** or none, then **`default`** (entity views): at most one default per class.
4. **`params`**: the same grammar as function parameters. Prefer a typed `ref` for an entity over a
   name; an enum parameter names a model enum (`enum: <curie>`). A parameter stands for a whole value
   in a `where` entry (`status=$status`) or in the `args` of a `rows` section (`{unit: $unit}`;
   `$entity` for the entity of an entity view).
5. **`sections`**: one entry per part of the answer, in order. Give a repeated kind an `id`. Use
   `fields: all` for a renderer that wants every attribute; list the few names for an agent. Prefer
   `neighbors` (filtered, ordered, with fields) to the card's `relations` overview when the agent will
   act on the neighbours. Let a `list` section carry `relations: all` so a consumer gets the keys of
   every neighbour and joins in memory.
6. **Anything beyond the `find` grammar** (an `OR` across relations, an aggregate, several hops) is a
   function in a `rows` section, not a longer view.
7. **`examples`**: a question or two, in plain words.

Check the shape first with `graph describe schema` and `graph describe type <type>`, and try the
filters as `graph find --type <type> --where <entry>` before writing them into the file.

## The loop

1. Write or edit `<folder>/views/<name>.yaml`.
2. Check without publishing: `graph-admin graph views publish <graph> <folder>/views --dry-run`.
   Every problem of every file comes back in one answer (`admin.views_invalid`, `details.problems`
   with the view, the rule `code`, the path in the file and often the line); a file with a shape
   problem shows its term and function problems only after that is fixed.
3. Fix and repeat until it passes, then publish as described in `maintaining-graphs`. `graph-admin
   graph views show <graph>` lists what is published and against which ontology release.
4. Try it as an agent would: `graph describe search "<the question>" --kind view`, `graph describe
   view <view>` (it ends with the call), `graph view REF --name <view>`; for a set view
   `graph view --name <view> --page-bytes 4096`, then `--cursor` with the `next` of the page, and
   compare with the `graph find` reads it replaces. If the search does not find it, sharpen `summary`
   and `use_when`.
5. Look at the answer as the smallest reader would: `bytes`, `cut`, `more`, `approx`. An entity view
   that is trimmed (`cut: true`, stubs) wants fewer or smaller sections or a lower `max`.

## Release refusals

The invariant: the published views always resolve against the ontology the graph is pinned to and the
functions it has. So:

- `graph-admin ontology diff <dir> --graph <graph>` warns `admin.views_affected` with the views a
  candidate release would break.
- `graph-admin ontology release` and `graph-admin graph push` refuse a release with
  `admin.views_broken` when a published view would no longer resolve, on every version bump. The fix
  is one `graph-admin graph push`: the new ontology version and the repaired or deleted view files
  together; the pushed files are checked against the model the graph will have. Do not deprecate your
  way out: a deprecated view still answers, so it must still resolve.
- `graph-admin graph functions publish` refuses with `admin.views_broken` when a function a view runs
  would be removed or change its parameters: push the function and the view change together.
- `graph-admin graph check <graph>` also reports a published catalog that no longer resolves; push the
  views again. A view that no longer resolves refuses a call that names it with `view.stale`.

## Changing and retiring

- Edit the file and publish; a view has no version, its hash changes with its content. Consumers that
  cache an answer key it by the hash, the parameters, the page size, the cursor, the graph's seq and the reader
  (what it may see).
- To replace one, publish the new view, then set `status: deprecated` and `replaced_by: <new name>` on
  the old one. It keeps answering, with a warning that names the replacement, and is left out of
  `describe views` and of the affordances (search marks it deprecated). To retire it, delete its file.

## Common refusals

| Code | What to do |
| --- | --- |
| `view.unknown_attribute`, `view.unknown_relation`, `view.unknown_class` | the name is not in the model for that class; `graph describe type <type>` shows what is |
| `view.invalid_where`, `view.invalid_order` | the entry does not plan: `details.error` carries the planner's code; test it with `graph find` |
| `view.type_mismatch` | the parameter type does not fit the attribute or function parameter (`references/format.md`, type fit) |
| `view.placeholder_position` | a `$name` must stand alone as a whole value |
| `view.flavour_mismatch` | the key or section kind belongs to the other flavour (`takes` decides) |
| `view.duplicate_section` | give each section of one kind its own `id`; at most one `files` and one `affordances` |
| `view.default_taken` | another view is already the default of that class |
| `view.unknown_function`, `view.argument_missing`, `view.literal_reference` | the `rows` section: publish the function first, bind every required parameter, bind a `ref` through `$param` or `$entity` |
| `view.unknown_key`, `view.missing_key`, `view.invalid_value` | the file does not follow the format; see `references/format.md` |
