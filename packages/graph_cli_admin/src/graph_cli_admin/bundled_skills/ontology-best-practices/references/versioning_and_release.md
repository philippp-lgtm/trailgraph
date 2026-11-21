# Versioning and release

How a change to an ontology is classified, tried and released.

## Change levels

`graph-admin ontology diff` classifies every change; the version bump of
the new directory must be at least the highest level, and `release`
refuses less. A larger bump than needed is a warning.

| Level | Changes |
| --- | --- |
| patch | summaries, descriptions, annotations, mappings, `display_prefix`, order of enum values, clearing `deprecated` |
| minor | new class, attribute (optional), relation, qualifier (optional), enum value, datatype, enum, lifecycle, state (not initial), transition, import; setting `deprecated`; loosening: `required` or `unique` off, a looser or removed facet, a cardinality, a wider `domain` or `range`, a looser mutability; `abstract` off; a new `role`; a changed `default`; removing a guard or a characteristic; a new or changed warning rule; removing a warning rule; declaring a marking; removing a marking from an attribute (`remove_marking`); a rule changed from error to warning; `on_target_inactive` changed to `warn` or `allow` |
| major | removing or renaming any term; a new required attribute or qualifier; tightening: `required` or `unique` on, a new facet, a tighter facet limit, a changed `pattern`, a cardinality, a narrower `domain` or `range`, a stricter mutability; `many` changed either way; a changed type, base type or parent class; `abstract` on; removing or changing a `role`; a new or changed guard; a new characteristic; a new initial state, changed initial or terminal states, removing a state or transition; changed `inverse_label`, `prefix` or `namespace`; removing an import; a new, changed or removed error rule; a rule changed from warning to error; adding a marking to an existing attribute (`add_marking`); removing a marking declaration (`remove_marking_term`); `on_target_inactive` changed to `refuse` |

## Renames

Renames are recognized only with `renamed_from`; without it a rename is a
removal plus an addition. A local name in `renamed_from` means the same
module; for a class or relation the CURIE of the old term
(`renamed_from: old:Name`, kept as written, no import needed, the old module
may be gone) moves it to another module. `ontology diff` and `ontology release` refuse a
`renamed_from` that does not fit the pinned release (`ontology.rename_invalid`:
the old term does not exist, still exists, the new term existed already, two
terms name the same old one, or the old term is a `core:` or built-in module
term); one the previous release applied already only warns
(`ontology.rename_stale`), so a copied version directory still releases.

## Markings

A marking hides an attribute from every reader who does not hold it: for any other reader the
attribute reads as if the type did not declare it, and a policy grant names the markings a role
holds (`maintaining-graphs`, `references/give_an_app_a_role.md`). For the release it means:

- **Levels** are in the table above. A new attribute declared with a marking is an ordinary new
  attribute (minor); declaring a marking (`add_marking_term`) is minor too. `add_marking` on an
  existing attribute is major because for readers without the marking the attribute disappears,
  like a removed one, and functions, actions and skills that read it change. `remove_marking` is
  minor, but the values, history included, become readable for every reader that sees the type:
  the release warns `admin.marking_removed` and names the markings that remain. A declaration can
  be removed (`remove_marking_term`, major) only once no attribute uses it. A marking that moves
  to another module has another CURIE: it shows as `remove_marking` plus `add_marking`.
- **Compiled form.** A model that uses markings compiles to `graph_cli.compiled_ontology/2` with
  `requires: ["markings"]`; a model without them stays `/1`, byte for byte. Programs before 0.23.0
  refuse `/2` (`format.unsupported_version`), so update the servers and clients before the first
  release that marks an attribute: older ones would otherwise show it.
