---
name: authoring-graph-skills
description: Write or improve the graph skill of a graph_cli graph - the template (skill/graph-<graph>/SKILL.md with graph_cli:block markers) that tells agents what the graph holds, when to use it and which rules to follow - and preview and publish it with graph-admin. Use when a graph needs its first skill, when agents misuse a graph or miss it, or after an ontology change that the curated text must follow.
---

# Authoring graph skills

A graph skill (`graph-<graph>`, `_` becomes `-`) is what an agent reads
before it uses one graph: what is in it, when to use it, which rules to
keep. The maintainer writes it as a template; `graph` fills in the model
part from the graph's live ontology whenever it installs the skill, so a
new ontology release needs no new skill. You write the curated part.
How to publish it is in the `maintaining-graphs` skill; this skill is
about the text.

## Where it lives

In a workspace the skill is `skill/graph-<graph>/` in the graph's folder
(`graph-admin workspace sync` pulls it, `graph-admin graph push` publishes
it). The directory holds `SKILL.md` and optional `references/*.md`.

## The template

`SKILL.md` starts with frontmatter, then curated prose with block markers.
A line holding only `<!-- graph_cli:block <name> -->` is replaced by a block
generated from the graph's model:

| Block | Renders |
| --- | --- |
| `start` | to run `graph start --json` once per session if not done yet (it refreshes stale skills and gives the counts and the search commands) and to load the `graph-cli` skill first; how to tell the graph is available here and which name to pass as `--graph`; the graph language; the rules of working with `graph` that hold from the first call, so the skill works even when an agent loads it alone; one clause that the graph has views (`graph describe views`) when it has some the reader can call. Do not repeat them in the curated text |
| `modules` | for a graph with several modules: one row per subject area (module, prefix, summary, number of types, links to other modules), the commands to read one module and, when the graph has functions or actions, one line that `graph describe module` lists a module's functions and actions; replaces `types` and `relations` |
| `types` | one row per entity type: name, alias example, summary, supertypes, fields (enum values inline, `required`, `many`, `unique`, write-once, defaults, their name roles), states and transitions |
| `relations` | relations from or to those types, each with its `graph query` pattern, cardinality, characteristics and qualifiers (inverse labels, which no command takes, only in `references/model.md`) |
| `constraints` | transition guards and rules with severity (write-once fields too when the template has no `types`) |
| `names` | the alias format and which fields `graph lookup` matches; next to `types` a pointer to the name roles marked there; on a modular graph one example and pointers, not every type. Tells agents to use a candidate only on exactly one exact match, else to ask |
| `model` | a pointer to `references/model.md`, the full model, written for every generated skill |
| `functions` | the graph's named functions: at most 12 of them (the ones marked `featured: true` in their file, by module then name) with signature and summary, and the commands to run, list and search them (further functions are not listed and nothing warns about them); one line saying so when there are none, and one line saying that functions are not for this token when the reader may not `read.function` |
| `actions` | the graph's named write actions: at most 8 of them (the ones marked `featured: true` in their file, by module then name, that the reader may run: an action is left out when a write it needs is not allowed to the actor that installs or renders the skill) as `name(signature)` with summary (`?` marks an optional parameter, "Needs `--reason`" an action with `reason: required`), the rule to dry run first (`graph do <action> --param <name>=<value> --dry-run`) and the commands to list, search and describe them; one line saying so when there are none |

Each block at most once. Put `functions` where agents decide how to answer
a question, after the model and before the recipes, and `actions` next to
it, before the curated rules for writing. Which functions and actions
appear is decided in their files (`featured`, see `authoring-graph-functions`
and `authoring-graph-actions`), not in the template. Do not repeat a write
recipe in the curated text that an action carries: say when to use the
action and why the rule behind it exists, and leave the steps to the
action. Without any marker the skill is `static`: it is
installed as it is and goes stale with every ontology release, so use
markers. Never write a file `references/model.md` yourself.

No count, list or page of functions, actions or views is generated into the
skill: a module page ends with one line pointing to `graph describe module`,
and `references/functions.md` and `references/actions.md` are not written any
more (both names stay reserved: a file of that name in a template is refused,
like `references/model.md`). So publishing, changing, deprecating or removing
a function, action or view that is not `featured` leaves every installed
skill as current as it was; only a change to a featured entry (or the first
or last function, action or view of the graph) makes installed copies stale.
The skill lists only the featured entries; everything else is found with
`graph start` and `graph describe search`.

