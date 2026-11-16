# Migrations: moving the data to a new major version

A major release on a graph with data is refused while the data does not fit the new
version (`admin.migration_required`). A migration closes that gap: the release carries a file
`migration.yaml`, and the release rewrites the stored data in place to the new names and shapes.
Entity ids, aliases, history, edges and the audit trail stay; every migrated entity gets one new
revision, written by the maintainer under `maint.migrate`. Old revisions keep what they stored.

## What the ontology says and what the file says

Renames come from the ontology, values and shapes from the file.

- **Renames.** `renamed_from` on a class, relation or attribute is the one source. It is a
  local name for a rename inside a module, and for a class or relation it may be the CURIE of
  the old term (`renamed_from: old:Task` on `new:Task`) to move it to another module. The data
  follows with no step, and the alias prefix moves with the class.
- **The file** says what the ontology cannot: a value that no longer exists, a new required
  attribute, a merged class, data to drop.

The file is required whenever the release rewrites any revision, also when the only change is a
rename (then `steps` may be empty): `from`, `summary` and the file itself are the explicit
opt-in. Without it the release is refused with a hint to the file and to the preview.

## Renames and moves across modules

`renamed_from` is the one source of renames, for classes, relations and attributes. A local
name (`renamed_from: Task`) means the same module; for a class or a relation the CURIE of the
old term (`renamed_from: old:Task`, kept as written, no import needed, the old module may be
gone) moves it to another module. The diff then reports a `rename` (major) instead of a removal
plus an addition, the data follows with no step, and the alias prefix moves with the class.

`ontology diff` and `ontology release` refuse a `renamed_from` that does not fit the pinned
release (`ontology.rename_invalid`, every problem with its path): the old term does not exist,
it still exists in the new version, the new term existed in the old one (the two would merge),
two terms name the same old one, or the old term is a `core:` or built-in module term
(`files:`). A `renamed_from` the previous release applied already is only the warning
`ontology.rename_stale`: remove it from the sources.

Enum values, states, qualifiers, datatypes, enums and lifecycles have no `renamed_from`. An enum,
datatype or lifecycle that moves to another module shows as a removal, an addition and a
`change_type` on every attribute that uses it, all major; the stored values still fit, so the
move needs no step (unless a value goes away: `map_values`).

## The file

```yaml
format: graph_cli.migration/1
from: 1.1.0
summary: Worn items count as good; the supplier class is merged into the vendor class.
steps:
  - map_values: {type: inv:Item, attribute: condition, values: {worn: good}}
  - fill: {type: inv:Item, attribute: quantity, value: "1"}
  - retype: {from: inv:Supplier, to: inv:Vendor, attributes: {title: name}}
```

`from` must be the version the graph is pinned to (`admin.migration_from_mismatch`): a graph
several majors behind releases them one by one. A migration on a minor or patch release is
`admin.migration_unexpected`. `summary` is required and shown in the history and the audit
trail. The steps apply in order, after the renames the ontology derives. In a step, old names
are names of the pinned release and new names are names of the release being made: `retype.from`,
`drop` (the attribute), `drop_edges`, `drop_qualifier` and the keys of `map_values` and
`map_qualifier_values` are old, everything else is new.

| Step | Keys | Effect |
| --- | --- | --- |
| `retype` | `from`, `to`, optional `attributes` `{old: new}` | every entity of exactly class `from` gets class `to` (subclasses are not touched), attributes renamed as given; answers a removed class whose data joins another class |
| `fill` | `type`, `attribute`, `value` | sets a constant where the attribute is absent, subclasses included; answers a new required attribute |
| `map_values` | `type`, `attribute`, `values` `{old: new}` | rewrites enum values or lifecycle states, single or `many`; answers a removed enum value or state |
| `map_qualifier_values` | `relation`, `qualifier`, `values` | the same for enum qualifiers on edges |
| `drop` | `type`, `attribute` | removes stored values; they stay in the history |
| `drop_edges` | `relation` | removes every edge of a relation; the history keeps them |
| `rename_qualifier`, `drop_qualifier` | `relation`, `from`, `to` / `qualifier` | qualifier names on edges |
| `convert` | `type`, `attribute`, `to` | a fixed table: `string` to `lang_string` and `text` to `lang_text` (tagged with the graph language), single to `many`, `many` to single (only where at most one value) |

