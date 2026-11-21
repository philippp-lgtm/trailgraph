# Example graph: company

A small, invented company (Marrowdale: nothing here is real) held in a graph whose ontology is
cut into **modules**, one per subject area. It shows how a modular graph is authored, grows
by a minor release and is explored by agents top down.

| Module | Prefix | Holds |
| --- | --- | --- |
| `people` | `ppl` | employees, externals, their identities in systems |
| `organization` | `org` | departments, teams, positions, roles, cost centers; who holds a position |
| `operations` | `ops` | processes, instructions, policies, trainings, tasks |
| `legal` (from 1.1.0) | `leg` | legal entities, shareholdings, which entity employs people or runs units |
| `company` | `co` | the main module: imports the others, holds no terms, carries the release |

## What it demonstrates

- **Two versions of one graph.** `ontology/1.0.0` is the internal organization;
  `ontology/1.1.0` adds the `legal` module. `graph-admin ontology diff` classifies the step as
  minor: a new module file plus one import in the main module; the other module files are
  unchanged. Rules and versioning:
  [Splitting into modules](../../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/ontology-best-practices/references/modeling.md#splitting-into-modules)
  and [Releasing a modular graph](../../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/ontology-best-practices/references/versioning_and_release.md#releasing-a-modular-graph).
- **Imports point from the dependent area to the one it builds on**
  (`people` <- `organization` <- `operations`, `legal` imports `people` and `organization`),
  without cycles. A relation between two areas lives in the importing module: `org:holds`
  (person to position) in `organization`, `ops:completed` (person to training) in
  `operations`, `leg:employed_by` and `leg:operated_by` in `legal`.
- **Lifecycles and rules where they pay off:** positions, employees, policies, tasks and legal
  entities have states with guards (a position that is held cannot be abolished; a dissolved
  legal entity employs nobody); a team sits in exactly one department; an employee who has
  left holds no position; the stakes in a legal entity add up to at most 100 percent (a
  warning, because the data may be right and the source wrong).
- **A reified relation where the link has facts of its own:** `leg:Shareholding` is a class so
  that the stakes of one entity can be summed and checked; a parent company is read from it,
  not stored.
- **A curated graph skill** (`skill/graph-company/SKILL.md`) with the `modules` block instead
  of one table per type. See [Skills](../../docs/skills.md) for how it is published and
  installed.
- **Named read functions** (`functions/`, one `graph_cli.function/1` file each) for the
  recurring questions: members of a unit, missing trainings, policies, employer, reporting chain,
  vacant positions. Each takes typed `ref` parameters and agents run it with `graph run`; the
  curated skill lists the featured ones through its `functions` block. See
  [Functions](../../docs/cli_graph.md#functions) and the `authoring-graph-functions` admin skill.
- **Named write actions** (`actions/`, one `graph_cli.action/1` file each) for the recurring
  changes: staff a vacant position, hand a held position over, offboard an employee. Each
  checks its preconditions on the current state and writes through the normal write path;
  agents run it with `graph do`, and the curated skill lists the featured ones through its
  `actions` block instead of spelling out the steps. See [Actions](../../docs/cli_graph.md#actions) and the
  `authoring-graph-actions` admin skill.
- **Views** (`views/`, one `graph_cli.view/1` file each): `position_overview` is an entity view
  and the default of `org:Position` (fields, the unit, the holder, and the reporting chain from
  the `reporting_chain` function in one answer under 6 KiB); `org_catalog` is a set view that lists
  every unit, position and person with their keys and relations, paged by bytes, for scripts and
  renderers that would otherwise make one `find` call per type. Agents call them with
  `graph view`. See [Authored views and set views](../../docs/cli_graph.md#authored-views-and-set-views)
  and the `authoring-graph-views` admin skill.
- **Synthetic seed data**: `seeds/1.0.0.jsonl` for the first version,
  `seeds/1.1.0.jsonl` for what only exists from the second. Stable idempotency keys
  (`company-<version>-<nn>`), so a second apply writes nothing twice.

## Build it

Needs `graph` and `graph-admin` on `PATH` (from a checkout: `uv sync`, then
`PATH="$PWD/.venv/bin:$PATH"`). `BASE` is a new or empty directory outside the checkout; the script
puts the data root, configuration and a home directory below it, a short state directory
in `/tmp` (the live view's socket path is limited in length), and leaves your own
installation alone:

```sh
BASE=/tmp/company_example examples/company/setup.sh
source /tmp/company_example/env.sh        # selects the throwaway installation in this shell
graph-admin view                          # the live view, see docs/live_view.md
```

It runs `graph-admin init`, creates the graph from `ontology/1.0.0` (`--module company`
names the main module), applies `seeds/1.0.0.jsonl`, releases `ontology/1.1.0`, applies
`seeds/1.1.0.jsonl`, publishes the functions, the actions, the views and then the skill (the
skill is checked against them) and ends with `graph-admin
graph check company` and `graph-admin audit verify`, both clean. `setup.sh 1.0.0` stops after the first version, so
the minor release can be shown by hand:

```sh
graph-admin ontology diff examples/company/ontology/1.1.0 --graph company --module company
graph-admin ontology release company examples/company/ontology/1.1.0 --module company
graph apply examples/company/seeds/1.1.0.jsonl
graph-admin graph functions publish company examples/company/functions
graph-admin graph actions publish company examples/company/actions
graph-admin graph views publish company examples/company/views
graph-admin graph skill publish company examples/company/skill/graph-company
```

Remove the installation with `rm -rf` of `$BASE` and the state directory that `setup.sh`
prints.

## Load it into an existing installation

`load.sh` does the steps of `setup.sh` in whatever installation `graph` and `graph-admin`
point at, so a maintainer can create the graph as a demo graph next to the real ones. The
graph is named `company`.

- **Local mode** (the data root is on this machine): run `examples/company/load.sh` from a
  directory outside any project, or from one whose `.graph_cli.toml` lists `company`.
- **Remote mode** (a server owns the data, see
  [Remote mode](../../docs/server.md#remote-mode-of-graph-admin)): you need an admin token
  that may create graphs, and a project token that may write the graph. Run it from a
  project directory whose `.graph_cli.toml` lists `company`:

  ```sh
  mkdir company_demo && cd company_demo
  printf 'format_version = 1\ngraphs = ["company"]\ndefault = "company"\n' > .graph_cli.toml
  /path/to/examples/company/load.sh
  ```

  `load.sh` is the single list of steps; read it for the exact commands. It can be run again
  after a failure: it creates the graph only when it is missing and releases 1.1.0 only while
  the graph is still at 1.0.0. If `graph apply` answers `access.denied`, the policy has no
  scope for the project ([Access control](../../docs/access_control.md)); if it answers
  `skills.outdated`, run `graph start`. The live view of a server runs where the data
  is ([Container](../../docs/server.md#container)).

## Explore it

An agent reads a modular graph top down. Run these in the throwaway installation (add
`--graph company` elsewhere):

```sh
graph describe modules                   # one line per area, and how the areas link
graph describe module legal              # its types, relations, links to and from other modules, rules
graph describe search shareholder        # find a term by a word; narrow with --module and --kind
graph describe types --module ops        # --module also limits types, relations and schema
graph describe schema --module legal --compact
```

Read one entity at a glance (the default view of its class, else the generated card with key
fields, relations with counts and the first neighbours, what applies next):

```sh
graph view POS-0013
graph view POS-0013 --name card           # the generated card, whatever views the graph has
```

Find and read the published views; a set view takes no entity and answers in pages of whole rows:

```sh
graph describe views
graph describe view org_catalog
graph view --name org_catalog --page-bytes 4096          # the first page, with next and more
graph view --name org_catalog --page-bytes 4096 --cursor <value>   # the cursor of that page
graph view --name org_catalog --page-bytes 262144        # the whole catalog in one call
```

A script that reads the JSON form of a large page (`--json`) gets it in a file once it is bigger than
the spill limit (`output.spilled`); `GRAPH_CLI_SPILL_BYTES=0` keeps it on stdout
([configuration](../../docs/configuration.md)).

Run a named function (parameters are ids or aliases, found with `graph lookup`):

```sh
graph describe functions                                   # what is published
graph run members_of_unit --param unit=DEPT-0003           # people of a unit and its sub-units
graph run reporting_chain --param position=POS-0013
```

Run a named action: a dry run first names the entities it will change and the operations on
each (preconditions are checked on the current state; a refusal names the one that failed):

```sh
graph describe actions
graph do hand_over_position --param position=POS-0013 --param person=EMP-0003 --dry-run
graph do offboard_employee --param employee=EMP-0007 --reason "HR confirmed" --dry-run
```

Questions the graph answers (some also exist as functions, see `graph describe functions`; the others are `graph query`; `graph start` counts the functions and actions you may use):

- Who holds a position, and whom does it report to, up to the top? `(:Employee)-[:holds]->(:Position)`
  and `(:Position)-[:reports_to*1..4]->(:Position)`.
- Which trainings must a person complete? Person, position, role, training:
  `(:Person)-[:holds]->(:Position)-[:carries]->(:Role)<-[:required_for]-(:Training)`, three modules in one path.
- Who owns a legal entity and how much? `(:Shareholding)-[:stake_in]->(:LegalEntity)` with
  `(:Shareholding)-[:shareholder]->(h)`.
- Which legal entity employs a person, and which runs a department? `leg:employed_by`,
  `leg:operated_by`.
- Which processes have no instruction? `graph-admin graph check company` reports
  `ops.process_documented` when one has none.

Try a write the model refuses. The answer names the relations that connect the two types
(the `connecting` hint):

```sh
graph link EMP-0003 org:in_unit DEPT-0006
# connect with: ppl:Employee -org:holds-> org:Position -org:in_unit-> org:Department
graph transition LE-0002 dissolved --dry-run      # guards: the entity still employs people
graph transition EMP-0001 left --dry-run          # rules: a leaver holds a position and has an employer
```
