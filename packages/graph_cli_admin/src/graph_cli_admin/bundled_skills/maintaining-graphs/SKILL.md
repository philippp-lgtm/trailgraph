---
name: maintaining-graphs
description: Maintain the graphs of a graph_cli installation with graph-admin, locally or against a server in remote mode - set up and sync a maintainer workspace (one folder per graph), create a graph, release a new ontology version, publish a graph skill or the named functions, check a graph's data and verify the audit trail. Use when the user wants a new graph, an ontology or graph skill change rolled out, a graph checked, or the workspace refreshed. Not for reading or writing graph data (the graph-cli skill) and not for how to model (ontology-best-practices).
---

# Maintaining graphs

This skill is for whoever maintains graphs with `graph-admin`: it creates
graphs, releases their ontologies and publishes their skills and named
functions. What a graph holds is designed with the `ontology-best-practices`
skill; the curated text of a graph skill is written with the
`authoring-graph-skills` skill; the function files with the
`authoring-graph-functions` skill.
Reading and writing the data of a graph is the `graph-cli` skill.

## Before the first command

1. `graph-admin graph list` shows the graphs you can maintain. It also
   tells where you are: in the remote mode every command goes to the
   server, and the commands that need the data root here (`init`, `view`,
   `--data-root`) are refused with `admin.remote_mode`.
2. Who you are decides what works. A maintainer may do everything. A
   project admin (an admin token bound to a project) sees and changes only
   the graphs its project maintains: release, publish, push, check. It
   cannot create graphs, apply policies or create tokens; on
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
   graph show <graph>`), it may have happened.
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
  .graph_cli_source.json            the base: what the folder was pulled from
```

The answer lists every graph with an `action`: `pulled`, `present`,
`behind` (the graph has another ontology release pinned than the folder
was pulled from), `conflict` (the folder name is taken by something else)
or `failed`. Folders are never replaced. A `behind` folder without
unpushed edits is refreshed with `graph-admin graph pull <graph> <folder>
--force`; with edits, pull into another folder, carry them over and push
from there.

## Change the ontology of a graph

1. Copy the highest version directory to the new version
   (`ontology/1.2.0/` to `ontology/1.3.0/`) and set `module.version` of
   the main module and of every module you change there. Never change a released version directory.
2. Edit it with the `ontology-best-practices` skill;
   `graph-admin ontology validate <folder>/ontology/<new>` until it
   reports no findings.
3. `graph-admin ontology diff <folder>/ontology/<new> --graph <graph>`
   classifies the change and says whether the version bump is enough; the
   warning `admin.module_bump_insufficient` names a changed module whose
   own version did not follow.
4. For a major change: `graph-admin graph check <graph> --ontology
   <folder>/ontology/<new>` shows which entities the new version would
   refuse. Fix the data (with the `graph-cli` skill) or the model first.
5. `graph-admin graph push <graph> <folder> --dry-run`, then the same with
   `--reason "<why>"`. Push releases the highest version directory, pins
   the graph to it and publishes a changed skill and changed functions, in
   one step; it writes the new base into the folder.
6. `graph-admin audit verify`.
7. Tell the user that the agents using the graph need fresh skills: a
   generated graph skill renders the new model on its own; they run
   `graph self update` (or `graph skills install`) where they work.

A graph with several ontology modules is released the same way. Give
`--module <main>` to `graph-admin ontology validate`,
`graph-admin ontology diff`, `graph-admin ontology release`,
`graph-admin graph create` and `graph-admin graph check` (with its
`--ontology` option) whenever the directory holds more than one module;
`graph-admin graph push` needs none. The rules (every module file in each
version directory, the bump of the main module, moving terms) are in
`ontology-best-practices`, `references/versioning_and_release.md`
(section "Releasing a modular graph").

