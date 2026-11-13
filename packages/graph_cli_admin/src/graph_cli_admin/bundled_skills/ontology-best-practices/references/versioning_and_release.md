# Versioning and release

How a change to an ontology is classified, tried and released.

## Change levels

`graph-admin ontology diff` classifies every change; the version bump of
the new directory must be at least the highest level, and `release`
refuses less. A larger bump than needed is a warning.

| Level | Changes |
| --- | --- |
| patch | summaries, descriptions, annotations, mappings, `display_prefix`, order of enum values, clearing `deprecated` |
| minor | new class, attribute (optional), relation, qualifier (optional), enum value, datatype, enum, lifecycle, state (not initial), transition, import; setting `deprecated`; loosening: `required` or `unique` off, a facet, a cardinality, a wider `domain` or `range`, a looser mutability; `abstract` off; a new `role`; a changed `default`; removing a guard or a characteristic; a new or changed warning rule; removing a warning rule; a rule changed from error to warning; `on_target_inactive` changed to `warn` or `allow` |
| major | removing or renaming any term; a new required attribute or qualifier; tightening: `required` or `unique` on, a facet, a cardinality, a narrower `domain` or `range`, a stricter mutability; `many` changed either way; a changed type, base type or parent class; `abstract` on; removing or changing a `role`; a new or changed guard; a new characteristic; a new initial state, changed initial or terminal states, removing a state or transition; changed `inverse_label`, `prefix` or `namespace`; removing an import; a new, changed or removed error rule; a rule changed from warning to error; `on_target_inactive` changed to `refuse` |

Renames are recognized only with `renamed_from`; without it a rename is a
removal plus an addition. `ontology diff` and `ontology release` refuse a
`renamed_from` that does not fit the pinned release (`ontology.rename_invalid`:
the old term does not exist, still exists, the new term existed already, two
terms name the same old one, or the old term is a `core:` or built-in module
term); one the previous release applied already only warns
(`ontology.rename_stale`), so a copied version directory still releases.

## Major versions on a graph with data

A major release on a graph that holds data runs a batch check of all data
under the new version and is refused if any error remains; the refusal
lists the first findings. Without a migration a major change therefore
passes only when the existing data already fits it. A release can carry a
`migration.yaml` that rewrites the data in place (renames, moves between
modules, value changes): see [migrations](migrations.md); the check then
judges the data after the migration.
`graph-admin graph check <graph> --ontology <dir>` runs the same check
without releasing and also lists the warnings (and previews a migration).
Without a migration, in practice:

- **Removing or renaming a term** that data uses fails (the data still has
  the old name). Deprecate it in a minor release, move the data to the
  replacement with normal writes, then remove it in a major release.
- **Tightening** (`required`, a cardinality, an error rule) passes only
  when every entity already satisfies it. Fill the gaps first, then
  release.
- **Moving text to a language** (`string` to `lang_string`, `text` to
  `lang_text`) fails while values are stored without a tag. Add a new
  `lang_string` attribute in a minor release, write the values there
  (untagged ones take the graph language), deprecate the old attribute,
  then remove it in a major release.
- **When the data cannot fit**, keep the term and narrow the new rule with
  `if`, so it applies to new entities only (minor or major depending on
  the change, but without existing errors).

## Deprecation

1. Minor release: set `deprecated: true`, name the replacement in the
   summary and add the replacement. `graph describe` and generated graph
   skills mark the term as deprecated, and a write that uses it gets the
   warning `data.structural.deprecated_term` with that summary.
2. Move existing data with `graph update`, `graph link`, `graph unlink` or
   `graph supersede`; tell the graph skill to stop using the term.
3. Major release: remove the term once no active data uses it.

## Releasing a modular graph

A graph whose ontology has several modules is released as one bundle; the
version of the bundle is the version of the main (umbrella) module.

- The version directory holds every module file, also those that did not
  change. A module file is never left out of a new version directory.
- Name the main module wherever a directory with several modules is
  given: `--module <main>` on `ontology validate`, `ontology diff`,
  `ontology release`, `graph-admin graph create` and `graph-admin graph check` (with its
  `--ontology` option); without it these commands refuse with
  `usage.missing_argument`. `graph-admin graph push` takes it from the
  pulled base.
- A change in any module needs at least the same bump of the main module.
  `ontology diff` classifies the whole bundle; the main module's version
  must follow it, and a patch-level edit of an area is a patch bump of the
  main module. Bump the area's own version too: `ontology diff` and
  `ontology release` warn with `admin.module_bump_insufficient` when it does
  not cover the changes to the area's terms (the release still goes on).
- Adding a module is a minor change: a new file plus a new import in the
  main module (or in the module that builds on it).
- Moving a term to another module changes its CURIE. Without a hint
  `ontology diff` sees a removal plus an addition. A class or relation
  names its old CURIE in `renamed_from` (`renamed_from: old:Name`, kept as
  written, no import needed) and the diff reports a rename; a local name
  still means the same module. Either way it is a major release.
- Removing an import is major.
- After the release, check what agents will see: `graph describe modules`
  and `graph describe module <module>`, and the generated graph skill
  (`graph-admin graph skill render`).

## Trying an ontology on a throwaway graph

Before the first release and before every release with new structure or
rules, try the version on an empty data root. Work in a directory outside
any project with a `.graph_cli.toml` pointer, so `graph` is not bound to
the project's graphs:

```sh
export GRAPH_CLI_ROOT="$(mktemp -d)/root"
graph-admin init
graph-admin graph create <graph>_probe --ontology <dir>
graph apply <sample.jsonl> --graph <graph>_probe
graph find --type <type> --graph <graph>_probe
```

`graph-admin` runs from the graph_cli checkout (`uv run --project
<checkout> graph-admin ...`). Then:

- load sample data shaped like the real data, as change sets;
- answer every competency question with `graph find`, `graph neighbors`
  and `graph lookup`;
- make every rule and guard fire once;
- for a change to a live graph, restore a backup copy into the throwaway
  root instead and release the new version there first.

Delete the throwaway root afterwards. Never point `GRAPH_CLI_ROOT` at the
real data root for a probe.

## Releasing

First version of a graph:

1. `graph-admin ontology validate <dir>`: no findings.
2. `graph-admin graph create <graph> --ontology <dir> --title "<title>"
   --language <tag>`. The language of the values is fixed at create.

Later versions:

1. Copy the released directory to the new version and edit the copy.
2. `graph-admin ontology validate <dir>`: no findings.
3. `graph-admin ontology diff <dir> --graph <graph>`: read every change and
   its level; set `module.version` accordingly.
4. `graph-admin graph check <graph> --ontology <dir>`: no errors; read
   the warnings (batch rules and `in` cardinality run nowhere else).
5. Back up the data root and check the backup (see the backup notes in the
   graph_cli documentation, `docs/files.md`).
6. `graph-admin ontology release <graph> <dir>`; a refusal lists what the
   data lacks.
7. `graph-admin audit verify`: clean.
8. The graph skill: a template skill renders the new model by itself;
   reinstall it with `graph skills install`. Check its curated text with
   `graph-admin graph skill render`, and publish again when the text or a
   static skill must change (see `docs/skills.md`).
9. Update the README next to the ontology: new questions, design choices,
   what is still open.
