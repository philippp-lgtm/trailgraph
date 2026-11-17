# Feeding a graph from a pipeline

A pipeline (a scheduled job, a collector, an extractor) keeps part of a graph equal to an
external source. It does not write ops: it hands `graph apply` the desired state of declared
scopes in a reconcile document (`source` and `reconcile`, keys in the change set reference of
the `graph-cli` skill), and graph_cli computes the difference by key and writes one
transaction. Agents do not write these documents. The maintainer's part is the model, the role
and the first run.

## What the graph needs

1. **A key attribute** on every class a pipeline feeds: an optional, unique, single-valued text
   attribute (`ontology-best-practices`, `references/modeling.md`, "Fields an external source
   sets"). A pipeline stores `<source name>/<item key>` in it, so no two sources and no hand
   write share a key, and entities written by people have none and are never in a scope. Adding
   it is a minor release.
2. **A source name per pipeline**: lowercase letters, digits and `_`, stable. It is the key
   namespace; renaming it rewrites every key, which is a mass move.
3. **A role for the pipeline's token** (below), not the write level of a person.

## The role

Roles are declared in the installation policy and given to a token or an enrollment code, as in
[give_an_app_a_role.md](give_an_app_a_role.md). For a pipeline the `writes` of its grant list
what its documents declare, per concrete class it feeds:

- `create` and `update` with the key attribute and every attribute named in `fields` (a list,
  or `true`);
- `link` and `unlink` with the relations named in `edges`, when it sets any;
- `retract` for a complete scope with the default retire, or `transition` for a complete scope
  that retires by lifecycle and for a scope that revives;
- `reads` for every class it reads, and the `markings` of every marked field it sets.

The right is checked before the run plans anything, from what the document declares, not from
what it would change: a role without `retract` is denied on its first run with a complete
scope, even when nothing would retire. So the denial shows up at set-up, not on the day
something disappears. A hidden or marked field is refused by name like an attribute the class
lacks. `graph-admin policy explain --graph <graph> --role <role>` shows what the role gets.
Never give a pipeline more than its documents declare; a pipeline that can retract can also
retract by hand.

## The first runs

1. Dry run as the pipeline's token: `graph apply <file> --dry-run --json`. `data.results[].result.reconcile`
   lists, per scope, what would be created, updated, retired and moved; every retired key is
   listed in a dry run.
2. Entities that already exist and have no key (written before the attribute existed) are
   **adopted**, not recreated: the first document lists `moves` with `{"id": <ref>, "to":
   <item key>}` for each, and the matching item for it. The matching rule (which entity is
   which item) belongs to the pipeline; graph_cli takes the mapping and does not guess. Check
   the dry run: `adopted` counts them and `created` must not hold duplicates. Run the same
   document again: it writes nothing (`seq` null).
3. First import: a document of at most 5000 operations after planning. A larger source goes in
   as partial scopes over several documents (no retire), then one complete run.
4. Switch the scope to `complete` only after a run that created and updated correctly.
5. `graph-admin audit verify`, and `graph history <id>` shows `source` on what the run wrote.

## What a run refuses, and what to do

Refusals are audited and write nothing. Raise an override only on the word of the pipeline's
owner that the loss is intended; never to make a failure go away.

| Code | Meaning | Action |
| --- | --- | --- |
| `reconcile.source_empty` | a complete scope with no items while entities exist | the source failed or was empty; fix the pipeline to send no entry for a failed source; `allow_empty` only for a real wipe |
| `reconcile.retire_limit` | more retires than 5 and a tenth of the scope | read the listed keys: a changed key scheme or a partial read is a defect; `allow_retire` set to the refusal's count only for an intended removal |
| `reconcile.key_attribute` | the key attribute is missing, not unique, not text or not single-valued | fix the ontology or the document |
| `reconcile.move_invalid`, `reconcile.type_mismatch`, `reconcile.scope_overlap`, `reconcile.scope_conflict`, `reconcile.target_not_found`, `reconcile.shape` | the document contradicts the state or itself | the pipeline's owner fixes the document |

## Releases that touch key attributes

A pipeline names terms of the model in every document, and keys are stored data.

- **Pause the pipeline across a major release** that renames or retypes a key attribute, a field,
  a relation or a fed class, or that rewrites values of a key attribute (`map_values`, `fill`
  in a migration). Rewritten keys make the next complete run retire everything under the old
  keys; the retire limit usually stops it, but a pause is the rule.
- After a rename the pipeline's documents name old terms and fail with the hint what they are
  called now: change the pipeline, resume it, and run it once with `--dry-run`.
- After a migration that changed key values, run a document with `moves` from the old keys to
  the new ones before the first complete run.
- A minor release that only adds terms needs no pause.

A queued run waits for the lock and plans under the new model after it; a run during an
unfinished migration is refused (`write.migration_incomplete`) and can be retried after the
same release is run again.
