---
name: authoring-graph-functions
description: Write, check and publish the named read functions of a graph_cli graph - one YAML file per function (format graph_cli.function/1) holding a typed, read-only Cypher query that agents find with graph describe search --kind function and run with graph run. Use when agents keep writing the same query, when a recurring question needs a safe typed shortcut, when a function is refused on publish or blocks an ontology release, or when functions are added, changed or deprecated. Not for the text of the graph skill (authoring-graph-skills) and not for the workspace workflow (maintaining-graphs).
---

# Authoring graph functions

A function is a curated, typed, read-only query with a name. The maintainer
writes it once; agents find it by what it is for and run it with
`graph run <function> --param <name>=<value>` instead of composing the
query themselves. The graph checks it against the ontology when it is
published and again on every ontology release, so a published function
always resolves. Publishing and releasing are in the `maintaining-graphs`
skill; the graph skill that lists featured functions is written with
`authoring-graph-skills`; this skill is about the function files.
`references/format.md` is the reference of the file format
(`graph_cli.function/1`) and of everything publishing checks.

## When to write a function

Write one when all of these hold:

- The question comes back: agents ask it again and again, in different
  words, or an agent run showed a long detour for it (several schema
  commands, then a hand-built query).
- The answer is a query, not a lookup: a multi-hop path, a walk of a
  hierarchy, an aggregate, a join across modules, or a result with
  hand-picked columns. `graph find`, `lookup` and `neighbors` already
  answer single-entity and single-hop questions.
- The inputs are few and typed: an entity of a known class, a literal, a
  value of an enum. If the question needs the agent to decide which
  entity is meant, the function still takes the reference; resolving a
  name is the agent's step before it (`graph lookup`).
- One stable set of columns answers the question. If every caller needs
  different columns, leave it to ad-hoc queries.

Do not write one for a single occasion, for a question the graph skill's
recipes answer in one command, or for a change to data: functions never
write. A recurring write recipe (several ops, or a check of the current
state first) is an action: see `authoring-graph-actions`. Functions and
actions share one namespace per graph (`function.name_taken`).

## Where it lives

In the maintainer workspace, in the folder of the graph:
`functions/<name>.yaml`, one file per function, next to `ontology/` and
`skill/`. `graph-admin workspace sync` pulls the published ones, and
`graph-admin graph push` publishes them again with the ontology and the
skill. A function belongs to a module of the graph (`module:`; optional
when the graph has one module).

## Writing one

1. **Name** it for the result, not for the command: lowercase words
   joined by `_`, the file name is the name plus `.yaml`. Name what comes
   back and from where (people of a unit, open items of an owner), so the
   name reads well in a list and matches the words people use. Keep names
   short and distinct from each other; two functions that differ only in
   a suffix are one function with a parameter.
2. **`summary`**: one line, at most 100 characters, what the function
   returns. It is shown in every listing.
3. **`use_when`**: at most 600 characters, the situations in which an agent
   should pick this function, in the words users use: the kinds of
   questions, the synonyms, what it counts or includes, and what it does
   not answer. Write criteria, not data: no entity names, ids or sample
   values; they go stale and add nothing to the match. Search and the
   agent read this text, so a vague `use_when` means a function nobody
   finds.
4. **`params`**: declare every input with a type. Prefer a typed
   `ref` parameter (with `class:`) over a name: the caller passes an id,
   an alias or trailing hex digits, never a name, and a name that is
   ambiguous in the graph cannot pick the wrong entity. The answer for a
   wrong class is a refusal that tells the agent to `lookup` first. Use
   `enum` for a closed set, `many: true` for a list, `default` for an
   optional input (a `ref` has no default).
5. **`query`**: read-only Cypher (`graph describe schema` shows the names
   and patterns). Use `$name` for every parameter, compare `ref`
   parameters with `id` or `alias`, do not use a parameter in `SKIP` or
   `LIMIT` (callers page with `--limit` and `--offset`), alias every
   returned expression with `AS`, never `RETURN *`, and give the rows an
   `ORDER BY` so results are stable. On a graph with several modules write
   names as `graph describe schema` shows them, so a later module that
   reuses a local name cannot make the query ambiguous on a release.
