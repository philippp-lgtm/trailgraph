# Skills

graph_cli gives coding agents two kinds of skills (Agent Skills format:
`SKILL.md` with `name` and `description` frontmatter, details under
`references/`): the `graph-cli` skill for the CLI itself, and one graph
skill per graph that says what the graph holds. `graph skills install`
puts both into a project or the user's skill directory. Maintainers have
the [admin skills](#admin-skills) that ship with `graph-admin`, for
authoring ontologies and graph skills and for rolling them out.

## The `graph-cli` skill

`skills/graph-cli/` is the agent skill for `graph`. It is generated, never
edited by hand: it is rendered from the app inventory (`graph describe
commands --json`, `graph_cli.inventory/1`) into `SKILL.md` (workflow, habits,
envelope, command overview, exit codes; the overview leaves out a command declared for programs
only, such as `graph describe tools`, which the reference lists),
`references/commands.md` (arguments, options with their choices and
defaults, limits and examples per command), `references/errors.md` (every
error code and what to do per category), `references/query.md` (when and
how to use `graph query`: names, parameters, the supported subset,
examples; fixed text of the renderer), `references/changesets.md` (the
change set documents of `graph apply`: keys, operations, values, limits,
an example; key tables and limits from the change set code, prose fixed in
the renderer), `references/output.md` (the fields of pages, hints,
warnings, entities, `lookup`, `search` and `history` items and write results; fixed
text, checked against real answers), `references/files.md` (storing,
linking and reading files; fixed text) and `references/history.md`
(earlier revisions and states; fixed text). The renderer reads nothing but
that document. Its write step sends an agent
to a named action first (`graph describe search <value> --kind action` or the graph skill's
featured ones, then `graph do <action> --dry-run`, then without) and to the primitives only when none
fits. Its description ends with the sentence "Run `graph start` once per session before
loading this or any graph skill", the first step of its workflow is `graph start`
([Starting a session](cli_graph.md#starting-a-session)), and its error guidance names
`graph start` for `skills.outdated` and `skills.stale`. The frontmatter `metadata` carries the
inventory hash and program version. How to regenerate it and what the
tests check: [`CONTRIBUTING.md`](../CONTRIBUTING.md#generated-files).

## Graph skills

A graph skill (`graph-<graph>`, `_` becomes `-`) says what one graph holds
and how to use it. The maintainer writes a skill directory and publishes it
into the graph's runtime area. Its kind follows the content:

- `generated`: `SKILL.md` is a template. A line consisting only of
  `<!-- graph_cli:block <name> -->` is replaced by a block generated from
  the graph's live model and manifest; the frontmatter, all other prose and
  extra files (`references/*.md`) are curated and kept. `graph` renders the
  template at every `graph skills emit`, `graph skills install` and
  `graph skills check` with the model of the
  graph's current pin, so after an ontology release an installed copy is
  `stale` and `graph skills install` refreshes it; no republish is needed.
- `static`: no marker. The files are the skill, installed as they are; a
  skill published against another ontology release than the pin gives the
  warning `skills.source_behind_ontology`.

### Blocks

The blocks are generated from the graph's model, its manifest and its
function and action catalogs. Which blocks there are, what each renders and
how to use them in a template is the catalog of the [`authoring-graph-skills`](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/authoring-graph-skills/SKILL.md#the-template)
admin skill, the one home of the authoring rules. Here is what holds for
all of them. Each block may be used once; an unknown block name is refused.
The renderer appends the sentence "Run `graph start` first and load graph-cli with it." to the
description of a generated skill that does not mention `graph start` yet, so an agent that
chooses the skill by its description is sent to the first call of a session. The sentence
lives in the renderer only; a template does not write it. `graph-admin` refuses to publish or
push a template whose description cannot take it (too long for the 1,024 characters of a
description, or wrapped over lines as a plain or quoted scalar), so every published template
gets it; the rule is in [What publishing checks](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/authoring-graph-skills/SKILL.md#what-publishing-checks).
Installing is never refused for it: a skill published before the rule keeps its description as
written. Folded and literal block scalars take the sentence as one more line. A `static` skill
is installed as it is, so it gets nothing.
The `start` block opens with "If you have not yet in this session: `graph start --json`".

Every generated skill also gets `references/model.md` (every type of the
model, imported ones included, with all fields, lifecycles, relations and
rules), so a template may not contain a file of that name. A stamp after
the frontmatter names the graph, the ontology release and the template
hash; the program version and the time are not in it, so a new program
with the same render stays `fresh`. The summary of a deprecated term starts
with "Deprecated." in every block. Blocks use only the model and the
manifest, never graph data (no counts, no records). Curated examples may
name real aliases.

A graph with two or more modules besides the built-in ones is modular. Its
generated skill reads the model module by module instead of in one piece:

- `references/modules/<module>.md`, one page per module that is not built in:
  the module's summary, its types (alias example, abstract, supertype,
  name fields, summary), the relations inside it, the relations to and from other
  modules with the other module named, and its rules. A template may not
  contain a file below `references/modules/`.
- `references/model.md` becomes an index: every module with its page and
  every type with its module and page. Fields, lifecycles and rules stay
  with `graph describe type`.

A graph with published functions ([Functions](cli_graph.md#functions)) or actions
([Actions](cli_graph.md#actions)) gets a little generated text from its compiled
catalogs, and no list or count of them (deprecated entries are left out):

- the `functions` and `actions` blocks list the entries the maintainer marked
  `featured` (at most 12 functions and 8 actions, by module then name, a signature
  and a summary each) and the commands to run, list, search and describe the rest;
- the `modules` block and every module page name `graph describe module <module>`
  for the functions and actions of a module, in one line each, when the graph has
  any;
- nothing else: no count, no per-module column or section, no
  `references/functions.md` or `references/actions.md` (both names stay reserved:
  a template may not contain them). Lists and counts come from the tool at the
  point of use ([`graph start`](cli_graph.md#starting-a-session) and the
  `describe` commands).

So a function, action or view that is published, changed, deprecated or removed
without being featured leaves every rendered file as it was, and installed copies
stay `fresh`. Featuring or unfeaturing one, or the first or last entry of a kind,
changes the render and makes them `stale` (see [Stale skills](#stale-skills)).

Only the featured actions the installing reader may run are listed (an action it may
not run is counted by `graph start` as not allowed, [Access control](access_control.md#actions)),
so a reader who may run none gets the skill of a graph without actions.
`graph-admin` renders and publishes with the whole catalog, so the preview
and the name check know every action.

A single-module graph has no module pages; the `modules` block then lists the
one module and does not mention pages. Use `modules` instead of `types` and
`relations` for a modular graph, so `SKILL.md` stays short however many
types the graph has: its generated blocks then grow with the number of
modules only, and the blocks list a fixed maximum of functions and actions,
whatever the number of either. Curated text adds to that. The size budgets
and what exceeding them does are in the [skill](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/authoring-graph-skills/SKILL.md#what-the-curated-text-is-for).

### Per reader

A generated graph skill is rendered for the reader who installs or checks it.
For a reader with [read limits](access_control.md#read-limits) the model blocks
and pages (`modules`, `types`, `relations`, the references), the featured functions and
the featured actions are rendered from what that reader sees: no hidden type, relation,
field, function or action is named, and a module with nothing left is gone. A
reader without limits gets the whole skill. The claim of an installed
copy records which set of types and which fields it was rendered for when the
reader is limited, so a change of the policy that changes either (a type limit,
a [marking](access_control.md#markings) grant) or a release that marks another
field makes the copy `stale` (see [Stale skills](#stale-skills)) and `graph
skills install` renders it again. A marking change on a type the reader does
not see leaves the claim alone.

The featured functions are listed only to a reader who may `read.function`: a reader
without it gets one line saying that functions are not for this token, and no name
(when it sees at least one function: a reader whose read limits hide them all gets the
skill of a graph without functions, with or without the right). The
digest of what the render shows of the catalogs (see [Stale skills](#stale-skills)) is the
stored one of the whole catalog for a reader without read limits, with every marking and with
`read.function`. For any other reader it is computed from exactly the documents its render
uses, so a featured entry it cannot see changes nothing for its copy (it stays `fresh`, and
no digest of hidden content appears in a warning), a featured entry it sees makes it stale,
and a change of its rights that changes the render does too.

A field that carries [markings](access_control.md#markings) is listed only for
a reader who holds all of them, and then with a short mark naming them (`marked
ex:personal`, several joined by `+`; they are conjunctive). The mark costs a few
bytes per marked field and appears only in graphs that use markings, so a
limited reader's skill is smaller, not larger.

Curated text is not filtered. The prose of the template and its curated
`references/*.md` are shown to every reader as written, so they must not name a
type, relation, function or marked field that some reader of the graph may not
see, nor an example value of one. The name
check at publish and at install runs against the full model, so a hidden name
in curated text raises no `skills.source_unknown_names` warning that would
itself name it. Write curated text in terms that hold for every reader, and let
the generated blocks carry the names.

### Publishing

Publishing checks the directory, renders a template against the current pin
exactly as `graph` will render it and checks its curated text against the
model, the app inventory and the published functions and actions. Any
problem refuses (`admin.skill_invalid`); a skill over its size budget is
published with a warning (`admin.skill_over_budget`), and so is a template
that teaches what the tool already carries (the recipe lint, `admin.skill_recipe_covered`,
`admin.skill_generic_example` and `admin.skill_recipe_broken`). The checks, the
lint and the budgets are listed in the [`authoring-graph-skills`](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/authoring-graph-skills/SKILL.md#what-publishing-checks)
admin skill.
Publishing is audited as `maint.graph_configure` and records the ontology
version it was checked against; the same files against the same version
publish nothing. `graph` reads published skills with `read.describe`, so a
project only gets the skills of the graphs its pointer allows. When the
model or the program changes later, `graph` checks the curated text again
at every render and warns with `skills.source_unknown_names` (the skill is
still installed, its model part is right); a template it cannot render (a
block from a newer publisher) is `skills.source_invalid`.

### Authoring loop

Edit the template, preview, read, publish, install.

```sh
uv run graph-admin graph skill render <graph> <dir> --out <preview>
uv run graph-admin graph skill publish <graph> <dir> --dry-run
uv run graph-admin graph skill publish <graph> <dir> --reason "<why>"
graph skills install --scope project --dir <project>
```

`render` writes `<preview>/graph-<graph>/` exactly as `graph skills
install` would (without the marker), with this checkout's program, and
reports what publish would refuse as an error (the preview is written anyway);
it changes nothing in the data root. `<preview>` must exist; an existing
`<preview>/graph-<graph>/` is replaced only with `--force`. Without the
authored copy at hand (another
machine, the remote mode), `graph-admin graph pull` writes the published
template into a workspace and `graph-admin graph push` publishes it again
([Workspaces](cli_graph_admin.md#workspaces-pull-and-push)).

## Admin skills

Hand-written skills for maintainers ship inside `graph-admin`
(`packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/`), so every
install of it, the wheel a server hands out included, carries the version
that fits it:

| Skill | For |
| --- | --- |
| [`ontology-best-practices`](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/ontology-best-practices/SKILL.md) | authoring an ontology: when a graph pays off, competency questions to release, modeling choices, names and summaries, rules and lifecycles, change levels, a review checklist; `references/ir_format.md` is the reference of the `graph_cli.ir/1` format |
| [`maintaining-graphs`](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/maintaining-graphs/SKILL.md) | the maintainer workflow with `graph-admin`: the workspace, new ontology versions, new graphs, publishing graph skills, named functions, named actions and views, giving apps roles, moving stored files to or from an object store, checks and the audit |
| [`authoring-graph-skills`](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/authoring-graph-skills/SKILL.md) | the curated text of a graph skill template: blocks, description, rules and recipes, what publishing checks, the preview loop |
| [`authoring-graph-functions`](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/authoring-graph-functions/SKILL.md) | the named read functions of a graph: when to write one, names, `use_when`, typed `ref` parameters, `returns`, `featured`, the publish loop and release refusals; `references/format.md` is the reference of the `graph_cli.function/1` format |
| [`authoring-graph-actions`](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/authoring-graph-actions/SKILL.md) | the named write actions of a graph: when a recurring write is worth an action, names, `use_when`, typed `ref` parameters, preconditions against ontology rules, `unlink ... all: true`, `reason`, `featured`, the publish loop and release refusals; `references/format.md` is the reference of the `graph_cli.action/1` format |
| [`authoring-graph-views`](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/authoring-graph-views/SKILL.md) | the views of a graph: entity views and set views, when a view is worth writing over the generated card, a function or a query, names, `use_when`, parameters, sections, the publish loop and release refusals; `references/format.md` is the reference of the `graph_cli.view/1` format, of what a call answers and of its bounds |

`graph-admin skills install --scope project|user` copies them into an
agent skill directory with the installer of `graph skills install` (the
same targets, marker, staged swap and statuses; marker kind `admin`), and
`graph-admin skills check` reports them; [`graph-admin workspace
sync`](cli_graph_admin.md#maintainer-workspaces) installs them into a
maintainer workspace. Their names never start with `graph-`: `graph`
judges, refreshes and reports only `graph-cli` and `graph-*`, and
`graph-admin` only its own, so both kinds share one skill directory.
Nothing refuses a command because an admin skill is stale; `skills check`
says so, and `skills install` or `workspace sync` refreshes them after
`graph-admin self update`.

How the tests hold the admin skills to the code, and how to link them for an
agent working in the checkout: [CONTRIBUTING.md](../CONTRIBUTING.md#tests).

## Installing skills

`graph skills install` copies skill 1 and graph skills into an agent skill
directory. There is no default target:

- `--scope project [--dir DIR]`: `DIR/.claude/skills/` (`DIR` defaults to
  the working directory; `DIR/.claude/` must exist).
- `--scope user`: the user skill directory, `$CLAUDE_CONFIG_DIR/skills/`
  when that variable is set, else `~/.claude/skills/` (the configuration
  directory must exist).

In the [remote mode](server.md#remote-mode-of-graph) the skills are
written on this machine as well; a graph skill comes from the server,
rendered there for the graph's current ontology.

Skill 1 is always installed, graph skills named with `--graph`, and every
graph skill installed there before is refreshed; one whose graph is gone,
no longer visible or unpublished is reported as `unavailable` and left
alone. `--project-graphs` adds the skills of every graph the pointer file
names (seen from `DIR` for `--scope project`); a graph without a published
skill or out of reach is listed in `skipped_graphs` instead of failing. Each installed directory carries the marker `.graph_cli_skill.json`
(program, source, file hashes); a directory without it, or with files that
differ from it, is replaced only with `--force`. Without `--force` such a
directory is left as it is and the other skills are still installed; the
answer lists every skill (`action: refused` for the held-back ones) and
fails with `skills.modified` or `skills.not_owned`. Unrelated skill
directories are never touched. Each skill is swapped in with renames from a
staging directory next to `skills/`, so an agent never sees a half copy.
`--dry-run` shows the plan. `graph skills check` reports each skill as
`fresh`, `stale`, `modified`, `unexpected`, `foreign`, `missing` or
`unavailable` and names the program that checked (`data.program.executable`).
A generated graph skill is `stale` after a new program, a republish or an
ontology release changed its render; the marker's `source.model` names the
ontology release it was rendered with.

```sh
graph skills install --scope project --dir <dir> --graph <graph>
graph skills check --scope project --dir <dir>
```

Run `install` and `check` with the installed `graph` (see
[Install](../README.md#install)), not with `uv run graph` from the
checkout: `check` compares against the program that runs it.
`graph self update` refreshes every scope that holds a skill of this
program in one call, after installing the server's build in the remote
mode ([Self update](server.md#self-update)).

## Stale skills

An agent follows the skills installed for it, so a copy older than what it
describes misleads it. Every `graph` command that reads or writes a graph
first checks the installed copies an agent may load: those in
`<dir>/.claude/skills/`, where `<dir>` holds the project's pointer
`.graph_cli.toml`, and those in the user skill directory; both count. Skill
1 is stale when it was rendered from another inventory than the running
program's, a graph skill when the graph published another skill since or,
for a generated one, when the graph's ontology pin moved or, for a reader with
read limits or without some marking, when the set of types or fields the reader
sees changed. A copy of a generated skill also records what its render showed of the catalogs: a
digest of the featured function and action lines and of whether the graph has functions,
actions and views (`source.featured` of the marker, `featured=` in the claims header).
The digest of the published catalogs is derived when a catalog is published and stored
in its `slot.json`; so a catalog change that is not about a featured entry (and not the
first or last entry of a kind) leaves every copy `fresh`, and one that is makes it
`stale` on the next command, `skills.stale` for a read and `skills.outdated` for a write.
A copy installed before the digest was recorded has none and is not judged on it by
the commands; `graph skills check` reports it `stale` (it also compares what the marker
says the copy was rendered from, not the program version) and `graph start` refreshes it.
For a reader without read limits (and with `read.function`) the digest covers the whole
published catalog, so a featured action that reader may not run still moves it (its copy is then
refreshed to the same files). It judges freshness only and is not access
control: nothing checks the stored value against the catalog it came from, and a claim
may carry any digest or none.

- A write (`--dry-run` included) is refused with `skills.outdated` (exit 1);
  the hint is `graph start` (with `--dir` for a project's copy), which refreshes
  the copies; `graph self update` is for a program that is not the server's build. In the
  remote mode against a server of another build the first `graph start` only reports the
  copies and names `graph self update` (two steps: the update installs the build and renders
  the copies again).
- A read runs and warns `skills.stale`, naming the same command.
- Meta commands (`graph whoami`, `graph describe commands`, `graph start`,
  `graph connect`, `graph self update`, `graph skills check`, `graph skills emit`,
  `graph skills install`) are never checked: they are how a stale copy is found and refreshed. `graph start` refreshes the
  stale copies of the scopes that have a skill of this program (as `graph self update`,
  without replacing the program) and reports each; `graph start --check` only reports.
- Without an installed skill nothing is checked (a person at a shell).

The details list each stale copy (`skill`, `kind`, `scope`, what it was
rendered from and what is current). In the
[remote mode](server.md#skill-freshness) the server judges the graph
skills; skill 1 is judged by the client, because `graph skills install`
renders it from the client's own inventory.
