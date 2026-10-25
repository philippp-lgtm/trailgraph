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
that document and imports only the kernel (import contract). The frontmatter `metadata` carries the
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
| `start` | how to tell the graph is available (`graph graphs --json`, its name and title, `--graph <graph>`); load the `graph-cli` skill before the first command, and two of its ground rules repeated (plain `graph` calls, retried plainly when refused; `graph query` for a few fields, a count or a join) |
| `types` | one row per entity type (types with an alias prefix; without any, every concrete type): CURIE and alias example, summary and supertypes, fields (enum values inline, `required`, `many`, `unique`, write-once, defaults), states and transitions |
| `relations` | the relations from or to those types: from -> to, cardinality, characteristics, qualifiers, inverse label; then the same relations as `graph query` patterns (`(:A)-[:rel]->(:B)`, from `describe schema`) |
| `constraints` | transition guards, write-once fields, rules with severity |
| `names` | the alias format (`ITEM-0001`) and the id forms that work; which fields `graph lookup` matches per type (label roles); use a candidate only on exactly one exact match, else ask |
| `model` | a pointer to `references/model.md` and `graph describe type` |

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
reported. Any problem refuses (`admin.skill_invalid`, `details.problems`
with `rule` `template`, `term`, `command`, `alias` next to the tree rules).
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

Three hand-written skills for maintainers ship inside `graph-admin`
(`packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/`), so every
install of it, the wheel a server hands out included, carries the version
that fits it:

| Skill | For |
| --- | --- |
| [`ontology-best-practices`](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/ontology-best-practices/SKILL.md) | authoring an ontology: when a graph pays off, competency questions to release, modeling choices, names and summaries, rules and lifecycles, change levels, a review checklist; `references/ir_format.md` is the reference of the `graph_cli.ir/1` format |
| [`maintaining-graphs`](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/maintaining-graphs/SKILL.md) | the maintainer workflow with `graph-admin`: the workspace, new ontology versions, new graphs, publishing graph skills, checks and the audit |
| [`authoring-graph-skills`](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/authoring-graph-skills/SKILL.md) | the curated text of a graph skill template: blocks, description, rules and recipes, what publishing checks, the preview loop |

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
key the parser accepts; `tests/skills/test_admin_skills.py` checks that
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
for a generated one, when the graph's ontology pin moved.

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