6. **`returns`**: one entry per result column, in the order of `RETURN`,
   with a summary of what the column holds. This is the output contract;
   publishing refuses a list that differs from the query's columns.
7. **`examples`**: at least one question a user would ask, in plain words.
   Questions only; an example parameter must be a literal of the declared
   type, never an entity reference.
8. **`featured`**: `true` puts the function into the graph skill's list
   that every agent reads first. At most 12 functions are listed (by
   module, then name; further ones are not shown there), so feature only
   the few that answer the questions most often asked, and leave the rest
   to search and the module pages.

Check the query by running it as an ad-hoc `graph query` with real values
first; then write the file around it.

## The loop

1. Write or edit `<folder>/functions/<name>.yaml`.
2. Check without publishing:
   `graph-admin graph functions publish <graph> <folder>/functions --dry-run`.
   Every problem of every file comes back in one answer
   (`admin.functions_invalid`, `details.problems` with the function, the
   rule `code`, the path in the file and often the line).
3. Fix and repeat until it passes, then publish as described in
   `maintaining-graphs`. `graph-admin graph functions show <graph>` lists
   what is published and against which ontology release.
4. Try it as an agent would: `graph describe search "<the question>" --kind
   function`, `graph describe function <function>`, `graph run <function>
   --param <name>=<value>`. If the search does not find it, sharpen
   `summary` and `use_when`.
5. Agents see a changed list in the generated graph skill after `graph
   skills install` or `graph self update`.

## Release refusals

The invariant: the published functions always resolve against the ontology
the graph is pinned to. So:

- `graph-admin ontology diff <dir> --graph <graph>` warns
  `admin.functions_affected` with the functions a candidate release would
  break, before anything is released.
- `graph-admin ontology release` and `graph-admin graph push` refuse a
  release with `admin.functions_broken` when a published function would no
  longer resolve, on every version bump, minor and patch included (a minor
  can remove or rename a term a query names). `details.problems` names the
  function and the rule.
- The fix is one `graph-admin graph push`: the new ontology version and the repaired
  or deleted function files together. The pushed files are checked against
  the model the graph will have, so the release and the fix go through
  in one step. Do not try to deprecate your way out: a deprecated function
  still runs, so it must still resolve.
- `graph-admin graph check <graph>` also reports a published catalog that
  no longer resolves (`function.stale`, for example after an interrupted
  push); push the functions again.

## Changing and retiring

- Edit the file and publish; a function has no version, its hash changes
  with its content. Keep the name when the answer stays the same kind of
  thing; a new meaning gets a new name.
- To replace one, publish the new function, then set `status: deprecated`
  and `replaced_by: <new name>` on the old one. It keeps running, with a
  warning that names the replacement, and is left out of listings. To
  retire it, delete its file (history is the audit trail and the
  workspace's git).
- A function that a published view runs in a `rows` section cannot be removed, or changed so that
  the view's `args` no longer fit, on its own: `graph-admin graph functions publish` refuses with
  `admin.views_broken`. Push the function and the view change together (`authoring-graph-views`).

## Common refusals

| Code | What to do |
| --- | --- |
| `function.returns_mismatch` | make `returns` list exactly the query's columns, in order (alias every expression) |
| `function.parameter_undeclared`, `function.parameter_unused` | declare every `$name` the query uses, and drop parameters it does not use |
| `function.pattern_impossible` | the relation does not connect those types; `details.connecting` and `graph describe schema` show what does |
| `function.type_mismatch` | the parameter type does not fit the field it is compared with (`references/format.md`, type fit) |
| `function.module_required`, `function.unknown_module` | name a module of the graph in `module:` |
| `term.unknown`, `query.*` | the query itself: fix the name or the syntax |
| `function.unknown_key`, `function.missing_key`, `function.invalid_value` | the file does not follow the format; see `references/format.md` |
| `function.round_trip_failed` | a defect of graph_cli, not of the file: write the query another way (`references/format.md`, published form) and tell the user so it gets reported |
