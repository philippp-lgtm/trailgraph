# Releasing a major version with a data migration

For a major release on a graph with data that does not fit the new version yet: renamed or
moved terms, a merged class, a new required attribute, a removed enum value. The release
carries a `migration.yaml` and graph-admin rewrites the stored data in place; ids, aliases,
history and the audit trail stay. What to write in the file, the steps, and a worked module
split are in the `ontology-best-practices` skill, `references/migrations.md`. This page is
the maintainer's workflow.

## Before the release

1. Ask the user first: a migration rewrites every entity it touches and cannot be undone
   (the old revisions stay in the history, but the current state changes). Only a maintainer
   may migrate; a project admin gets `access.ceiling`.
2. Make sure a backup of the graph exists and is checked.
3. Work in the graph's workspace folder: the new version directory next to the released one
   (`ontology/<new>/`), all module files in it, `migration.yaml` beside them. `from` is the
   version the graph is pinned to now (`graph-admin graph show <graph>`).
4. `graph-admin ontology diff <folder>/ontology/<new> --graph <graph>` lists every rename, the
   functions, actions and policy rules that name an old term, and what a release would refuse.
   Fix the sources it lists. A deny rule that names only an old term is refused
   (`admin.policy_renamed_term`): the user decides on a policy that names both terms, then
   `graph-admin policy diff <file>` and `graph-admin policy apply <file> --reason "<why>"`.
5. `graph-admin graph check <graph> --ontology <folder>/ontology/<new>` is the preview: per
   derived rename and per step the entities and edges it changes (by type, three sample ids),
   the findings that remain, and the steps that match nothing (`admin.migration_step_unused`).
   Read the counts against what you expect; a surprising count is a wrong step or rename.

## The release

```sh
graph-admin graph push <graph> <folder> --dry-run --reason "<why>"
graph-admin graph push <graph> <folder> --reason "<why>"
graph-admin audit verify
```

Push releases the ontology with its migration, publishes functions, actions and the skill in
the same run and writes the new base into the folder. `graph-admin ontology release <graph>
<dir> --dry-run` and the same without `--dry-run` do the ontology alone. The answer's
`migration` block reports the entities, edges and entities per step that were migrated, the
transaction's seq and the duration. `audit verify` must report no errors.

The migration is one transaction under the graph lock. Apps that write wait for the lock
(up to their `lock_timeout_s`) and then get `backend.lock_timeout`, so release in a quiet
moment and tell the user how long it took.

## Refusals

| Code | Meaning and what to do |
| --- | --- |
| `migration.invalid` | the file is wrong; `details.problems` has the path of each problem |
| `admin.migration_required` | the data still does not fit after the migration, or a rename rewrites data and there is no file; `details.findings` and the hint say which |
| `admin.migration_unexpected` | a migration file on a minor or patch release; remove it or make the release major |
| `admin.migration_from_mismatch` | `from` is not the pinned version; a graph several majors behind releases them one by one |
| `ontology.marking_name_reused` | a marked name would show its values, history included, under an attribute with weaker markings (declared again unmarked, an implicit retype carry, a parent attribute redeclared unmarked); `details.reuses` names each; keep the markings, map the retype explicitly or use a new name. `details.unreadable` means an earlier release could not be read |
| `ontology.rename_invalid` | a `renamed_from` does not fit the pinned release; `details.problems` names each |
| `admin.policy_renamed_term` | a deny rule names only the old term; apply a policy that names both first |
| `admin.functions_broken`, `admin.actions_broken` | a published function or action names an old term; fix its file and push it with the ontology |
| `admin.release_exists` | the same version is installed with another ontology or another migration file; use a new version |
| `access.ceiling` | the caller is a project admin; a maintainer must release |

Warnings: `admin.migration_step_unused` (a step matched nothing, check it was meant),
`ontology.rename_stale` (remove a `renamed_from` the previous release applied), `admin.marking_removed` (a deliberate downgrade of a marked attribute: its values, history included, become readable for more callers; confirm with the user) and `ontology.marking_name_returned`.

## An interrupted release: the incomplete state

A crash, a kill or a lost connection can leave the migration committed and the pin not
switched. Then `graph-admin audit verify` warns
(`integrity.audit.migration_incomplete`), `graph-admin graph check <graph>` judges the data under the
new version, apps read with the new version's names and the warning
`graph.migration_incomplete`, apps' writes fail with `write.migration_incomplete`, and any other
release is refused (`admin.migration_incomplete`).

Finish it: run the same release again (the same command, the same folder). It finds the data
migrated, checks it and switches the pin; nothing is migrated twice. Do not change the version
directory or the migration file in between: another file or version is refused as long as the
state is incomplete. If the crash came before the commit, the graph is simply in the old state
and the same command starts over. Then `graph-admin audit verify`.

## Remote mode and timeouts

Over a server the whole release is one request and the answer comes when the migration is done.
The time grows with the entities migrated (the preview counts them). Before a large migration
ask the user to raise `[remote] timeout_s` and to check the read timeout of the ingress in front
of the server, both above the expected duration with a margin (the answer reports `duration_s`).
A dry run is safe to repeat.

If the answer is lost (`remote.outcome_unknown`), do not guess: wait, run `graph-admin graph
show <graph>`, then run the same release again. It waits for the lock the running migration
holds, then finds it done or finishes it.

## After the release

- Tell the user that agents using the graph get fresh skills with their next `graph start`, and that
  older revisions are shown under the new names, so `graph history` and `--as-of` keep working
  with the new terms; scripts that name an old term get `term.unknown` with the new name in the
  message.
- Shapes in the usage report (`graph-admin usage report <graph> --since <date>`) from before
  the migration keep the old names.