- **A marked name is never reused unmarked.** Old revisions keep the names they stored, so a
  name that was marked once must not turn up in a new version without the marking as another
  attribute of the data. The release is decided by the name map, history included: for every
  released version of the graph, a value stored under a marked attribute is followed to where
  the new version reads it. It is refused (`ontology.marking_name_reused`, `details.reuses`
  lists version, type, attribute and where it lands) when it lands in an attribute with weaker
  markings and nothing in the diff links the two: a marked name dropped and declared again
  unmarked, an implicit retype carry (marked `A.x` retyped to `B`, which declares an unmarked
  `x`), a marked parent attribute redeclared unmarked lower in the hierarchy. A deliberate
  downgrade, visible as `remove_marking`, a `renamed_from` or an explicit retype attribute map, is
  allowed and warns `admin.marking_removed` ("including history"). A name that returns
  unmarked while every old value maps away by a rename warns
  `ontology.marking_name_returned`: maintainer surfaces still show the old names. The check fails
  closed while markings are in play: a release of the graph's history that cannot be read, or an
  incomplete pin history, refuses with `details.unreadable`. A graph with no trace of markings is
  not judged. Rename a marked attribute with `renamed_from` and keep its markings; give a new
  meaning a new name.
- **Other warnings** (state based, shown by `graph-admin ontology release`, `graph-admin graph push` and
  `graph-admin ontology diff --graph`; the first also by `graph-admin policy check` and
  `graph-admin policy apply` of the installation policy, the last also by
  `graph-admin graph actions publish`): `admin.marking_ungranted` (a marking in use is granted by no
  installed grant; only maintainer surfaces see those attributes, and without an installation policy
  no app caller can read or write them), `admin.policy_unknown_marking` (an installed grant names
  a marking the version does not declare), `admin.marking_blocks_create` (a required
  attribute without a default is marked), `admin.marking_in_expression` (a rule, lifecycle guard
  or action precondition names a marked attribute). Fix each one or record why it stays.

## Major versions on a graph with data

A major release on a graph that holds data runs a batch check of all data
under the new version and is refused if any error remains; the refusal
lists the first findings. `graph-admin graph check <graph> --ontology <dir>`
runs the same check without releasing and also lists the warnings.

Two ways through:

- **With a migration** (the data is rewritten in place, ids, aliases and
  history stay): a `migration.yaml` in the version directory carries the
  value changes, `renamed_from` the renames and moves between modules, and
  the check judges the data after the migration. Required whenever the
  release rewrites any revision, renames included. Format, steps and a worked
  module split: [migrations](migrations.md). Use it for a rename, a move to
  another module, a merged class, a new required attribute, a removed enum
  value or state.
- **Without one** the major change passes only when the existing data
  already fits it, so prepare the data first:
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
    then remove it in a major release; or let a migration `convert` it.
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
- Moving a term to another module changes its CURIE; name the old one in
  `renamed_from` ([Renames](#renames)), else the diff sees a removal plus an
  addition. Either way it is a major release, and on a graph with data it
  needs a `migration.yaml` (its `steps` may be empty): the entities move
  with their ids and aliases
  ([migrations](migrations.md#a-module-split-step-by-step)).
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

Run `graph-admin` as for the real graphs (from a checkout:
`uv run --project <checkout> graph-admin ...`). Then:

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
   the warnings (batch rules and `in` cardinality run nowhere else). With a
   `migration.yaml` it is also the preview of what the migration changes.
5. Back up the data root and check the backup (graph-admin has no backup command;
   ask the user, and see `maintaining-graphs`, `references/move_stored_files.md` for
   graphs whose stored files live in an object store: back up the data root first, the
   bucket after it).
6. `graph-admin ontology release <graph> <dir> --reason "<why>"` (add
   `--dry-run` first for a release with a migration); a refusal lists what the
   data lacks. In a maintainer workspace `graph-admin graph push` does this
   step and publishes a changed skill, functions and actions in the same run
   (`maintaining-graphs` skill). An interrupted migration is finished by
   running the same release again (`maintaining-graphs`,
   `references/release_with_a_migration.md`).
7. `graph-admin audit verify`: clean.
8. The graph skill: a template skill renders the new model by itself;
   installed copies go stale and the next `graph start` of an agent
   refreshes them (`graph skills install` does too). Check its curated text with
   `graph-admin graph skill render`, and publish again when the text or a
   static skill must change (`authoring-graph-skills`).
9. Update the README next to the ontology: new questions, design choices,
   what is still open.
