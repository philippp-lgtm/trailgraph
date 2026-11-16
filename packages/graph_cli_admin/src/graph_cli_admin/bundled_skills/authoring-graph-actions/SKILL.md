---
name: authoring-graph-actions
description: Write, check and publish the named write actions of a graph_cli graph - one YAML file per action (format graph_cli.action/1) holding typed parameters, preconditions on the entities it is given and a template of write ops that agents find with graph describe search --kind action and run with graph do. Use when agents keep repeating the same multi-step change, when a change needs a check of the current state before it is written, when an action is refused on publish or blocks an ontology release, or when actions are added, changed or deprecated. Not for read queries (authoring-graph-functions), not for rules every write must obey (ontology-best-practices) and not for the workspace workflow (maintaining-graphs).
---

# Authoring graph actions

An action is a curated, typed write recipe with a name. The maintainer
writes it once; agents find it by what it is for and run it with `graph do
<action> --param <name>=<value>` instead of composing the writes
themselves. It compiles to one change set: one document, one transaction,
one audit entry that names the action, and every op is authorized as if the
caller had written it by hand. The graph checks the file against the
ontology when it is published and again on every ontology release, so a
published action always resolves. Publishing and releasing are in the
`maintaining-graphs` skill; the graph skill that lists featured actions is
written with `authoring-graph-skills`; this skill is about the action
files. `references/format.md` is the reference of the file format
(`graph_cli.action/1`) and of everything publishing checks.

## When to write an action

Write one when the change comes back and one of these holds:

- It takes several ops that belong together (remove the old holder, add the
  new one; remove the links of a leaver, then move it to the state `left`),
  and agents show detours or wrong shapes when they compose it
  themselves.
- It is only right in a certain state: a precondition on the entity as it is
  now ("nobody holds the position", "the employee has not left").
- It must carry a reason every time (`reason: required`).

Choose the nearest tool instead when:

- The change is one `create`, `update` or `link` that the model already
  validates: use the primitives; an action would only rename them.
- The answer is a read: that is a function (`authoring-graph-functions`).
- A condition must hold for every write, whoever makes it and however: that
  is an ontology rule or guard (`ontology-best-practices`). A
  precondition says "this action applies to the entity as it is now" and is
  checked on the current state under the lock; rules judge the state after
  the write, for every write, and still apply to what an action writes.
- The recipe needs the agent to decide between entities, or a query to
  find them: the agent resolves them first (`graph lookup`) and passes
  references; an action never searches.

Actions are never mined or generated: a maintainer writes each one and
reviews it before it is published, because it writes data.

## Where it lives

In the maintainer workspace, in the folder of the graph:
`actions/<name>.yaml`, one file per action, next to `ontology/`, `skill/`
and `functions/`. `graph-admin workspace sync` and `graph-admin graph
pull` bring the published ones, and `graph-admin graph push` publishes
them again with the ontology, the skill and the functions. An action
belongs to a module (`module:`; optional when the graph has one module).
Functions and actions share one namespace: an action cannot be named like
a function of the graph (`action.name_taken`).

## Writing one

1. **Name** it for the change, as a verb phrase: lowercase words joined by
   `_`, the file name is the name plus `.yaml` (`assign_position`,
   `hand_over_position`). Two actions that differ only in a suffix are one
   action with a parameter.
2. **`summary`**: one line, at most 100 characters, what the action
   changes. It is shown in every listing, and it is all of the action an
   agent reads in the graph skill: say the request it answers when that is
   not the action's name (joining a unit is holding one of its positions).
3. **`use_when`**: at most 600 characters, the situations and request
   words that fit the action, and what does not: name the neighbouring
   action. Two actions that are easy to mix up (staffing a vacant position
   against replacing its holder) must each say "not for X: use Y"; that
   sentence is what lets an agent choose, and the preconditions are what
   stops a wrong choice. Write criteria, not data: no entity names or ids.
4. **`params`**: every input, typed, in the grammar of the function files.
   Entities are `ref` parameters with a `class`: the caller passes an id,
   an alias or trailing hex digits, never a name, so an ambiguous name
   cannot pick the wrong entity. `required: false` leaves out an
   attribute or qualifier when it is not given (never a whole op).
5. **`preconditions`**: for each thing that must hold before the action
   makes sense, one entry with an `id`, the `ref` parameter it is `on`, a
   `summary` the agent reads when refused (say what to do instead) and a
   `require` list in the condition form of ontology rules. Keep them to the
   entity the action is given.
6. **`ops`**: the change-set ops with `$parameter` in place of references
   and values. Use `unlink ... all: true` where the recipe removes
   whatever edges there are; the dry run shows which. Every write is
   visible in the file: types, relations and attributes are never
   parameters.
7. **`reason`**: `required` when the change should always be justified (a
   leaver, a retraction); the agent is refused without `--reason`.
8. **`examples`**: at least one request in plain words, no ids and no
   names.
9. **`featured`**: `true` lists the action in the graph skill that every
   agent reads first. At most 8 are listed, so feature only the few that
   save the most detours; the rest is found by search and on the module
   pages.

A complete file that compiles against the example company ontology:

```yaml
schema: graph_cli.action/1
name: assign_position
module: organization
summary: Staff a vacant position from a date; a person joins a unit by holding one of its positions.
use_when: >-
  Someone starts in, joins, takes over or is appointed to a job, a role in the org chart
  or a unit; staffing a vacant position. Not for replacing the current holder: use
  hand_over_position.
params:
  person:
    type: ref
    class: ppl:Person
    summary: Who takes over the position.
  position:
    type: ref
    class: org:Position
    summary: The vacant position.
  since:
    type: date
    summary: Date the person takes over.
preconditions:
  - id: position_open
    on: position
    summary: The position is planned or active, not abolished.
    require:
      - {attr: status, equals_string_in: [planned, active]}
  - id: position_vacant
    on: position
    summary: Nobody holds the position; use hand_over_position to replace a holder.
    require:
      - count: {rel: holds, direction: in, where: {record_status: [active]}}
        equals_number: 0
ops:
  - {op: link, id: $person, rel: org:holds, target: $position, qualifiers: {since: $since}}
examples:
  - request: The new recruiter starts as the support lead on the first of March.
featured: true
```

## The loop

1. Write or edit `<folder>/actions/<name>.yaml`.
2. Check without publishing:
   `graph-admin graph actions publish <graph> <folder>/actions --dry-run`.
   Every problem of every file comes back in one answer
   (`admin.actions_invalid`, `details.problems` with the action, the rule
   `code`, the path in the file and often the line). In a workspace,
   `graph-admin graph push <graph> <folder> --dry-run` does the same
   together with the ontology, the skill and the functions.
3. Fix and repeat until it passes. Have the file reviewed: an action
   writes data on every run. Publish as described in `maintaining-graphs`;
   `graph-admin graph actions show <graph>` lists what is published and
   against which ontology release.
4. Try it as an agent would, on a throwaway graph or with the dry run:
   `graph describe search "<the request>" --kind action`, `graph describe
   action <action>`, then `graph do <action> --param <name>=<value>
   --dry-run`. The dry run writes nothing and shows the ops the action
   expands to. If search does not find it, sharpen `summary` and
   `use_when`; try the neighbouring action with the request that should
   not fit and read the refusal of its precondition.
5. Agents see a changed list in the generated graph skill after `graph
   skills install` or `graph self update`.

## Release refusals

The invariant: the published actions always resolve against the ontology
the graph is pinned to. So:

- `graph-admin ontology diff <dir> --graph <graph>` warns
  `admin.actions_affected` with the actions a candidate release would
  break, before anything is released.
- `graph-admin ontology release` and `graph-admin graph push` refuse a
  release with `admin.actions_broken` when a published action would no
  longer compile, on every version bump (a minor can remove a relation or
  a state an action writes). `details.problems` names the action and the
  rule.
- The fix is one `graph-admin graph push`: the new ontology version and the
  repaired or deleted action files together; the pushed files are checked
  against the model the graph will have. Do not try to deprecate your way
  out: a deprecated action still runs, so it must still compile.
- `graph-admin graph check <graph>` also reports a published catalog that
  no longer compiles (`action.stale`, for example after an interrupted
  push); push the actions again. At run time a stale action is refused with
  the same code.

## Changing and retiring

- Edit the file and publish; an action has no version, its hash changes
  with its content and the audit trail names the hash that ran. Keep the
  name when the change stays the same kind of thing; a new meaning gets a
  new name.
- To replace one, publish the new action, then set `status: deprecated`
  and `replaced_by: <new name>` on the old one. It keeps running, with a
  warning that names the replacement, and is left out of listings. To
  retire it, delete its file.

## Common refusals

| Code | What to do |
| --- | --- |
| `action.parameter_undeclared`, `action.parameter_unused` | declare every `$name` the template uses, and drop parameters nothing uses |
| `action.placeholder_position`, `action.optional_position`, `action.literal_reference` | a `$name` stands only for a whole reference or value; optional parameters only for an entry that can be dropped; entities are `ref` parameters or `@local`, never ids |
| `action.op_impossible` | the relation does not connect those types; `details.connecting` and `graph describe schema` show what does |
| `action.type_mismatch` | the parameter type does not fit the field or reference it stands for |
| `action.lifecycle_direct_write` | change a lifecycle attribute with `transition` |
| `action.template_invalid`, `action.invalid_op` | the ops do not form a change set, or use a key that depends on the state (`expect_rev`, `edge_id`) |
| `action.precondition_on_invalid`, `action.invalid_condition`, `action.not_numeric` | `on` names a required single `ref` parameter; the `require` list follows the condition form of ontology rules |
| `action.name_taken` | functions and actions share one namespace; rename one |
| `action.module_required`, `action.unknown_module`, `action.unknown_*` | name a module and terms the graph has |
| `action.unknown_key`, `action.missing_key`, `action.invalid_value` | the file does not follow the format; see `references/format.md` |

At run time the agent sees `action.precondition_failed` (your `summary`
of each failed precondition), `action.reason_required` and
`action.nothing_to_do` (every `all: true` found no edge): check that these
messages tell it what to do next.