Refused when the file is checked (`migration.invalid`, with the path of every problem): an
unknown step or key, a term missing from the side it belongs to, a `retype.from` that is also
renamed, an abstract `retype.to`, a `core:` or built-in module term in any step, `convert`
without a graph language. A step that matches nothing in the data is the warning
`admin.migration_step_unused`.

Steps bypass lifecycle transitions, guards and immutability, because they are maintenance;
the data is judged by the full check under the new version afterwards, so a result that
does not fit is `admin.migration_required` and nothing is written. Entities that are retracted
or superseded are migrated too, with their status kept. An entity that a migration does not
change gets no revision.

Not part of the file: conditional steps, computed values, copying between attributes, splitting
one class by a value, retyping single entities, and CURIE renames of enums, datatypes and
lifecycles. If a release needs one of them, keep the old term, add the new one and move the data
with normal writes.

## Marked attributes

Old revisions keep their stored names and a migration rewrites only the current ones, so the
release checks where the values of every marked attribute of every earlier version end up
(the name map, history included: `references/versioning_and_release.md`, "Markings").

- A `renamed_from` on a marked attribute keeps its markings: the values follow the rename and
  stay hidden for the same readers, history included.
- A `retype` carries an attribute by name unless its `attributes` map says otherwise. A marked
  attribute carried that way into a class that declares the name without all its markings is
  refused (`ontology.marking_name_reused`); map it to an attribute with the same markings, or to one with
  weaker markings on purpose (the release then warns `admin.marking_removed`, "including
  history").
- `drop` removes the current values; the history keeps them, still hidden. Declaring the name
  again unmarked is refused.
- A release that cannot read an earlier version of a graph with markings is refused
  (`details.unreadable`): restore or repair the history first.

## Aliases

A migrated entity keeps its alias. A class that moves (a `renamed_from` CURIE, or a `retype`
whose `from` class is gone and whose `to` class has no prefix yet) takes the alias prefix of
its old name and replaces that entry in the graph's prefix map, so new entities of the class
continue the numbering. A prefix is never freed: a class that is removed without a move keeps
its prefix reserved.

## A module split, step by step

A graph with one module `workshop` (prefix `wk`) holds projects, tasks, budgets, tools, gadgets
and manuals (a subclass of `files:File`), with the relations `belongs_to` (task to project) and
`uses` (task to tool). Version 2.0.0 splits it into the main module `workshop`, which keeps a few
classes and brings the areas together, and two areas: `planning` (prefix `plan`) and `equipment`
(prefix `equip`). The gadgets become tools.

1. Copy the version directory to `2.0.0/`. It holds a file per module, also those that did not
   change. Bump the main module to 2.0.0; the areas start at their own `1.0.0`. The main module
   imports both areas; an area that needs another imports it (no cycles).
2. Move each class and relation by naming its old CURIE; the move keeps the display prefix, so
   the aliases stay (`TASK-0001` is still `TASK-0001`):

   ```yaml
   # planning.yaml
   classes:
     Task:
       summary: A step of a project.
       display_prefix: TASK
       renamed_from: wk:Task
       attributes:
         priority: {summary: How soon the task is to be done., type: Priority, required: true}
   relations:
     belongs_to:
       summary: The project that the task is a step of.
       renamed_from: wk:belongs_to
       domain: Task
       range: Project
   ```

   A `files:File` subclass moves the same way (`renamed_from: wk:Manual` in `equipment.yaml`);
   its stored content, the edges that cite it and their qualifiers stay.
3. Merge the gadgets into the tools: `equip:Tool` has `renamed_from: wk:Tool`, `wk:Gadget` is
   gone, and a step moves its entities (their aliases `GADG-0001` stay and keep resolving):

   ```yaml
   # migration.yaml
   format: graph_cli.migration/1
   from: 1.0.0
   summary: Split the workshop into planning and equipment; the gadgets are tools now.
   steps:
     - retype: {from: wk:Gadget, to: equip:Tool, attributes: {title: name}}
     - fill: {type: plan:Task, attribute: priority, value: normal}
     - map_values: {type: plan:Project, attribute: phase, values: {paused: running}}
   ```

   `fill` answers the new required attribute `priority`, `map_values` the enum value `paused`
   that the moved enum no longer has.
4. `graph-admin ontology diff <dir> --module workshop --graph <graph>` lists every rename and
   the policy rules, functions and actions that still name an old term. Fix the problems
   it names first ([Functions and actions](#functions-and-actions)).
5. Apply a policy that names old and new terms of every hidden type or denied term (below), if
   there is one.
6. Preview and release as under [Releasing](#releasing), with the version directory as it is.
   One `graph-admin graph push` carries the ontology, the migration, the functions and the skill.
7. Afterwards the ids, aliases, edges and history of every entity are the ones before, now
   read under the new names; `TASK-0004` is the next task, because the prefix moved with the
   class.

## Releasing

1. Back up the graph first (a migration rewrites data; the log keeps the old revisions, but
   a restore point is cheaper than a repair).
2. `graph-admin graph pull <graph> <workspace>`, copy the version directory to the new
   version, edit the modules (with `renamed_from` where terms are renamed or moved) and write
   `migration.yaml` next to them.
3. Preview: `graph-admin graph check <graph> --ontology <dir>` applies the migration to an
   overlay and prints, per derived rename and step, the entities and edges changed, by type,
   with three sample ids, and the findings that remain. `graph-admin ontology release <graph> <dir> --dry-run` and
   `graph-admin graph push <graph> <dir> --dry-run` show the same block and also check the published functions and actions.
4. `graph-admin ontology release <graph> <dir> --reason <why>` (or `graph-admin graph push`). The migration
   is one transaction under the graph lock, committed before the pin switches; the answer
   reports the entities, edges and entities per step, the transaction's seq and the duration.
   Writers wait for the lock: run a large migration in a quiet moment. Over the remote mode the
   release is one request that answers when the migration is done: raise `[remote] timeout_s`
   and the ingress read timeout above the duration (about 7 seconds for 6,300 entities in a
   benchmark; time grows with the entities migrated). If the answer is lost
   (`remote.outcome_unknown`), run the same command again: it waits for the running migration,
   then finds it done or finishes it, and never migrates twice.
5. `graph-admin audit verify` must report no errors.

## Functions and actions

Check what a rename does around the data before step 4 of the release: `graph-admin ontology
diff <dir> --graph <graph>` lists, per renamed term, the functions, actions and policy rules
that name the old one.

- **Deny rules.** A rule that hides or protects a term by its old name applies to nothing once
  the term is renamed, so it would fail open. A deny rule (`types: {T: none}`, a `deny` entry,
  a glob the new name leaves) that names only the old term is refused
  (`admin.policy_renamed_term`; `ontology diff --graph` warns
  `admin.policy_renamed_term_affected`): apply a policy naming both terms first (a policy may
  name terms that do not exist yet), release, then drop the old name. Allow lists (`reads`,
  `writes`) fail closed and only warn.
- **Functions.** A published function is stored with resolved names. After a release with
  renames, those whose source names classes and relations bare (and, with several modules, has a
  `module:` line that exists in both versions) are compiled again and published with the
  release (the same sources, a new compiled document). Those that name an old CURIE or a
  renamed attribute are `admin.functions_broken`: fix them in `functions/` and push them with
  the ontology. A function without a `module:` line must get one in the same push as soon as the
  release makes the graph multi-module (`function.module_required`); name a module that exists
  before and after the split (the main module, even when it holds no terms of its own: a reader
  with read limits still sees the function while what it touches is visible).
- **Actions** name their classes by CURIE, so an action that names a class that moves never
  follows (`admin.actions_broken`): fix its file in the same push.

Only maintainers may migrate (`maint.migrate`); a project admin's release that migrates is
refused with `access.ceiling`. The file is part of the release: the same version with another
file is `admin.release_exists`.

## An interrupted release

A crash leaves either the old state or the incomplete one, in which the migration
transaction is committed but the pin is not switched. There, reads use the new version's names
(warning `graph.migration_incomplete`), writes are refused (`write.migration_incomplete`),
`audit verify` warns, and any other release is refused (`admin.migration_incomplete`). Run
the same release again: it finds the data migrated, checks it and switches the pin. After a
lost answer in the remote mode do the same: wait, then run the release again.
