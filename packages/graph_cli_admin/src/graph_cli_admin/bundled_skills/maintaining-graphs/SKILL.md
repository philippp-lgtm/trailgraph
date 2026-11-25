---
name: maintaining-graphs
description: Maintain the graphs of a graph_cli installation with graph-admin, locally or against a server in remote mode - set up and sync a maintainer workspace (one folder per graph), create a graph, release a new ontology version, publish a graph skill, the named functions, the named actions or the views, give an app a role, feed a graph from an external pipeline, move a graph's stored files between the data root and an object store, check a graph's data and verify the audit trail. Use when the user wants a new graph, an ontology or graph skill change rolled out, an app's role set up, a graph checked, or the workspace refreshed. Not for reading or writing graph data (the graph-cli skill) and not for how to model (ontology-best-practices).
---

# Maintaining graphs

This skill is for whoever maintains graphs with `graph-admin`: creating
graphs, releasing their ontologies, publishing their skills, named functions,
named actions and views. Design a graph with `ontology-best-practices`, write
its graph skill with `authoring-graph-skills`, its function, action and view
files with `authoring-graph-functions`, `authoring-graph-actions` and
`authoring-graph-views`. Graph data is read and written with `graph-cli`.

## Before the first command

1. `graph-admin graph list` shows the graphs you can maintain. It also
   tells where you are: in the remote mode every command goes to the
   server, and the commands that need the data root here (`init`, `view`,
   `--data-root`) are refused with `admin.remote_mode`.
2. Who you are decides what works. A maintainer may do everything. A
   project admin (an admin token bound to a project) releases, publishes,
   pushes and checks only the graphs its project maintains; it cannot create
   graphs, release a migration, apply policies or create tokens. On
   `access.denied` stop and tell the user.
3. `graph-admin describe commands --json` lists every command with its
   options; `graph-admin <command> --help` shows one.

## Ground rules

1. Every write runs with `--dry-run` first where the command has it; read
   the answer, then run it again with `--reason "<why>"`.
2. One change at a time, then `graph-admin audit verify`. Stop when it
   reports errors.
3. Never edit `.graph_cli_source.json`, the data root or a graph's store
   files. Changes go through `graph-admin` only.
4. On a refusal, read `error.code`, `error.message` and `error.hint`; fix
   the cause instead of trying variants. Never retry a write that ended
   with `remote.outcome_unknown`: check the state first (`graph-admin
   graph show <graph>`), it may have happened; only a release (`graph-admin
   ontology release`, `graph-admin graph push`) is safe to run again.
5. Policies, tokens and enrollment codes only when the user asks for
   them. Never print a token or a code into a file or a commit.
6. Ask before what cannot be undone: a major release on a graph with
   data, removing files with `files gc`.

## The workspace

A workspace is a folder with one subfolder per graph, set up and refreshed
by one command:

```sh
graph-admin workspace sync <dir>
```

It installs the admin skills into `<dir>/.claude/skills/`, writes a short
`CLAUDE.md` when there is none, and pulls every graph that has no folder
yet into `<dir>/<graph>_<id suffix>/` (the suffix is the end of the graph
id, so two graphs of one name on two installations never share a folder).
Each graph folder holds:

```
<graph>_<id suffix>/
  README.md                         purpose, competency questions, design choices (yours)
  ontology/<version>/<module>.yaml  the released ontology, one directory per version
  skill/graph-<graph>/              the published graph skill, if the graph has one
  functions/<name>.yaml             the published named functions, if the graph has any
  actions/<name>.yaml               the published named actions, if the graph has any
  views/<name>.yaml                 the published views, if the graph has any
  .graph_cli_source.json            the base: what the folder was pulled from
```

The answer lists every graph with an `action`: `pulled` (`pull` in a dry run),
`present`, `behind` (the graph has another ontology release pinned than the folder was
pulled from), `conflict` (the folder name is taken by something else) or `failed`.
Folders are never replaced. A `behind` folder without unpushed edits is refreshed with
`graph-admin graph pull <graph> <folder> --force`; with edits, pull into another folder,
carry them over and push from there.

## Change the ontology of a graph

1. Copy the highest version directory to the new version
   (`ontology/1.2.0/` to `ontology/1.3.0/`) and set `module.version` of
   the main module and of every module you change there. Never change a released version directory.
2. Edit it with the `ontology-best-practices` skill; `graph-admin ontology
   validate <folder>/ontology/<new>` until it reports no findings.
3. `graph-admin ontology diff <folder>/ontology/<new> --graph <graph>`
   classifies the change and says whether the version bump is enough; the
   warning `admin.module_bump_insufficient` names a changed module whose
   own version did not follow.