Published functions must resolve under the new version. `ontology diff
--graph` warns `admin.functions_affected` with the functions the new
version would break, and a release (also a minor or patch) that would
break one is refused with `admin.functions_broken`. Do not release around
it: change or delete the affected files in the folder's `functions/` (see
the `authoring-graph-functions` skill) and push them together with the new
ontology version, in the same `graph-admin graph push`; both are checked against the
model the graph will have.

`admin.source_conflict` means the graph changed since the folder was
pulled: pull it into another folder, carry the edits over, push from
there. `admin.version_bump_insufficient` means the bump is too small for
the change `ontology diff` found.

## Create a graph

Only a maintainer can create a graph.

1. Draft it outside the graph folders, for example in
   `drafts/<graph>/`: a `README.md` with the purpose and 5 to 10
   competency questions, and `ontology/1.0.0/<module>.yaml` (the
   `ontology-best-practices` skill).
2. `graph-admin ontology validate drafts/<graph>/ontology/1.0.0` until it
   reports no findings. Where a local data root is at hand, try the model
   on a throwaway graph first (`ontology-best-practices`, versioning and
   release).
3. Create it:

   ```sh
   graph-admin graph create <graph> --ontology drafts/<graph>/ontology/1.0.0 --title "<what it is for>"
   ```

   `--language <tag>` sets the default language of its values; it cannot
   change later. Graph names are lowercase letters, digits and `_`, unique
   per installation (`admin.graph_name_taken`).
4. `graph-admin workspace sync .` pulls it into its folder. Move the
   draft's `README.md` there and delete the draft.
5. Write and publish its graph skill (below), then `graph-admin audit
   verify`.
6. Agents reach the graph only when the policy lets their project use it
   and their project's `.graph_cli.toml` lists it. Tell the user what is
   missing; change the policy only when asked (`graph-admin policy diff
   <file>`, then `graph-admin policy apply <file> --reason "<why>"`).

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
and lists what publishing would refuse (exit 1). Read the preview before
pushing. A graph without a skill gets one the same way: create the
directory in its folder and push.

## Publish or change functions

The named functions of a graph (`functions/<name>.yaml` in its folder) are
published with the ontology and the skill by `graph-admin graph push`.
Write them with the `authoring-graph-functions` skill, then:

```sh
graph-admin graph functions publish <graph> <folder>/functions --dry-run
graph-admin graph push <graph> <folder> --dry-run
graph-admin graph push <graph> <folder> --reason "<why>"
graph-admin graph functions show <graph>
```

The dry run reports every problem of every file together
(`admin.functions_invalid`). A missing or empty `functions/` folder
unpublishes all functions. Agents may run the functions only when their
policy grants `read.function`; a policy installed before that action
existed needs `graph-admin policy apply` with the same source once. On
`access.denied` for that, tell the user.

## Check a graph

- `graph-admin graph show <graph>`: manifest, head, installed releases,
  the published skill, the number of published functions.
- `graph-admin graph check <graph>`: every entity against the pinned
  ontology, including what no write checks, and the published functions
  against the same model; exit 1 on error findings or when a function no
  longer resolves (`admin.functions_broken`).
- `graph-admin files verify --graph <graph>`: the stored files of a graph
  that imports the `files` module.
- `graph-admin audit verify`: every chain, log and pin of the
  installation.

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
| `admin.functions_broken` | a published function would not resolve; fix or delete its file and push it together with the ontology in one `graph-admin graph push` |
| `admin.data_errors` | the data breaks the new version; fix the entities in `details.findings` |
| `access.denied` | not allowed for this token; stop and tell the user |
| `remote.unauthorized` | no valid admin token; tell the user, never search for one |
| `remote.client_outdated` | this graph-admin is not the build the server runs; stop and tell the user to run `graph-admin self update` |

The warning `remote.version_differs` means the server runs another build.
With `details.compatible: true` the commands and these skills are the
same: finish the task, then tell the user once that
`graph-admin self update` installs the server's build.
