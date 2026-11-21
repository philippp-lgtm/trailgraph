# Action file format: `graph_cli.action/1`

One file per action, `actions/<name>.yaml` in the graph's workspace
folder. Read with the strict YAML loader (no anchors, no duplicate keys,
no unknown keys). This page is the single reference of the format and of
what publishing checks; the guidance on writing good actions is in
`SKILL.md`. Parameters follow the grammar of the function files
([Parameters](../../authoring-graph-functions/references/format.md#parameters)):
that page is the reference of the types, `ref`, `many` and `default`; only
what differs for actions is stated here.

## A complete file

```yaml
schema: graph_cli.action/1
name: hand_over_position
module: organization
summary: Replace the current holder of a position by another person, from a date.
use_when: >-
  A position that somebody holds gets a new holder: succession, a swap, a handover.
  Not for staffing a vacant position: use assign_position.
params:
  position:
    type: ref
    class: org:Position
    summary: The position that changes hands.
  person:
    type: ref
    class: ppl:Person
    summary: The new holder.
  since:
    type: date
    summary: Date the new holder takes over.
    required: false
preconditions:
  - id: position_held
    on: position
    summary: Somebody holds the position; use assign_position for a vacant one.
    require:
      - count: {rel: holds, direction: in, where: {record_status: [active]}}
        minimum_value: 1
ops:
  - {op: unlink, id: $position, rel: org:holds, direction: in, all: true}
  - {op: link, id: $person, rel: org:holds, target: $position, qualifiers: {since: $since}}
examples:
  - request: The head of the support team hands the lead position over to a colleague.
featured: true
```

## Top-level keys

| Key | Required | Meaning |
| --- | --- | --- |
| `schema` | yes | exactly `graph_cli.action/1` |
| `name` | yes | `^[a-z][a-z0-9_]{1,62}$`; equals the file name without `.yaml`; unique in the graph across actions and functions |
| `module` | with several modules | id or prefix of a module of the graph that is not built in; optional when the graph has one module |
| `summary` | yes | one line, at most 100 characters: what the action does |
| `use_when` | yes | at most 600 characters: the situations and requests the action fits, in users' words, and the neighbouring action for what it does not fit; what search and agents match a request against |
| `params` | no | mapping of parameter name to declaration (below); omit for none |
| `preconditions` | no | list of preconditions (below), at most 10 |
| `ops` | yes | the template: a non-empty list of ops (below), at most 50 |
| `reason` | no | `optional` (default) or `required`: with `required`, `graph do` is refused without `--reason` |
| `examples` | yes | at least one `{request}`: what a user would ask for, in plain words, without entity ids or names |
| `featured` | no | `true` lists the action in the graph skill (at most 8 are listed); default `false`. The only key that changes the skill: publishing an action that is not featured leaves installed skills current |
| `status` | no | `active` (default) or `deprecated` |
| `replaced_by` | no | name of an active action; only with `status: deprecated`; an action cannot replace itself |

Summaries of parameters and preconditions and example requests have at
most 200 characters (a precondition `summary` is one line).

## Parameters

The declaration keys and the types are those of the function files
(`type`, `summary`, `class`, `enum`, `many`, `default`); what differs:

| Key | Meaning |
| --- | --- |
| `required` | `false` makes the parameter optional without a default: when the caller leaves it out, its entry in the op is dropped. `true` is the default. Not with `default` (a default already makes it optional) and not in function files |

- An optional parameter may only stand where its entry can be dropped: a
  value in `attrs`, `attrs_text`, `set`, `set_text`, `qualifiers`
  (also of `edges`), `valid_from`, `valid_to`, or a whole list value of
  `add`, `remove` and their `_text` forms. Anywhere else (a reference, an
  element of a list, an alias, a precondition's `on`) it is refused. A
  dropped entry never drops a whole op: an action has no conditional ops.
- A `ref` stands only where an entity reference goes, never as a value. A
  `many` `ref` stands for a whole `targets` list and nowhere else.
- A `default` fits the declared type; a `ref` has no default.
- Examples carry no parameter values at all.

## Ops

Each entry of `ops` is a change-set op (`graph_cli.changeset/1`) where a
parameter written `$name` stands for a whole reference or a whole value.
The keys per op kind:

| Op | Keys |
| --- | --- |
| `create` | `op`, `type`, `local`, `attrs`, `attrs_text`, `edges` (each `{rel, target, qualifiers}`), `valid_from`, `valid_to` |
| `update` | `op`, `id`, `set`, `set_text`, `unset`, `add`, `add_text`, `remove`, `remove_text` |
| `link` | `op`, `id`, `rel`, `target`, `qualifiers` |
| `unlink` | `op`, `id`, `rel`, `target`; or the form below |
| `transition` | `op`, `id`, `to`, `attr` |
| `supersede` | `op`, `by`, `targets` |
| `retract` | `op`, `id`, `by` |
| `set_alias` | `op`, `id`, `alias` |

Types, relations, attributes, qualifiers and states are written as in a
change set and must exist in the ontology; they are never parameters.

**Where a placeholder may stand.** Entity references (`id`, `target`, `by`,
`edges[].target`, an element of `targets`) take a `ref` parameter or an
`@local` created earlier in the action (`local` of a `create`); a
literal id is refused, because ids go stale. Values (attributes, qualifiers,
`valid_from`, `valid_to`, `alias`, an element or the whole list of `add` and
`remove`) take a parameter of a type that fits the field. A `string`
parameter fits `string`, `text` and `iri` fields; an `enum` parameter
`string` and `text`; `integer` and `decimal` fit `integer` and `decimal`;
`boolean`, `date` and `datetime` fit their own. A `_text` position takes
the text form of the value. A `$` string anywhere else (a type, a relation,
an attribute name, a state, a key) is refused, and there is no escape for a
literal text that starts with `$`.

**Refused keys.** `expect_rev`, `target_rev` and `edge_id` depend on the
state when the action runs; `reason`, `idempotency_key`, `expect_seq` and
`schema` belong to the document around the ops. None can be part of a
template (`graph do` supplies `--reason` and `--idempotency-key`). A
lifecycle attribute is changed by `transition` only (`set`, `unset`, `add`
and `remove` are refused; `create` may set an initial state).

**One dynamic form.** `{op: unlink, id: $p, rel: <relation>, direction:
out, all: true}` removes every active edge of that relation at the entity
the parameter names, whatever the other end is. `direction` is `out`
(default: the edges the entity owns) or `in` (the edges pointing at it,
from active entities). The only keys are `op`, `id`, `rel`, `direction` and
`all`; `id` is a `ref` parameter, not an `@local`. When the entity has no
such edge nothing is removed; when that leaves the whole action without an
op, `graph do` is refused with `action.nothing_to_do`. The dry run shows
the unlinks it expanded to.

## Preconditions

A precondition says that the action applies to one entity as it is now. It
is checked on the current state of that entity under the graph lock,
before the action's own change; when any fails, the action writes nothing
and the refusal lists every failed one (`action.precondition_failed`, with
the precondition, the parameter and the entity).

| Key | Meaning |
| --- | --- |
| `id` | `^[a-z][a-z0-9_]{0,31}$`, unique in the action |
| `on` | a required `ref` parameter, not `many`; the condition is evaluated on that entity, in the scope of the parameter's class |
| `summary` | one line, at most 200 characters, shown to the agent when the action is refused and in `graph describe action` |
| `require` | a list of conditions in the authored form of ontology rules and guards; all must hold |

The conditions are those of ontology rules (attribute tests, `count` and
`sum` over the edges one step away, `all_of`, `any_of`, `none_of`); the
grammar is in
[Conditions](../../ontology-best-practices/references/ir_format.md#conditions).
Terms are checked against the class of `on`. Not possible: a parameter as
an operand, and conditions that relate two entities.

## What publishing checks

`graph-admin graph actions publish` (also as part of `graph-admin graph
push`) checks every file against the ontology the graph is pinned to (or
will be, in a push) and reports all problems together as
`admin.actions_invalid`. The `code` of each problem in `details.problems`:

| Rule | Codes |
| --- | --- |
| the file | `action.invalid_yaml`, `action.unsupported_schema`, `action.unknown_key`, `action.missing_key`, `action.invalid_value`, `action.name_mismatch`, `action.invalid_file` (the folder holds anything but `<name>.yaml` files) |
| the module | `action.unknown_module`, `action.builtin_module`, `action.module_required` |
| parameters | `action.unknown_class`, `action.unknown_enum`, `action.invalid_default`, `action.parameter_undeclared`, `action.parameter_unused`, `action.optional_position`, `action.placeholder_position`, `action.type_mismatch` |
| the ops | `action.unknown_type`, `action.unknown_relation`, `action.unknown_attribute`, `action.unknown_qualifier`, `action.unknown_state`, `action.ambiguous_term`, `action.abstract_type` (a `create` of an abstract class), `action.lifecycle_missing` (a `transition` on a class without that lifecycle attribute), `action.lifecycle_direct_write`, `action.invalid_op`, `action.literal_reference`, `action.local_duplicate`, `action.local_unresolved` |
| the fit | `action.op_impossible`: no domain and range of the relation allows the types of this op (it could never write); `details.connecting` shows what connects them |
| the assembly | `action.template_invalid`: with typed values (and one edge per `all: true`) the ops do not form a change set, also with every optional parameter left out |
| preconditions | `action.precondition_on_invalid` (`on` is not a required single `ref` parameter), `action.invalid_condition` (the conditions do not parse), `action.not_numeric` (a numeric test on an attribute that is not a number) and the term codes above for an attribute or relation the class does not have |
| replacement | `action.replaced_by_invalid` (target missing, deprecated or itself) |
| the namespace | `action.name_taken` (the name is a function of the graph; functions and actions share one namespace) |
| limits | `action.too_many_ops` (50 per action), `action.too_many_preconditions` (10 per action), `action.too_many_actions` (1,000 per graph) |

A declared parameter that no op or precondition uses is
`action.parameter_unused`; a `$name` that is not declared is
`action.parameter_undeclared`.

Every ontology release, `graph-admin graph push` and `graph-admin graph
check` compile the stored actions again against the model the graph will
have (`admin.actions_broken` when one fails), and the stored normalized
form must still compile too: an action that no longer does is
`action.stale`, also when `graph do` meets it.

## Published form

Publishing stores the files and a compiled catalog
(`graph_cli.action_catalog/1`) that the app reads: per action the
declaration, the normalized ops (names become prefixed CURIEs), the
lowered preconditions, `touches` (the types and relations it names, with
their modules), `needs` (the `write.*` action and type pairs its ops are
authorized as) and a hash of the entry. Agents see the entry with `graph
describe action <action>`; the hash names the definition in the audit trail.
