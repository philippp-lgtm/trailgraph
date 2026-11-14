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

## Aliases

A migrated entity keeps its alias. A class that moves (a `renamed_from` CURIE, or a `retype`
whose `from` class is gone and whose `to` class has no prefix yet) takes the alias prefix of
its old name and replaces that entry in the graph's prefix map, so new entities of the class
continue the numbering. A prefix is never freed: a class that is removed without a move keeps
its prefix reserved.

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

Before step 4, check what a rename does around the data: `graph-admin ontology diff <dir> --graph
<graph>` lists per renamed term the functions, actions and policy rules that name the old one.
A deny rule (`types: {T: none}`, a `deny` entry) that names only the old term is refused
(`admin.policy_renamed_term`): apply a policy naming both terms first, release, then drop the
old name. Published functions whose files name the class by its bare name (and a `module:`
line) are compiled again and published with the release; those that name the old CURIE or a
renamed attribute are fixed in the same push (`admin.functions_broken`).

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
