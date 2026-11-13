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
edited by hand: `graph_cli.skills.app_skill.render_app_skill` turns the app
inventory (`graph describe commands --json`, `graph_cli.inventory/1`) into
`SKILL.md` (workflow, habits, envelope, command overview, exit codes),
`references/commands.md` (arguments, options with their choices and
defaults, limits and examples per command), `references/errors.md` (every
error code and what to do per category), `references/query.md` (when and
how to use `graph query`: names, parameters, the supported subset,
examples; fixed text of the renderer), `references/changesets.md` (the
change set documents of `graph apply`: keys, operations, values, limits,
an example; key tables and limits from `kernel.changeset`, prose fixed in
the renderer), `references/output.md` (the fields of pages, hints,
warnings, entities, `lookup`, `search` and `history` items and write results; fixed
text, checked against real answers), `references/files.md` (storing,
linking and reading files; fixed text) and `references/history.md`
(earlier revisions and states; fixed text). The renderer reads nothing but
that document and imports only the kernel (import contract). Its write step sends an agent
to a named action first (`graph describe search <value> --kind action` or the graph skill's
list, then `graph do <action> --dry-run`, then without) and to the primitives only when none
fits. The frontmatter `metadata` carries the
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
  template at every `skills emit|install|check` with the model of the
  graph's current pin, so after an ontology release an installed copy is
  `stale` and `graph skills install` refreshes it; no republish is needed.
- `static`: no marker. The files are the skill, installed as they are; a
  skill published against another ontology release than the pin gives the
  warning `skills.source_behind_ontology`.

### Blocks

| Block | Renders |
| --- | --- |
| `start` | load the `graph-cli` skill before the first command; how to tell the graph is available (`graph graphs --json`, its name and title, `--graph <graph>`); the graph language (and the `<text>@<tag>` form when the model has language-tagged fields); then the rules of working with `graph` that apply from the first call, so that the skill also works when an agent loads it without `graph-cli`: plain `graph` calls with `--json`, retried plainly when refused; `ok`, `data` and `error.hint`; never cut an answer, ask for less (`--fields`, `graph query` with a narrow `RETURN`; on a modular graph the patterns come from the module views, not the whole schema); never guess an option or a term (`--help`, `graph describe type`); a named function before a query (an empty value in its answer means nothing is recorded) and a named action before the write commands, when the graph has them; dry runs, `--reason`, `--expect`, change sets in a file; report `data.findings` |
| `modules` | the subject areas (ontology modules) of the graph: one row per module that is not built in (module, prefix, summary, number of types, links to other modules with the number of relations per direction), the built-in modules in one line, then the commands to read one module (`graph describe module <module>`), to find a term (`graph describe search <text>`) and to read a type (`graph describe type <type>`). For a graph with several modules it takes the place of `types` and `relations`, which would list every type |
| `types` | one row per entity type (types with an alias prefix; without any, every concrete type): CURIE and alias example, summary and supertypes, fields (enum values inline, `required`, `many`, `unique`, write-once, defaults, the label role as main name, other names or hidden names), states and transitions |
| `relations` | the relations from or to those types: the `graph query` pattern (`(:A)-[:rel]->(:B)`, from `describe schema`), cardinality, characteristics, qualifiers (inverse labels, which no command takes, only in `references/model.md`) |
| `constraints` | transition guards, rules with severity, and write-once fields unless the template also has `types`, which marks them |
| `names` | the alias format (`ITEM-0001`; one example when the template also has `types`, whose rows name every alias) and the id forms that work; which fields `graph lookup` matches (label roles: when the template also has `types` and it lists every type with names, a pointer to the roles marked there, else per type); use a candidate only on exactly one exact match, else ask. On a modular graph: one alias example, and the module pages and `graph describe type` for the prefix and name fields of each type |
| `model` | a pointer to `references/model.md` and `graph describe type` |
| `functions` | the graph's named functions ([Functions](cli_graph.md#functions)): the number of active functions, then at most 12 of them, those marked `featured: true` in their file (by module, then name; further ones are not listed and nothing warns about them), one line each (`name(signature)`: summary), and the commands to run one (`graph run`), list a module's functions, search them (`graph describe search <text> --kind function`) and read one. Without published functions: one line saying so |
| `actions` | the graph's named actions ([Actions](cli_graph.md#actions)): the number of actions the installing reader may run, the dry-run rule (`graph do <action> --param <name>=<value> --dry-run`, read, then run without), at most 8 of them, those marked `featured: true` in their file (by module, then name; the others are not listed), one line each (`name(signature)`: summary; `?` marks an optional parameter, "Needs `--reason`" an action with `reason: required`), and the commands to list a module's actions, search them (`graph describe search <text> --kind action`) and read one (`graph describe action`). Unlike the functions, the listing follows the reader's write rights: an action is left out when a write it needs is not allowed to the actor that installs or renders the skill, and with none left the block is one line saying there are no published actions, as for a graph without any |