### Modular graphs

A graph with two or more modules besides the built-in ones is modular. Use
`modules` in the template instead of `types` and `relations`: those still
work, but they list every type into `SKILL.md`, which grows with the model.
For a modular graph `graph` also generates `references/modules/<module>.md`
(one page per module: its types, relations inside, links to other modules,
rules) and turns `references/model.md` into an index of them. Both are
generated: a file of your own below `references/modules/` is refused, like
`references/model.md`.

The module list comes from the module summaries, so a vague summary in the
ontology is a vague skill: fix it there
(`ontology-best-practices`, modeling). What stays yours is the curated part:
when to use the graph, its purpose, recipes, and judgement rules the model
cannot express, for instance which module a new kind of fact belongs in.
Do not copy module summaries or type lists into the text.

Check what agents will see, on the installed graph, with `graph describe
modules`, `graph describe module <module>` and `graph describe search
<text>`, next to the rendered preview (below).

A good skeleton:

```markdown
---
name: graph-<graph>
description: "<What the graph holds, in the words a user would use>. Use when <the situations and requests>. Do not use for <what belongs elsewhere>."
---

# Graph `<graph>`: <what it is for>

## Is it available here?

<!-- graph_cli:block start -->

## What is in it

<!-- graph_cli:block types -->

<!-- graph_cli:block relations -->

<!-- graph_cli:block constraints -->

<!-- graph_cli:block model -->

## Rules for you

- <conventions the model cannot express, true for every request>
- Before you <a recurring job, such as entering one kind of document>, read
  [references/<job>.md](references/<job>.md).

## Reading

<!-- graph_cli:block functions -->

<!-- graph_cli:block names -->

<a few graph commands for the questions no function answers>
```

## What the curated text is for

The blocks already say what the model says. Write what it cannot:

1. **The description decides when the skill is loaded.** Name what the
   graph holds in the words users use (synonyms, abbreviations), the
   situations to use it in, and what not to use it for. The renderer appends
   "Run `graph start` first and load graph-cli with it." (52 characters with
   its space), so do not write that sentence, nor a "Needs the graph-cli
   skill." of your own: the appended one says it. At most 1024 characters
   with the sentence, so write at most 972 characters, on one line (or as a
   block scalar); publish refuses a description that cannot take the
   sentence (rule `description`, below).
2. **Rules for you**: how to decide between two types, which values a
   field takes in practice, when to create versus update, language and
   wording of values, and the cases of this graph where the general rules
   bite: which lookup finds the entity before a write, which changes need
   consent beyond a retraction or a move into a terminal state. The `start`
   block already says to look a name up before a create, to say why in
   `--reason` and to ask before `retract` and a terminal `transition`; do
   not repeat that. Name where the line between subject matter and the work
   on the data runs in this graph, with an example of each. One rule per
   bullet, imperative, with the reason when it is not obvious.
3. **Recipes**: the three to six questions agents ask most that no
   function answers, each as one `graph` command (`graph find`, `graph
   lookup`, `graph neighbors`, `graph query`), and one write as `graph
   apply` or `graph create` when no action carries it. A question that
   recurs is a candidate for a function (`authoring-graph-functions`), not
   for a longer recipe list; publish warns about a recipe a function answers
   and about an example that names nothing of the graph (recipe lint, below).
   More recipes go into `references/recipes.md`, linked from `SKILL.md`.
4. **Keep it short.** Every generated skill has a budget: `SKILL.md` after
   rendering at most 20,000 bytes (2,400 more with published functions,
   1,950 more with actions), and the curated text of `SKILL.md` at most
   6,000 bytes. `SKILL.md` is read in full every time the skill loads, so
   keep there only what holds for every request: when to use the graph,
   its scope, and the rules no schema says. Rules for one recurring job
   (entering one kind of document, building one kind of entry) and
   glossaries go into `references/<job>.md`, with one line in `SKILL.md`
   that says when to read it. A recipe that a function covers does not
   belong in the text at all: the `functions` block lists it when it is
   `featured`, and `graph start` and `graph describe search` find the rest. Hard limits:
   `SKILL.md` at most 500 lines, the whole tree at most 256 KB.
