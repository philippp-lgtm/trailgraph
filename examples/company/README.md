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
- **Synthetic seed data** (117 entities): `seeds/1.0.0.jsonl` for the first version,
  `seeds/1.1.0.jsonl` for what only exists from the second. Stable idempotency keys
  (`company-<version>-<nn>`), so a second apply writes nothing twice.

## Build it

Needs `graph` and `graph-admin` on `PATH` (from a checkout: `uv sync`, then
`PATH="$PWD/.venv/bin:$PATH"`). `BASE` is a new directory outside the checkout; the script
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
`seeds/1.1.0.jsonl`, publishes the skill and ends with `graph-admin graph check company` and
`graph-admin audit verify`, both clean. `setup.sh 1.0.0` stops after the first version, so
the minor release can be shown by hand:

```sh
graph-admin ontology diff examples/company/ontology/1.1.0 --graph company --module company
graph-admin ontology release company examples/company/ontology/1.1.0 --module company
graph apply examples/company/seeds/1.1.0.jsonl
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

  Without a script, the same by hand: `graph-admin graph create company --ontology
  examples/company/ontology/1.0.0 --module company`, `graph apply
  examples/company/seeds/1.0.0.jsonl --graph company`, `graph-admin ontology release company
  examples/company/ontology/1.1.0 --module company`, `graph apply
  examples/company/seeds/1.1.0.jsonl --graph company`, `graph-admin graph skill publish
  company examples/company/skill/graph-company`, then `graph-admin graph check company` and
  `graph-admin audit verify`. If `graph apply` answers `access.denied`, the policy has no
  scope for the project ([Access control](../../docs/access_control.md)); if it answers
  `skills.outdated`, run `graph self update`. The live view of a server runs where the data
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

Questions the graph answers (each is a `graph query`; the generated skill lists more):

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
graph transition EMP-0001 left --dry-run          # rule: a leaver still holds a position
```
