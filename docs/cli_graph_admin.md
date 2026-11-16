# `graph-admin`: the maintenance CLI

`graph-admin` is run from the repository checkout (`uv run graph-admin ...`)
and is not installed on `PATH`. On a machine in remote mode it is installed
from the server's `/v1/dist/` ([Self update](server.md#self-update)) and
sends its commands to the server's admin API
([Server](server.md#remote-mode-of-graph-admin)). An agent maintains graphs
from a [maintainer workspace](#maintainer-workspaces) with the admin
skills that ship with `graph-admin`. Output and exit codes are those of `graph`
(see [Output and exit codes](cli_graph.md#output-and-exit-codes)).
`graph-admin --help` and `graph-admin <command> --help` show every option;
`graph-admin describe commands --json` lists every command as data
(`graph_cli.inventory/1` with `surface: "maint"`); `--output-schemas` adds the
schema of every answer, as for `graph` (see [`graph`](cli_graph.md)).

## Commands

| Command | What it does |
| --- | --- |
| `describe commands` | every command as data: arguments, options, examples, error codes, exit codes |
| `init` | create the data root and, if missing, `config.toml` with a device id |
| `graph create NAME --ontology DIR` | create a graph pinned to the first release of an authored ontology; `--language TAG` sets the BCP 47 default language of its values (canonical case, fixed after create) |
| `graph list`, `graph show REF` | list graphs with title and language (a project admin: the graphs it maintains); show manifest, head, installed releases, the published skill and the count and hash of the published functions and actions |
| `graph check REF` | check every entity against the pinned ontology, including what no write checks (rules with `when: batch`, `in` cardinality) and names shared by two active entities of one type family (warning `data.semantic.possible_duplicate`, once per name; only `same` names, a near spelling is reported on the write that adds it); exit 1 (`admin.data_errors`) on error findings, warnings are listed; also checks the published functions against the model (`admin.functions_broken`, for example `function.stale` after an interrupted push) and the published actions (`admin.actions_broken`, for example `action.stale`; the functions are reported first); `--ontology DIR` checks against an authored version before it is released, and previews its migration when it has one ([below](#releasing-with-a-data-migration)) |
| `graph reindex REF` | rebuild the revision files, the checkpoint and the index of a graph from its transaction log (`--dry-run`, `--reason`), audited as `maint.reindex`; for derived files that `audit verify` reports as different from the log, or reads that warn `storage.index_stale` |
| `graph pull REF DIR` | write the authored sources of a graph into a workspace directory: its pinned release (`--version V`: another installed one), its published skill, functions and actions, byte for byte, with the base `push` checks (`--force` replaces an earlier pull); changes nothing in the data root ([Workspaces](#workspaces-pull-and-push)) |
| `graph push REF DIR` | release and publish what changed in a pulled directory, under one lock; refused when the graph changed since the pull (`--dry-run`, `--reason`); a `migration.yaml` in the new version directory migrates the data ([below](#releasing-with-a-data-migration)) |
| `workspace sync DIR` | set up or refresh a [maintainer workspace](#maintainer-workspaces): the admin skills in `DIR/.claude/skills/`, `DIR/CLAUDE.md` when there is none, and a pull of every graph that has no folder yet (`--dry-run`, `--force` for the skills) |
| `graph skill publish GRAPH DIR` | publish a skill directory or a template as the graph's skill (`--dry-run`, `--reason`); a template over its [budget](skills.md#budgets) is published with the warning `admin.skill_over_budget` |
| `graph functions publish GRAPH DIR` | publish a folder of function files (`<name>.yaml`, [`graph_cli.function/1`](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/authoring-graph-functions/references/format.md), the format reference of the `authoring-graph-functions` admin skill) as the graph's function catalog, checked against the graph's ontology pin; every problem is reported together (`admin.functions_invalid`); an empty folder unpublishes all (`--dry-run`, `--reason`) |
| `graph functions show GRAPH` | the published function catalog: hash, ontology pin it was published against, and one line per function |
| `graph actions publish GRAPH DIR` | publish a folder of action files (`<name>.yaml`, [`graph_cli.action/1`](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/authoring-graph-actions/references/format.md), the format reference of the `authoring-graph-actions` admin skill) as the graph's action catalog, checked against the graph's ontology pin; every problem is reported together (`admin.actions_invalid`); an action may not be named like a published function, nor a function like a published action (one namespace per graph); an empty folder unpublishes all (`--dry-run`, `--reason`) |
| `graph actions show GRAPH` | the published action catalog: hash, ontology pin it was published against, and one line per action |
| `graph skill render GRAPH DIR --out OUT` | preview: write what `graph skills install` would install into `OUT/graph-<graph>/` and report what publish would refuse (`--force`) and the size against the [budgets](skills.md#budgets) (`data.size`, warning `admin.skill_over_budget`); changes nothing in the data root |
| `ontology validate DIR` | compile and check an authored ontology |
| `ontology diff DIR --against DIR` / `--graph REF` | classify the changes and check the version bump; a `renamed_from` that does not fit the older version is `ontology.rename_invalid` (a leftover the previous release applied already is the warning `ontology.rename_stale`); with `--graph`, warns `admin.functions_affected` and `admin.actions_affected` for the published functions and actions the release would break, `admin.policy_renamed_term_affected` for deny rules that would fail open, and lists `references` (where functions, actions and policy name each renamed term) and the catalogs the release would publish again (`republished`) |
| `ontology release GRAPH DIR` | publish a release into the graph and pin the graph to it; classes the release adds get their `display_prefix` as alias prefix (a prefix stays with its entities: a class renamed with `renamed_from` (a local name, or the CURIE of the old term when it moves to another module) takes the prefix of its old name and replaces that entry, a removed class keeps its prefix, and a prefix another class already holds is not given out, with the warning `admin.alias_prefix_taken`); every published function and action must still resolve under the new release, on every bump, else `admin.functions_broken` or `admin.actions_broken` ([Auditing and releases](#auditing-and-releases)), unless the release has renames and they can be compiled again (`republished`); a deny rule that names a renamed term and no new one is `admin.policy_renamed_term`; a major release may carry a data migration (`migration.yaml`, [below](#releasing-with-a-data-migration)); `--dry-run` makes every check and shows the migration without writing, `--reason` is recorded with the release and the migration |
| `audit verify` | check every chain, tx log, revision file, index, manifest pin, installed policy and blob location ([Blob location](audit.md#blob-location)); exit 1 on error findings |
| `audit anchor` | record every graph stream's head in the installation stream |
| `usage report [GRAPH] [--since DATE]` | read the access log (needs `[audit] access_log = true`; else `access_log: false`) per graph, or for `GRAPH` only: function runs (calls, distinct sessions, errors by code), `never_run` (published functions without a run), `function_share` (whole percent of runs among runs and queries), query `shapes` ranked by distinct sessions, and three lists derived from them: `discovery_misses` (a covered shape still queried), `batch_gaps` (the covering function takes one value where the query took a list) and `candidates` (uncovered shapes in at least two sessions, the input for new functions). Each list shows 20 entries, `<list>_more` counts the rest. `--since` takes a date (midnight UTC) or an RFC 3339 timestamp, inclusive. Shapes only, no values; sessions are counted, never listed. In remote mode the server's log is read through the admin API (`maint.audit_read`) |
| `files verify --graph REF` | check the stored files of a graph: missing (and, with `--rehash`, damaged) files are errors, orphans, staging leftovers and misplaced copies (`blob_misplaced`: a copy outside the location the graph's manifest names) info; for a graph in an object store one listing of its prefix (`--rehash` reads every object), and a store that cannot be reached or is another store is the error `blob_store_unavailable` instead of a stop; a local graph is checked for copies in the configured object store only when one is configured with credentials and reachable, and never fails because of it |
| `files gc --graph REF` | remove stored files no transaction names (in an object store only those older than 24 hours by the store's clock), staging files older than one hour and misplaced copies whose bytes the graph's location holds intact (same size, and its copy, read in full before the graph lock, has the hash; a damaged or unreadable copy there keeps the misplaced one, listed as `misplaced_kept`; a local graph's objects only in the store its audit trail records it moved away from, others are report-only) (`--dry-run`, `--reason`), audited as `maint.repair` ([what the event names](audit.md#blob-location)) |
| `files move REF... --to s3\|local` | move the stored files of graphs between the data root and the object store (`--all` for every graph, archived ones too; graphs already there are skipped and listed as `at_target`): copy passes while writes go on, then the switch under the graph lock, audited as `maint.graph_configure`; the old copies stay until `files gc` (`--dry-run` counts files and bytes, `--reason`; [Moving stored files](#moving-stored-files)) |
| `files store check` | check the configured object store and say in words what is wrong: the settings and credentials (never their values), the store marker (its id and the graphs of this installation that keep their files in it), listing, a probe write, read and delete below `graph_cli_probe/`, a missing key answering 404 (403 means the key lacks `ListBucket`), the clock against the store's `Date`, and redirects (endpoint, region, addressing); exit 1 (`admin.store_check_failed`) on an error, warnings (no marker yet, a drifting clock) do not fail; without a configured object store it says so and succeeds; authorized as `maint.audit_verify`, records no audit event |
| `policy check FILE [--graph G]` | compile an authored policy and report every problem; installs nothing |
| `policy show [--graph G]` | the installed compiled policy and its hash (the built-in one when none is installed) |
| `policy diff FILE [--graph G]` | rules and keys added, removed or changed against the installed policy |
| `policy apply FILE [--graph G] --reason R` | install a policy, audited as `maint.policy_change` (the installation policy also mirrored into every graph stream); refuses one that would lock you out; warns `admin.role_orphaned` when an installation policy drops a role that a token holds or a graph policy names; the installed policy again records missing mirrors |
| `policy explain --graph G [--project P] [--token NAME] [--role R]... [--action A] [--type T [--attribute N \| --relation REL] \| --marking M] [--file F]` | what an app caller from a project (or from none), a request with a token, or a server token that holds the roles `R` (with `--project` verified; excludes `--token`) would get, and by which rule; `maintains` says whether the caller may maintain the graph; `--attribute` or `--relation` (with `--type` and `--action`, excluding each other) explains one decision on an attribute or relation, with `type_decision`, the decision on the type, next to it; `--marking` (the marking CURIE, with `--action read.get` and without `--type`) explains whether the caller holds that marking; without `--action`, `writes` says what a write allow list lets the caller write, `reads` which types a caller with read limits sees and `markings` which markings the caller holds (the same as `graph whoami`; a project admin holds none, an installation admin all); records nothing; see [Roles](access_control.md#roles), [Write allow lists](access_control.md#write-allow-lists), [Read limits](access_control.md#read-limits) and [Markings](access_control.md#markings) |
| `token create NAME [--project P] [--expires DATE] [--scope S] [--role R]... [--reason R]` | create a bearer token for the server API, audited as `maint.token_create`; the token is shown only once and stored as a hash; `--scope admin` makes it a token of the [admin API](server.md#admin-api), with `--project` a [project admin](access_control.md#project-admins); `--role` (repeatable, app tokens only, declared by the installed installation policy, at most 16) gives it [roles](access_control.md#roles); warns when the policy grants an app token (with its roles) nothing or does not let an admin token maintain (a project admin: no graph) |
| `token list [--role R]` | the tokens: name, scope, project, roles, expiry, creation time, hash prefix; never the token; `--role` lists only the tokens that hold `R` |
| `token revoke NAME [--reason R]` | revoke a token, audited as `maint.token_revoke`; the server refuses it from the next request on |
| `token role add NAME ROLE... [--reason R]` | give an app token roles declared by the installed installation policy (`admin.role_unknown` otherwise), audited as `maint.token_update` with the token's full set of roles; changes nothing (no event, `changed: false`) for roles it holds; counts from the token's next request |
| `token role remove NAME ROLE... [--reason R]` | take roles from an app token, audited as `maint.token_update`; a role the policy no longer declares can be removed too; no event and `changed: false` for a role it does not hold |
| `enroll create --project P [--scope app\|admin] [--expires-in D] [--role R]... [--reason R]` | a one-time code a client exchanges (`graph connect`) for a token bound to `P`; shown once, valid 15 minutes or `D` (`<n>m`, `<n>h`, at most 24h), audited as `maint.token_create`; `--role` (repeatable, app scope only, declared by the installed installation policy) gives the token its roles; see [Enrollment](server.md#enrollment) |
| `enroll list` | the codes not used yet: id, project, scope, roles, expiry, creation time, hash prefix; never the code |
| `enroll revoke ID [--reason R]` | revoke an unused code, audited as `maint.token_revoke` |
| `self update` | in the remote mode install the server's build of `graph-admin` when it differs (`--check` only reports); see [Self update](server.md#self-update) |
| `skills install --scope user\|project` | install or refresh the [admin skills](skills.md#admin-skills) for an agent (`--dir`, `--force`, `--dry-run`); runs here, also in the remote mode |
| `skills check --scope user\|project` | report each admin skill as `fresh`, `stale`, `modified`, `unexpected`, `foreign`, `missing` or `unavailable` (`--dir`); exit 1 unless all are fresh |
| `view [--graph REF]` | serve a live view of the graphs in the browser until Ctrl+C or `SIGTERM`, opened on `REF` or on none (`--port`, `--host`, `--no-open`, `--token-file`, `--project`); no JSON mode (see [Live view](live_view.md)) |

## Releasing with a data migration

A major release on a graph with data can carry a migration: the data is rewritten in place to
the new names and shapes, every migrated entity getting one new revision, and its id, alias,
history, edges and the audit trail stay. The migration is part of the release, never a
separate command; its format and what to write in it is the
[migrations reference](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/ontology-best-practices/references/migrations.md)
of the `ontology-best-practices` skill. The app shows older revisions under the new names
([graph: Earlier revisions under current names](cli_graph.md#earlier-revisions-under-current-names)),
from the pin history and the installed release files, so a release directory stays where it is.
A function or action published with an old name is refused with its current name
("inv:Item was renamed to inv:Product in 2.0.0") in the problem's message and
`details.renamed`, also in a `graph push` that makes the release.

- **The file** is `migration.yaml` (`graph_cli.migration/1`: `from`, `summary`, `steps`) next
  to the module files of the version directory; `ontology release`, `graph push` and
  `graph check --ontology` read it. Renames come from `renamed_from` in the ontology; the
  steps say what the ontology cannot (values, new required attributes, merged classes, dropped
  data). It is required whenever the release rewrites any revision, renames included (steps may
  be empty); without it the release is `admin.migration_required` with a hint to the file and
  to the preview. It is installed with the release and part of its identity: the same version
  with another file is `admin.release_exists`.
- **Refusals** before anything is written: `migration.invalid` (the problems with their paths),
  `admin.migration_unexpected` (a minor or patch release), `admin.migration_from_mismatch`
  (`from` is not the pinned version: a graph several majors behind releases them one by one),
  `admin.migration_required` (the data still does not fit after the migration, or a rename
  rewrites data and there is no file). A step that matches nothing is the warning
  `admin.migration_step_unused`.
- **Authorization** is `maint.migrate`, decided with the release and pin before the lock
  (also for a dry run): maintainers only, a project admin's release that migrates is refused
  with `access.ceiling`.
- **Preview**: `ontology release --dry-run`, `graph push --dry-run` and
  `graph check REF --ontology DIR` apply the migration to an overlay and report, in the
  `migration` block of the answer, the counts per derived rename and per step (entities,
  edges, by type, three sample ids), the findings that remain and the steps that matched
  nothing. Nothing is written, not even a marker.
- **Policy and catalogs** follow or block. A release that renames or retypes a term is
  refused with `admin.policy_renamed_term` while an installed deny rule (a graph policy's
  `deny` entry, `types: {T: none}` or `read`, a glob such as `prefix:*`) names the old term and
  no deny rule names the new one for the same actions: it would apply to nothing, so it would
  fail open. Apply a policy that names both (a policy may name terms that do not exist yet),
  release, then drop the old names; allow lists (`reads`, `writes`) fail closed and only warn.
  An unfinished migration is completed whatever the policy says. Published functions and
  actions that no longer resolve are compiled again from their sources: when those compile
  cleanly against the new model (a function that names a class without a prefix, a
  relation that kept its name), the catalog is published again with the release, the same
  sources with a new compiled document, audited like a publish (`maint.graph_configure`,
  `change.functions.from` equal to `to`, only `compiled_hash` differs); the answer lists it in
  `republished`. Those that name an old CURIE, or an attribute that was renamed, stay
  `admin.functions_broken` / `admin.actions_broken` unless the same `graph push` brings the
  fixed files. A function without a `module:` line on a graph that the release splits into
  several modules needs that line (`function.module_required`), so it is fixed in the push
  too. A release without renames or migration file is unchanged. `ontology diff --graph`
  lists per renamed term the functions, actions and policy rules that name the old one
  (`references`; a function or action is `explicit` when its own file names the old CURIE) and
  the catalogs the release would publish again (`republished`); a deny rule the release would
  be refused for is the warning `admin.policy_renamed_term_affected`. `graph check --ontology`
  gives the same refusals and `republished`.
- **The release** runs under the graph lock: it installs the release, writes the migration
  marker, appends the `maint.migrate` intent (`--reason` recorded), commits the migration as
  one transaction, publishes the catalogs again, switches the pin and the alias prefixes, and
  removes the marker. The republish sits before the pin switch so that the incomplete state,
  which reads see under the target model, has catalogs compiled for it, and a release run
  again publishes what a crash left. The answer
  reports `migrated` (entities, edges, entities per step), the transaction's `tx_seq` and the
  duration. App writers wait for the lock, up to `lock_timeout_s`, and are judged by the new
  model when they get it; run a large migration in a quiet moment. Over a server the release is
  one request that answers when the migration is done: see [Timeouts](server.md#timeouts).
- **An interrupted release** leaves either the old state or the incomplete one, where the
  transaction is committed but the pin is not switched: reads use the target version's names
  (warning `graph.migration_incomplete` once no release holds the lock), writes are refused
  (`write.migration_incomplete`), `audit verify` warns, and every other release is refused
  (`admin.migration_incomplete`). Run the same release again: it finds nothing left to
  migrate, checks the data and switches the pin.

## Workspaces: pull and push

`graph pull REF DIR` writes what a graph was released and published from,
so it can be changed on any machine, also one in the remote mode:

```
DIR/ontology/<version>/<module>.yaml   the authored modules of the release
DIR/skill/graph-<graph>/...            the published skill (if the graph has one)
DIR/functions/<name>.yaml              the published functions (if the graph has any)
DIR/actions/<name>.yaml                the published actions (if the graph has any)
DIR/.graph_cli_source.json             the base: graph, ontology release, skill, functions and actions hash
```

Pull owns these five entries (the `functions` and `actions` folders as a whole): if any exists, it is refused
(`admin.source_exists`) unless `--force` replaces them. Everything else in
`DIR` stays (a README, notes, older version directories, `.git`).

To change the ontology, copy the version directory to a new one named
after the new version and bump the module version there; to change the
skill, edit `skill/graph-<graph>/`; to change the functions or actions, edit, add or
delete files in `functions/` or `actions/`. `graph push REF DIR` then, under the
graph's lock:

- refuses with `admin.source_conflict` when the graph no longer has the
  base, unless it already has what `DIR` holds (a push repeated after a
  lost answer is safe). Pull into another directory, carry the changes
  over and push from there;
- releases and pins the highest version directory when it is newer than
  the base (the rules of `ontology release`; a changed module under the
  base version is `admin.release_exists`), and publishes the skill
  directory when it differs (the rules of `graph skill publish`) and the
  `functions` folder when it differs, after the release (the rules of
  `graph functions publish`) and the `actions` folder the same way (the
  rules of `graph actions publish`); the order is release, skill, functions,
  actions. A missing folder is an empty one, so deleting every file
  unpublishes the catalog;
- checks the skill the graph will have against the ontology it will have
  whenever either changes, and the functions and actions the same way: the
  pushed files against the model the graph will have, so a release that
  breaks a function or an action and the fixed or deleted files go in one
  push. When the push does not touch the functions or the actions, the
  published ones are checked and a break refuses the release
  (`admin.functions_broken`, `admin.actions_broken`). Functions and actions
  share one namespace, checked against the names the graph will have: a
  name can move from `functions/` to `actions/` in one push, but not be in
  both. Every other rule is checked before the first write too;
- writes the new base into `DIR` (not with `--dry-run`).

Pull is authorized as `maint.graph_configure` on the graph; push as that
and, for a release, `maint.ontology_release` and `maint.ontology_pin`. Every
change is audited as by the single commands, with the push's `--reason`.

## Ontologies with several modules

An authored version directory holds one `<module>.yaml` per module (the name
`migration` is reserved: `migration.yaml` is no module, it is the migration file
of a major release), and a
graph's release is one bundle: the version of the bundle is the version of
the main module (the one that imports the others). When a directory holds
more than one module file, name the main module with `--module <module>` on
`ontology validate`, `ontology diff`, `ontology release`, `graph create` and
`graph check --ontology`; without it these commands refuse with
`usage.missing_argument`. `graph push` takes the main module from the pulled
base. The bump of the main module must cover every change of the bundle;
the own version of each other module is checked against the changes to its
terms, and one that does not cover them is the warning
`admin.module_bump_insufficient` on `ontology diff` and `ontology release`. The release rules (all module files in every version directory, the
bump of the main module, moving a term between modules) are in the
`ontology-best-practices` skill
([versioning](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/ontology-best-practices/references/versioning_and_release.md#releasing-a-modular-graph)),
as is how to cut a model into modules
([modeling](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/ontology-best-practices/references/modeling.md#splitting-into-modules));
what agents see of a modular graph is described in
[Skills](skills.md#blocks).

## Maintainer workspaces

`workspace sync DIR` turns a directory into a place an agent maintains
graphs from, on the machine with the data or on any machine in the remote
mode:

```
DIR/CLAUDE.md                    written when missing, never replaced: points at the skills
DIR/.claude/skills/<skill>/      the admin skills (as `skills install --scope project --dir DIR`)
DIR/<graph>_<id suffix>/         one folder per graph, as `graph pull` writes it
```

The suffix is the last six hex digits of the graph's id (the random end of
a UUIDv7), so graphs of one name from two installations never share a
folder. A folder belongs to a graph by its `.graph_cli_source.json`, not by
its name: a renamed folder is still found. Each run installs or refreshes
the skills and pulls every graph without a folder (archived ones are left
out), through `graph list` and `graph pull` with their authorization and
checks; the answer lists each graph with its `action`:

| Action | Meaning |
| --- | --- |
| `pull` | would be pulled (`--dry-run`) |
| `pulled` | pulled into its new folder |
| `present` | its folder holds the release the graph has pinned |
| `behind` | the graph has another release pinned than its folder was pulled from; pull it again with `graph pull --force` once the folder's edits are pushed |
| `conflict` | the folder name is taken by something that is not a pull of this graph; left as it is, the command ends with `admin.workspace_conflict` (exit 1) after doing the rest |
| `failed` | its pull was refused; `error` names the code, the command ends with that error |

A folder is never replaced or deleted. New graphs are drafted outside the
graph folders, created with `graph create`, and get their folder at the
next sync. The workflow for agents is the `maintaining-graphs` skill
([Admin skills](skills.md#admin-skills)).

## Moving stored files

`files move REF... --to s3|local` moves where graphs keep their stored files
(the data root or the object store configured in `[files.s3]`,
[Configuration](configuration.md)). For each graph it copies every file the
graph's transactions name and every other file stored there (orphans too),
each verified (an upload signed with the file's hash, then checked for its
size; a copy into the data root rehashed while written and synced), without
holding the graph lock, so writes go on. Then it takes the lock, copies what
arrived meanwhile and switches the graph: one
`maint.graph_configure` event pair whose intent names the location before
and after, the object store's id and the number and bytes of the files
([Blob location](audit.md#blob-location)). A file added while a graph moves
is committed where the graph keeps its files once the add takes the lock.

- **Nothing is deleted.** The old copies are `blob_misplaced` (info) in
  `files verify` until `files gc` removes them, each only after the new
  location's copy, read in full, showed the same hash. A move back to the
  data root records the object store it leaves, which is what lets `files
  gc` clean the bucket; the bucket's marker stays.
- **Refused before any copy:** an object store that is not configured or
  has no credentials (`config.invalid`, `backend.unavailable`), or, for a
  graph in an object store, whose marker is not that graph's store
  (`backend.unavailable`); a graph in the middle of a data migration
  (`admin.migration_incomplete`: finish the release first). `--to s3`
  creates the bucket's marker when there is none. A store that refuses an
  upload (quota, permissions) stops with `files.store_refused`; the graph
  stays where it was, and the copies made so far are reused by the next run.
  A file a transaction names that is in neither location stops the move with
  `backend.corrupt` (`files verify` names it).
- **Interrupted moves** leave the graph where it was with every file: a run
  stopped while copying only leaves extra copies, and one stopped right
  before the switch leaves an intent without effect, which `audit verify`
  reports as info. Running the move again finishes it.
- **`--dry-run`** copies and writes nothing and answers per graph how many
  files and bytes the move covers (`stored`) and would copy (`remaining`).
  A graph already at the target writes no audit event.
- **`--step pass|switch`** runs one copy pass, or only the switch, with 30
  seconds of copying each (a switch whose copies under the lock take longer
  switches nothing and answers `copying`). In the remote mode `graph-admin`
  sends a dry run, then these steps for each graph, so every request fits
  the client's timeout; without `--step` a local run copies and switches in
  one go. Each step is safe to repeat.
- Exit codes follow the other commands; with several graphs the first
  failure ends the command, its answer lists the graphs handled before.

## Auditing and releases

Every change is audited: an intent in the graph's stream, the effect, then
an installation event anchoring the intent. A release must bump the version
at least as much as the changes require; a new major version on a graph
with data is released only if the data has no error findings under the new
version (`check_graph`, the check `graph check --ontology` runs without
releasing), otherwise the refusal lists them. On every release, also a minor
or a patch, the published functions are checked against the new model
(`graph_cli.kinetic` rules): one that no longer resolves refuses the release
with `admin.functions_broken` (the first 20 problems with the function
names in `details`); `ontology diff --graph` warns about them beforehand
(`admin.functions_affected`). The published actions get the same check
(`admin.actions_broken`, `admin.actions_affected`). Releases of graphs that use [markings](access_control.md#markings) are also checked for them: a release that would reuse the name of a once-marked attribute without its markings is refused with `ontology.marking_name_reused` (`ontology release`, `ontology diff --graph`, `graph push`, `graph check --ontology`; history and the name map decide, see the `ontology-best-practices` skill), and `ontology release`, `ontology diff --graph` and `graph push` warn about a marking that no installed grant names (`admin.marking_ungranted`), a grant that names an undeclared one (`admin.policy_unknown_marking`), a marking taken off an attribute (`admin.marking_removed`), a marked name that returns unmarked (`ontology.marking_name_returned`), a required marked attribute without default (`admin.marking_blocks_create`) and a marked attribute in a rule, guard or action precondition (`admin.marking_in_expression`). Fix or delete the
functions and actions in the same `graph push` as the ontology; a release with renames
compiles those whose sources still compile and publishes them again by itself
([Releasing with a data migration](#releasing-with-a-data-migration)). Verification reports an
interrupted operation as info and anchors only graphs without errors.

Related guides: [Ontology authoring](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/ontology-best-practices/SKILL.md)
(the `graph_cli.ir/1` format, modeling, versioning),
[Admin skills](skills.md#admin-skills) (the skills for maintainers),
[Access control](access_control.md) (policies),
[Skills](skills.md#graph-skills) (publishing graph skills),
[Files](files.md#backups) (verifying stored files after a restore).
