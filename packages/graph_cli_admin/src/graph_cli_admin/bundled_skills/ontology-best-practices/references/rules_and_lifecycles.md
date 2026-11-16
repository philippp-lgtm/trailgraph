# Rules, guards and lifecycles

What must hold in the data, and when it is checked. The syntax of
conditions is in [ir_format.md](ir_format.md#conditions).

## When checks run

| Check | On every write | In a batch check |
| --- | --- | --- |
| types, facets, enum values, required, `many` | touched entities | every entity |
| `unique`, `acyclic` | touched entities | every entity |
| `immutable` | touched entities, against their previous revision | no (needs the previous revision) |
| `domain`, `range`, `out` cardinality, qualifiers | edges of touched entities | every edge |
| `in` cardinality | no | every active entity |
| edges to an inactive target (`on_target_inactive`) | edges the write adds, and the edges pointing at an entity it retracts or supersedes | every edge of an active entity |
| lifecycle: initial state, allowed transition, guards | the transitioned entity | state names only |
| rules with `when: write` | touched entities, and for rules with `count` or `sum` also the entities one edge away | every active entity |
| rules with `when: batch` | no | every active entity |

Two consequences:

- **A batch check runs only when someone runs it**:
  `graph-admin graph check <graph>` lists every finding, warnings
  included, and fails on errors; a major release on a graph with data
  runs it too and is refused on errors. No write runs batch rules or `in`
  cardinality, so anything someone must see while working is a `write`
  rule. Batch rules suit completeness that is reviewed from time to time,
  such as before a release.
- **A write rule judges an entity when it is written.** A rule added in a
  release does not look at existing entities until they are written
  again; `graph-admin graph check` or a major release checks them all.

## Error or warning

| Use `error` when | Use `warning` when |
| --- | --- |
| a violation is always a mistake | the data may be right and the source wrong |
| the write must not happen | the write must happen and the finding must be seen |
| existing data already satisfies it | it checks plausibility (sums, counts, completeness) |

A warning is written into the result of the write (`data.findings`), so
the agent can report it. Report a mismatch, never smooth it: a warning
that a sum does not add up must not lead to an invented value. Adding an
error rule later is major and needs every existing entity to satisfy it.

## Writing rules

- **One rule, one statement.** Its summary is the message of the finding;
  write it as the condition that must hold.
- **Narrow with `if`.** A rule that applies only in a state or when a value
  is present uses `if`; entities outside it are not affected. This is also
  the way to tighten a model without touching old data: the rule applies
  from the moment an entity reaches the state.
- **Aggregates.** `count` and `sum` look one edge away. A write checks the
  entity and its direct neighbors, so load a tree that a rule sums over in
  one change set; loaded piece by piece, a warning fires for every
  intermediate state.
- **Absent sums.** A `sum` is absent when one counted entity lacks the
  value. Put `value_presence: PRESENT` on the sum in `if`, unless a
  missing value should itself be a finding.
- **Filter by `record_status`.** Counts over children usually mean active
  ones: `where: {record_status: [active]}`.
- **Numbers.** Constants are integers. Compare with a decimal value by
  naming the attribute (`maximum_value: {attr: total}`).
- **No derived storage.** If a rule checks that a stored total equals a
  sum, ask first whether the total must be stored at all.

## Edges to inactive targets

`graph retract` and `graph supersede` make an entity inactive; edges that
point at it stay. A relation's `on_target_inactive` decides what a write
meets that leaves an active entity pointing at an inactive one:

| Value | Retract or supersede of a target that active entities point at | New edge to an inactive target | Batch check |
| --- | --- | --- | --- |
| `allow` (default) | passes | warning | warning |
| `warn` | warning `data.referential.target_referenced` | warning | warning |
| `refuse` | refused: error `data.referential.target_referenced` | error | error |

Choose per relation:

- **`refuse`** when the source makes no sense without its target: a part
  and its whole, a folder and the folder it lies in, a step and its plan.
  The target stays active until every source is relinked or retracted.
- **`warn`** when the link should be looked at but may stay: an
  assignment, a reference to a document that was replaced.
- **`allow`** for history and provenance: a link to something that was
  later withdrawn is still a true record (`derived_from`, `based_on`).

The judgement is on the result of the whole change set: relinking or
retracting the sources together with the target passes. Nothing is
retracted or relinked automatically; the agent decides for each source.
This replaces the rule idiom "count the active targets" on the source
class, which reports at the source and names no reason. Moving a relation
to `refuse` is major: a major release runs the batch check, so edges left
pointing at inactive targets must be fixed before.

## Guards

A guard is a condition for one transition: it is checked when the entity
moves, not afterwards. Use a guard for "may only be marked done when ...";
use a rule for "is never ...". Guards take the same conditions as rules;
their summary is the message when the move is refused.

## Lifecycles

- **States are decisions people make**, each with a summary that says when
  an entity is in it. Three to five states are typical.
- **One initial state** where possible; the entity starts there without
  naming it. Several initial states make every create choose one.
- **Terminal states** end the lifecycle. Mark them only when nothing ever
  comes back; otherwise add the transition back.
- **Correction paths.** Add transitions back (`tested` to `draft`) for real
  corrections; without them a wrong move can only be fixed by superseding
  the entity.
- **Not for record state.** Withdrawn and replaced entities are handled by
  `graph retract` and `graph supersede`; a lifecycle describes the domain
  (draft, active, closed), not whether the record is still valid.
- **Not for values that are corrected often.** A lifecycle attribute
  changes only through `graph transition`; a value that is just edited is
  an enum attribute.

## Proving rules and guards

On a throwaway graph
([versioning_and_release.md](versioning_and_release.md#trying-an-ontology-on-a-throwaway-graph)),
make every rule and guard fire once with data that breaks it, and check
that the finding names the rule and reads well. A rule that cannot be
made to fire is wrong or unnecessary.
