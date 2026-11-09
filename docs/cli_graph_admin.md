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
| `init` | create the data root and, if missing, `config.toml` with a device id and the key-encryption key file ([Keys of sealed graphs](configuration.md#keys-of-sealed-graphs)) |
| `graph create NAME --ontology DIR` | create a sealed graph (its values stored encrypted, with a key store of its own) pinned to the first release of an authored ontology; `--language TAG` sets the BCP 47 default language of its values (canonical case, fixed after create) |
| `graph list`, `graph show REF` | list graphs with title and language (a project admin: the graphs it maintains); show manifest, head, installed releases, the published skill and the count and hash of the published functions and actions |
| `graph check REF` | check every entity against the pinned ontology, including what no write checks (rules with `when: batch`, `in` cardinality) and names shared by two active entities of one type family (warning `data.semantic.possible_duplicate`, once per name; only `same` names, a near spelling is reported on the write that adds it); exit 1 (`admin.data_errors`) on error findings, warnings are listed; also checks the published functions against the model (`admin.functions_broken`, for example `function.stale` after an interrupted push) and the published actions (`admin.actions_broken`, for example `action.stale`; the functions are reported first); `--ontology DIR` checks against an authored version before it is released |
| `graph reindex REF` | rebuild the revision files, the checkpoint and the index of a graph from its transaction log (`--dry-run`, `--reason`), audited as `maint.reindex`; for derived files that `audit verify` reports as different from the log, or reads that warn `storage.index_stale` |
| `graph pull REF DIR` | write the authored sources of a graph into a workspace directory: its pinned release (`--version V`: another installed one), its published skill, functions and actions, byte for byte, with the base `push` checks (`--force` replaces an earlier pull); changes nothing in the data root ([Workspaces](#workspaces-pull-and-push)) |
| `graph push REF DIR` | release and publish what changed in a pulled directory, under one lock; refused when the graph changed since the pull (`--dry-run`, `--reason`) |
| `workspace sync DIR` | set up or refresh a [maintainer workspace](#maintainer-workspaces): the admin skills in `DIR/.claude/skills/`, `DIR/CLAUDE.md` when there is none, and a pull of every graph that has no folder yet (`--dry-run`, `--force` for the skills) |
| `graph skill publish GRAPH DIR` | publish a skill directory or a template as the graph's skill (`--dry-run`, `--reason`) |
| `graph functions publish GRAPH DIR` | publish a folder of function files (`<name>.yaml`, [`graph_cli.function/1`](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/authoring-graph-functions/references/format.md), the format reference of the `authoring-graph-functions` admin skill) as the graph's function catalog, checked against the graph's ontology pin; every problem is reported together (`admin.functions_invalid`); an empty folder unpublishes all (`--dry-run`, `--reason`) |
| `graph functions show GRAPH` | the published function catalog: hash, ontology pin it was published against, and one line per function |
| `graph actions publish GRAPH DIR` | publish a folder of action files (`<name>.yaml`, [`graph_cli.action/1`](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/authoring-graph-actions/references/format.md), the format reference of the `authoring-graph-actions` admin skill) as the graph's action catalog, checked against the graph's ontology pin; every problem is reported together (`admin.actions_invalid`); an action may not be named like a published function, nor a function like a published action (one namespace per graph); an empty folder unpublishes all (`--dry-run`, `--reason`) |
| `graph actions show GRAPH` | the published action catalog: hash, ontology pin it was published against, and one line per action |
| `graph skill render GRAPH DIR --out OUT` | preview: write what `graph skills install` would install into `OUT/graph-<graph>/` and report what publish would refuse (`--force`); changes nothing in the data root |
| `ontology validate DIR` | compile and check an authored ontology |
| `ontology diff DIR --against DIR` / `--graph REF` | classify the changes and check the version bump; with `--graph`, warns `admin.functions_affected` and `admin.actions_affected` for the published functions and actions the release would break |
| `ontology release GRAPH DIR` | publish a release into the graph and pin the graph to it; classes the release adds get their `display_prefix` as alias prefix (existing prefixes never change: a class renamed with `renamed_from` keeps the prefix of its old name, and a prefix another class already holds is not given out, with the warning `admin.alias_prefix_taken`); every published function and action must still resolve under the new release, on every bump, else `admin.functions_broken` or `admin.actions_broken` ([Auditing and releases](#auditing-and-releases)) |
| `audit verify` | check every chain, tx log, revision file, index, manifest pin and installed policy; exit 1 on error findings |
| `audit anchor` | record every graph stream's head in the installation stream |
| `usage report [GRAPH] [--since DATE]` | read the access log (needs `[audit] access_log = true`; else `access_log: false`) per graph, or for `GRAPH` only: function runs (calls, distinct sessions, errors by code), `never_run` (published functions without a run), `function_share` (whole percent of runs among runs and queries), query `shapes` ranked by distinct sessions, and three lists derived from them: `discovery_misses` (a covered shape still queried), `batch_gaps` (the covering function takes one value where the query took a list) and `candidates` (uncovered shapes in at least two sessions, the input for new functions). Each list shows 20 entries, `<list>_more` counts the rest. `--since` takes a date (midnight UTC) or an RFC 3339 timestamp, inclusive. Shapes only, no values; sessions are counted, never listed. In remote mode the server's log is read through the admin API (`maint.audit_read`) |
| `files verify --graph REF` | check the stored files of a graph: missing (and, with `--rehash`, damaged) files are errors, orphans and staging leftovers info |
| `files gc --graph REF` | remove stored files no transaction names and staging files older than one hour (`--dry-run`, `--reason`), audited as `maint.repair` |
| `policy check FILE [--graph G]` | compile an authored policy and report every problem; installs nothing |
| `policy show [--graph G]` | the installed compiled policy and its hash (the built-in one when none is installed) |
| `policy diff FILE [--graph G]` | rules and keys added, removed or changed against the installed policy |
| `policy apply FILE [--graph G] --reason R` | install a policy, audited as `maint.policy_change` (the installation policy also mirrored into every graph stream); refuses one that would lock you out; the installed policy again records missing mirrors |
| `policy explain --graph G [--project P] [--token NAME] [--action A] [--type T] [--file F]` | what an app caller from a project (or from none), or a request with a token, would get, and by which rule; `maintains` says whether the caller may maintain the graph; records nothing |
| `token create NAME [--project P] [--expires DATE] [--scope S] [--reason R]` | create a bearer token for the server API, audited as `maint.token_create`; the token is shown only once and stored as a hash; `--scope admin` makes it a token of the [admin API](server.md#admin-api), with `--project` a [project admin](access_control.md#project-admins); warns when the policy grants an app token nothing or does not let an admin token maintain (a project admin: no graph) |
| `token list` | the tokens: name, scope, project, expiry, creation time, hash prefix; never the token |
| `token revoke NAME [--reason R]` | revoke a token, audited as `maint.token_revoke`; the server refuses it from the next request on |
| `enroll create --project P [--scope app\|admin] [--expires-in D] [--reason R]` | a one-time code a client exchanges (`graph connect`) for a token bound to `P`; shown once, valid 15 minutes or `D` (`<n>m`, `<n>h`, at most 24h), audited as `maint.token_create`; see [Enrollment](server.md#enrollment) |
| `enroll list` | the codes not used yet: id, project, scope, expiry, creation time, hash prefix; never the code |
| `enroll revoke ID [--reason R]` | revoke an unused code, audited as `maint.token_revoke` |
| `self update` | in the remote mode install the server's build of `graph-admin` when it differs (`--check` only reports); see [Self update](server.md#self-update) |
| `skills install --scope user\|project` | install or refresh the [admin skills](skills.md#admin-skills) for an agent (`--dir`, `--force`, `--dry-run`); runs here, also in the remote mode |
| `skills check --scope user\|project` | report each admin skill as `fresh`, `stale`, `modified`, `unexpected`, `foreign`, `missing` or `unavailable` (`--dir`); exit 1 unless all are fresh |
| `view [--graph REF]` | serve a live view of the graphs in the browser until Ctrl+C, opened on `REF` or on none (`--port`, `--host`, `--no-open`, `--token-file`, `--project`); no JSON mode (see [Live view](live_view.md)) |

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

An authored version directory holds one `<module>.yaml` per module, and a
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
(`admin.actions_broken`, `admin.actions_affected`). Fix or delete the
functions and actions in the same `graph push` as the ontology. Verification reports an
interrupted operation as info and anchors only graphs without errors.

Related guides: [Ontology authoring](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/ontology-best-practices/SKILL.md)
(the `graph_cli.ir/1` format, modeling, versioning),
[Admin skills](skills.md#admin-skills) (the skills for maintainers),
[Access control](access_control.md) (policies),
[Skills](skills.md#graph-skills) (publishing graph skills),
[Files](files.md#backups) (verifying stored files after a restore).