5. **Rules as criteria, not values.** Say what decides a case and give
   the test question; do not list values from the data. A rule that pulls
   against the model (an "always link X" for a relation the ontology
   makes optional) misleads; say when it applies.
6. **Write for every reader.** Readers with read limits get the generated
   blocks without the types and the marked attributes they may not see, but
   your text reaches every reader as written: name no type, relation,
   function or marked attribute (its name, an example value, a rule that
   mentions it) that some reader of the graph may not see.

## What publishing checks

`graph-admin graph skill render` and `graph-admin graph push` refuse a
skill that breaks any of these (`admin.skill_invalid`, `details.problems`
names the file and the rule: `template`, `description`, `term`, `command`,
`alias`, `function`, `action`, next to the tree rules):

- `name` in the frontmatter equals the directory name `graph-<graph>`;
  regular UTF-8 files only, no dotfiles, no symlinks; `SKILL.md` at most
  500 lines and the tree at most 256 KB.
- A template renders against the current ontology pin exactly as `graph`
  will render it, and its curated text is checked against the model and
  the `graph` inventory (the rules below).
- The description of a template takes the sentence the renderer appends
  (rule `description`): with it, at most 1024 characters, so the curated
  description has at most 972; and it is on one line or a block scalar, not
  a plain or quoted scalar wrapped over lines. A description that names
  `graph start` itself gets no sentence and is not checked for it. A graph
  release pushed with an unchanged skill checks the published one too.
- No maintenance vocabulary and no local paths: the skill is for agents
  that use the graph, not for maintainers. No `graph-admin`, no `maint.*`
  or `admin.*` codes, no data root option, no absolute or home paths.
- Every prefixed name (CURIE) with one of the model's prefixes names a
  real type, relation, enum, lifecycle or datatype of the pinned release.
- Every `graph <command>` in code names a real command and only its
  options.
- Aliases with the graph's prefix have the live format (`ITEM-0001`, not
  `ITEM-1`); in code, an alias of four or more digits with a prefix the
  graph does not use is reported too.
- Each block name is known and used once.
- Every `graph run <name>` in code names a published function (deprecated
  ones count; a placeholder such as `<function>` is not a name and is not
  checked; in a workspace push the functions of the same push count).
  Publish the functions first or in the same push, else the rule `function`
  refuses the skill.
- Every `graph do <name>` in code names a published action, by the same
  rules, else the rule `action` refuses the skill.

Not refused, but warned (`admin.skill_over_budget`, with the sizes in
`details` and in `data.size` of `render`; publish and push warn too): a `SKILL.md` or a curated text over its budget (above). Nothing
is refused for its size below the hard limits. `data.size` lists the bytes
of `SKILL.md` and of the curated text, both budgets and the bytes of each
block, largest first. A single-module graph lists its whole model in
`SKILL.md`, so it reaches the budget as its model grows: move text into
references, or split the model into modules.

**Recipe lint, warned and never refused** (`render`, `publish`, `graph-admin graph push`;
one record per code, up to 20 findings with file and line): `admin.skill_recipe_covered`
(a `graph query` recipe a published function answers; the finding gives the `graph run`
call), `admin.skill_generic_example` (a fenced `graph` example that names nothing of the
graph; the `graph-cli` skill and the `start` block hold it) and `admin.skill_recipe_broken`
(a recipe that fails against the model). Rules and what to do with a finding:
[references/recipe_lint.md](references/recipe_lint.md).

When a later ontology release removes a name the curated text uses, agents
get the warning `skills.source_unknown_names`: fix the text and push. The
same warning follows when a function or action the text runs is later deleted.

## The loop

1. Edit `skill/graph-<graph>/` in the graph's folder.
2. Preview: `graph-admin graph skill render <graph> <folder>/skill/graph-<graph>
   --out <scratch dir> --force`, then read `<scratch dir>/graph-<graph>/`
   as an agent would: is it clear when to use the graph, and what to do?
   `data.size` tells how much of the budget each block and the curated
   text take.
3. Fix what `render` reports (exit 1 lists the problems; the preview is
   written anyway).
4. Publish as described in `maintaining-graphs`. Agents get the new text
   with `graph start` (it refreshes stale copies; `graph self update` and
   `graph skills install` do too) where they work.

Test it: give an agent with the installed skill a few real requests and
watch whether it loads the skill, picks the right types and follows the
rules. Turn every mistake into a rule or a sharper description.