Each block may be used once; an unknown block name is refused. Every
generated skill also gets `references/model.md` (every type of the model,
imported ones included, with all fields, lifecycles, relations and rules),
so a template may not contain a file of that name. A stamp after the
frontmatter names the graph, the ontology release and the template hash;
the program version and the time are not in it, so a new program with the
same render stays `fresh`. The summary of a deprecated term starts with
"Deprecated." in every block. Blocks use only the model and the manifest,
never graph data (no counts, no records). Curated examples may name real
aliases.

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

A graph with published functions ([Functions](cli_graph.md#functions)) gets
more, generated from its compiled catalog (deprecated functions are left
out everywhere); a graph without any renders exactly as without this
feature:

- the `modules` block gets a `Functions` column with the number of active
  functions of each module;
- every module page gets a `Functions` section: a table of name, parameters
  (the signature of `graph describe functions`) and summary, and how to run
  one;
- a single-module graph, which has no module pages, gets
  `references/functions.md` with the same table. A template may not contain
  that file either.

A function costs about 180 bytes on its page (name, signature and a summary
of the longest length), so a thousand functions take about 70 percent of the
256 KB of the tree; the model pages share the rest.

A graph with published actions ([Actions](cli_graph.md#actions)) gets the
same, next to the functions (deprecated actions are left out as well):

- the `modules` block gets an `Actions` column after `Functions`;
- every module page gets an `Actions` section: a table of name, parameters
  and summary, and the dry-run rule;
- a single-module graph gets `references/actions.md`. The name is reserved
  in every template, so a template may not contain that file either.

Only the actions the installing reader may run are listed (the `describe`
commands apply the same rule, [Access control](access_control.md#actions)),
so a reader who may run none gets the skill of a graph without actions.
`graph-admin` renders and publishes with the whole catalog, so the preview
and the name check know every action.

A single-module graph renders as before; the `modules` block then lists the
one module and does not mention pages. Use `modules` instead of `types` and
`relations` for a modular graph, so `SKILL.md` stays short however many
types the graph has: its generated blocks then grow with the number of
modules only, and with the template of the test fixtures `SKILL.md` of a
graph of 50 modules stays under 20,000 bytes (about 5,000 tokens), whatever
the size of each module. A graph with published functions may use 2,500
bytes more for the `functions` block and the `Functions` column, and 2,000
more for the `actions` block and the `Actions` column (the budget
`L0_BUDGET` is the base 20,000 plus 2,500 plus 2,000, and a graph without
functions or actions keeps the base for that part): `SKILL.md` stays under
24,500 bytes for 50 modules of 20 functions and 10 actions each, whatever
the number of either, because the blocks list at most 12 functions and 8
actions. Curated text adds to that.

### Per reader

A generated graph skill is rendered for the reader who installs or checks it.
For a reader with [read limits](access_control.md#read-limits) the model blocks
and pages (`modules`, `types`, `relations`, the references), the functions and
the actions are rendered from what that reader sees: no hidden type, relation,
field, function or action is named, and a module with nothing left is gone. A
reader without limits gets the skill as before. The claim of an installed
copy records which set of types it was rendered for when the reader is
limited, so a change of the policy that changes the set makes the copy `stale`
(see [Stale skills](#stale-skills)) and `graph skills install` renders it
again.

Curated text is not filtered. The prose of the template and its curated
`references/*.md` are shown to every reader as written, so they must not name a
type, relation or function that some reader of the graph may not see. The name
check at publish and at install runs against the full model, so a hidden name
in curated text raises no `skills.source_unknown_names` warning that would
itself name it. Write curated text in terms that hold for every reader, and let
the generated blocks carry the names.

### Budgets

Every generated graph skill has a level 0 budget, single-module graphs
included: `SKILL.md` after rendering at most 20,000 bytes, plus 2,500 for a
graph with published functions and 2,000 for one with actions, and its
curated text (the template's `SKILL.md` outside the frontmatter and the
block markers) at most 6,000 bytes. A single-module graph lists its whole
model in `SKILL.md`, so it reaches the budget as its model grows; past it,
split the model into modules. Curated text past its budget belongs in
curated `references/*.md` that `SKILL.md` links with the situation in
which to read them (for instance the rules for entering one kind of
document). `graph-admin graph skill render` reports the sizes (`data.size`:
bytes of `SKILL.md` and of the curated text, both budgets and the bytes of
each block, largest first); render, `graph skill publish` and `graph push`
warn `admin.skill_over_budget` when either budget is exceeded. Nothing is
refused for its size below the hard limits of the next section.

### Publishing

Publishing checks the directory (`graph_cli.skills.tree`): the name equals
the directory and the frontmatter `name`, regular UTF-8 files only, no
dotfiles, `SKILL.md` at most 500 lines and the tree at most 256 KB, and no
maintenance vocabulary (`graph-admin`, `maint.*`, `admin.*`, the data root
option and variable) or local paths. A template is also rendered against
the current pin exactly as `graph` will render it, and its curated text is
checked against the model and the app inventory (`graph_cli.skills.names`):
every CURIE with a model prefix names a type, relation, enum, lifecycle or
datatype; every `graph <command>` in code names a command and only its
flags; aliases with a graph prefix have the live format (`ITEM-1` is
refused), and in code an alias with a prefix the graph does not use is
reported; every `graph run <name>` in code names a published function (a
placeholder such as `<function>` does not count; in a workspace push the
functions of the same push count), and every `graph do <name>` names a
published action (the same rules; a deprecated action counts, it still runs). Any problem refuses (`admin.skill_invalid`,
`details.problems` with `rule` `template`, `term`, `command`, `alias`,
`function`, `action` next to the tree rules).
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
reports what publish would refuse (exit 1, the preview is written anyway);
it changes nothing in the data root. A non-empty preview directory is
replaced only with `--force`. Without the authored copy at hand (another
machine, the remote mode), `graph-admin graph pull` writes the published
template into a workspace and `graph-admin graph push` publishes it again
([Workspaces](cli_graph_admin.md#workspaces-pull-and-push)).

## Admin skills

Five hand-written skills for maintainers ship inside `graph-admin`
(`packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/`), so every
install of it, the wheel a server hands out included, carries the version
that fits it:

| Skill | For |
| --- | --- |
| [`ontology-best-practices`](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/ontology-best-practices/SKILL.md) | authoring an ontology: when a graph pays off, competency questions to release, modeling choices, names and summaries, rules and lifecycles, change levels, a review checklist; `references/ir_format.md` is the reference of the `graph_cli.ir/1` format |
| [`maintaining-graphs`](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/maintaining-graphs/SKILL.md) | the maintainer workflow with `graph-admin`: the workspace, new ontology versions, new graphs, publishing graph skills, giving apps roles, checks and the audit |
| [`authoring-graph-skills`](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/authoring-graph-skills/SKILL.md) | the curated text of a graph skill template: blocks, description, rules and recipes, what publishing checks, the preview loop |
| [`authoring-graph-functions`](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/authoring-graph-functions/SKILL.md) | the named read functions of a graph: when to write one, names, `use_when`, typed `ref` parameters, `returns`, `featured`, the publish loop and release refusals; `references/format.md` is the reference of the `graph_cli.function/1` format |
| [`authoring-graph-actions`](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/authoring-graph-actions/SKILL.md) | the named write actions of a graph: when a recurring write is worth an action, names, `use_when`, typed `ref` parameters, preconditions against ontology rules, `unlink ... all: true`, `reason`, `featured`, the publish loop and release refusals; `references/format.md` is the reference of the `graph_cli.action/1` format |

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

`tests/skills/test_ontology_skill.py` compiles the YAML examples of
`ontology-best-practices` and checks that its format reference names every
key the parser accepts, and `tests/skills/test_functions_skill.py` does the
same for the function files of `authoring-graph-functions` and
`tests/skills/test_actions_skill.py` for the action files of
`authoring-graph-actions`; `tests/skills/test_admin_skills.py` checks that
every `graph-admin` and `graph` command line in the admin skills names a
real command and only its options. In the checkout, link them for an agent
working there (`.claude/` is not tracked):

```sh
mkdir -p .claude/skills
for s in packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/*/; do
  ln -s "../../$s" ".claude/skills/$(basename "$s")"
done
```

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
read limits, when the set of types the reader sees changed. A new function
catalog changes the render of a generated skill, so `graph skills check`
reports the installed copy as `stale` and `graph skills install` refreshes
it; the commands above do not notice a catalog-only change.

- A write (`--dry-run` included) is refused with `skills.outdated` (exit 1);
  the hint is `graph self update` (with `--dir` for a project's copy),
  which refreshes the copies.
- A read runs and warns `skills.stale`, naming the same command.
- Meta commands (`whoami`, `describe commands`, `skills ...`) are never
  checked: they are how a stale copy is found and refreshed.
- Without an installed skill nothing is checked (a person at a shell).

The details list each stale copy (`skill`, `kind`, `scope`, what it was
rendered from and what is current). In the
[remote mode](server.md#skill-freshness) the server judges the graph
skills; skill 1 is judged by the client, because `graph skills install`
renders it from the client's own inventory.