4. For a major change: `graph-admin graph check <graph> --ontology <folder>/ontology/<new>`
   shows which entities the new version would refuse. Fix the data or the model, or add a
   `migration.yaml` that rewrites it and `check` previews (`ontology-best-practices`,
   `references/migrations.md`; [release_with_a_migration.md](references/release_with_a_migration.md)).
5. `graph-admin graph push <graph> <folder> --dry-run`, then the same with
   `--reason "<why>"`. Push releases the highest version directory, pins
   the graph to it and publishes a changed skill, functions, actions and views
   in one step; it writes the new base into the folder.
6. `graph-admin audit verify`.
7. Agents get the new model with their next `graph start`. A release that imports a
   file kind (`files_pdf`, ...) is followed by `graph-admin files extract --graph <graph>
   --dry-run` and the run ([extract_file_metadata.md](references/extract_file_metadata.md)).

A graph with several ontology modules is released the same way. Give `--module <main>`
to `graph-admin ontology validate`, `ontology diff`, `ontology release`, `graph-admin
graph create` and `graph-admin graph check` (with its `--ontology` option) when the
directory holds more than one module; `graph-admin graph push` needs none. Rules:
`ontology-best-practices`, `references/versioning_and_release.md` ("Releasing a modular
graph"); splitting a module: its `references/migrations.md`.

Published functions, actions and views must resolve under the new version. `ontology
diff --graph` warns `admin.functions_affected`, `admin.actions_affected` and
`admin.views_affected` for the ones it would break and lists where renamed terms are
named; a release that would break one is refused (`admin.functions_broken`,
`admin.actions_broken`, `admin.views_broken`), unless it has renames and the sources
still compile (published again with it). Fix the rest in `functions/`, `actions/` or
`views/` and push them with the ontology. A deny rule naming only a renamed term is
refused (`admin.policy_renamed_term`): apply a policy naming both first.

`admin.source_conflict` means the graph changed since the folder was pulled:
pull it into another folder, carry the edits over, push from there.
`admin.version_bump_insufficient`: the bump is too small for the change.

## Create a graph

Only a maintainer can create a graph.

1. Draft it outside the graph folders, for example in `drafts/<graph>/`: a
   `README.md` with the purpose and 5 to 10 competency questions, and
   `ontology/1.0.0/<module>.yaml` (the `ontology-best-practices` skill).
2. `graph-admin ontology validate drafts/<graph>/ontology/1.0.0` until it
   reports no findings. Where a local data root is at hand, try the model on a
   throwaway graph first (`ontology-best-practices`, versioning and release).
3. Create it:

   ```sh
   graph-admin graph create <graph> --ontology drafts/<graph>/ontology/1.0.0 --title "<what it is for>"
   ```

   `--language <tag>` sets the default language of its values; it cannot
   change later. Graph names are lowercase letters, digits and `_`, starting
   with a letter, unique per installation (`admin.graph_name_taken`).
4. `graph-admin workspace sync .` pulls it into its folder. Move the
   draft's `README.md` there and delete the draft.
5. Write and publish its graph skill (below), then `graph-admin audit verify`.
6. Agents reach the graph only when the policy lets their project use it
   and their project's `.graph_cli.toml` lists it. Tell the user what is
   missing; change the policy only when asked (`graph-admin policy diff
   <file>` first, then apply it as in
   [give_an_app_a_role.md](references/give_an_app_a_role.md)).

## Publish or change a graph skill

The graph skill (`skill/graph-<graph>/` in the graph's folder) tells
agents what the graph holds and how to use it. Write it with the
`authoring-graph-skills` skill, then:

```sh
graph-admin graph skill render <graph> <folder>/skill/graph-<graph> --out <scratch dir> --force
graph-admin graph push <graph> <folder> --dry-run
graph-admin graph push <graph> <folder> --reason "<why>"
```

`render` writes what agents would get into `<scratch dir>/graph-<graph>/`
and lists what publishing would refuse (exit 1); read it before pushing. A
graph without a skill gets one the same way: create the directory, push.

## Publish or change functions

The named functions of a graph (`functions/<name>.yaml` in its folder) are
published with the ontology and the skill by `graph-admin graph push`.
Read `graph-admin usage report <graph>` before writing or changing functions:
its candidates and batch gaps are the input. Write them with the
`authoring-graph-functions` skill, then:

```sh
graph-admin graph functions publish <graph> <folder>/functions --dry-run
graph-admin graph push <graph> <folder> --dry-run
graph-admin graph push <graph> <folder> --reason "<why>"
graph-admin graph functions show <graph>
```

The dry run reports every problem of every file together (`admin.functions_invalid`); a missing
or empty `functions/` folder unpublishes all functions. A result column that no type describes
only warns (`admin.function_untyped_columns`): add `type:`. Agents run functions only with
`read.function` (without it they are marked `allowed: false`; an older policy needs `policy apply`
again, [give_an_app_a_role.md](references/give_an_app_a_role.md)). A publish of a function, action
or view needs nothing from agents; only `featured` changes make installed graph skills stale
(`graph start` refreshes them).

## Publish or change actions and views

The named actions (`actions/<name>.yaml`) and the views (`views/<name>.yaml`) are
published by `graph-admin graph push` after the release, the skill and the functions,
views last (they run functions); write them with `authoring-graph-actions` and
`authoring-graph-views`. `graph-admin graph actions publish <graph> <folder>/actions
--dry-run` and `graph-admin graph views publish <graph> <folder>/views --dry-run` report
every problem together (`admin.actions_invalid`, `admin.views_invalid`); the push as
above publishes, an empty folder unpublishes all of its kind, `graph-admin graph actions
show <graph>` and `graph-admin graph views show <graph>` list them. Functions and actions share one namespace per graph; views have
their own. An action needs no policy: a caller lacking a write right of its ops, or
`read.get`, `read.find` or `read.function` for a view, finds it marked `allowed: false`
(`access.denied` when it calls it); what names a hidden term does not exist for it. A
function a view runs cannot be removed or changed incompatibly alone
(`admin.views_broken`): push both.

## Give an app a role

Roles let apps of one project differ: declared in the installation policy,
given to app tokens or to enrollment codes. Only when the user asks; the steps
and the policy grammar are in
[give_an_app_a_role.md](references/give_an_app_a_role.md). A pipeline that keeps part
of a graph equal to an external source needs a role, a first run and a pause across
releases that touch its keys: [feed_a_graph_from_a_pipeline.md](references/feed_a_graph_from_a_pipeline.md).

## Check a graph

- `graph-admin graph show <graph>`: manifest, head, releases, skill, functions, actions, views.
- `graph-admin graph check <graph>`: every entity against the pinned ontology
  (also what no write checks) and the published functions, actions and views
  against the same model; exit 1 on error findings or one that no longer resolves.
- `graph-admin files verify --graph <graph>`: the stored files of a graph
  that imports the `files` module, and the metadata records behind their extractor.
  Moving them to an object store and back:
  [move_stored_files.md](references/move_stored_files.md).
- `graph-admin audit verify`: every chain, log and pin of the installation.

## Common refusals

| Code | What to do |
| --- | --- |
| `admin.remote_mode` | the command needs the data root; run it where the data lives, or use the remote alternative |
| `admin.model_invalid` | fix the findings `ontology validate` lists |
| `admin.release_exists` | a released version directory was changed; put the change into a new version |
| `admin.source_exists` | the folder holds a pull already; `graph-admin workspace sync`, or `graph-admin graph pull --force` |
| `admin.workspace_conflict` | a folder `workspace sync` needs is taken; move it away |
| `admin.skill_invalid` | fix what `details.problems` lists, check with `graph-admin graph skill render` |
| `admin.functions_invalid` | fix what `details.problems` lists, check with `graph-admin graph functions publish <graph> <folder>/functions --dry-run` |
| `admin.functions_broken`, `admin.actions_broken`, `admin.views_broken` | a published function, action or view would not resolve; fix or delete its file and push it with the ontology (or the function change) in one `graph-admin graph push` |
| `admin.actions_invalid`, `admin.views_invalid` | fix what `details.problems` lists, check with the `--dry-run` of `graph-admin graph actions publish` or `graph-admin graph views publish` |
| `admin.data_errors`, `admin.migration_required` | the data breaks the new version (after its migration, if there is one); fix the entities in `details.findings` or the steps of `migration.yaml` |
| `admin.migration_incomplete` | an interrupted release; run the same release again (`references/release_with_a_migration.md`) |
| `access.denied` | not allowed for this token; stop and tell the user |
| `remote.unauthorized` | no valid admin token; tell the user, never search for one |
| `remote.client_outdated` | this graph-admin build is not compatible with the server's; stop and tell the user to run `graph-admin self update` |

The warning `remote.version_differs` means the server runs another build;
with `details.compatible: true` the commands and these skills are the same:
finish the task, then tell the user once that `graph-admin self update`
installs the server's build.
