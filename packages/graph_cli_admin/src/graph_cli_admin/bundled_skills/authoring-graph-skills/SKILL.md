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
| `start` | how to tell the graph is available here and which name to pass as `--graph`; to load the `graph-cli` skill first |
| `types` | one row per entity type: name, alias example, summary, supertypes, fields, states and transitions |
| `relations` | relations from or to those types with cardinality and qualifiers, then the same as `graph query` patterns |
| `constraints` | transition guards, write-once fields, rules with severity |
| `names` | the alias format and which fields `graph lookup` matches per type |
| `model` | a pointer to `references/model.md`, the full model, written for every generated skill |

Each block at most once. Without any marker the skill is `static`: it is
installed as it is and goes stale with every ontology release, so use
markers. Never write a file `references/model.md` yourself.

A good skeleton:

```markdown
---
name: graph-<graph>
description: "<What the graph holds, in the words a user would use>. Use when <the situations and requests>. Do not use for <what belongs elsewhere>. Needs the graph-cli skill."
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

- <conventions the model cannot express>

## Reading

<!-- graph_cli:block names -->

<a few graph commands for the common questions>
```

## What the curated text is for

The blocks already say what the model says. Write what it cannot:

1. **The description decides when the skill is loaded.** Name what the
   graph holds in the words users use (synonyms, abbreviations), the
   situations to use it in, and what not to use it for. At most 1024
   characters; end with "Needs the graph-cli skill."
2. **Rules for you**: how to decide between two types, which values a
   field takes in practice, when to create versus update, what to look up
   before writing (an existing entity with a similar name), what needs the
   user's consent (state changes, retractions), language and wording of
   values. One rule per bullet, imperative, with the reason when it is not
   obvious.
3. **Recipes**: the three to six questions agents ask most, each as one
   `graph` command (`graph find`, `graph lookup`, `graph neighbors`,
   `graph query`), and one write as `graph apply` or `graph create`. More
   go into `references/recipes.md`, linked from `SKILL.md`.
4. **Keep it short.** `SKILL.md` at most 500 lines (aim for well under
   200 after rendering), the whole tree at most 256 KB.

## What publishing checks

`graph-admin graph skill render` and `graph-admin graph push` refuse a
skill that breaks any of these (`admin.skill_invalid`, `details.problems`
names the file and the rule):

- `name` in the frontmatter equals the directory name `graph-<graph>`;
  regular UTF-8 files only, no dotfiles, no symlinks.
- No maintenance vocabulary and no local paths: the skill is for agents
  that use the graph, not for maintainers. No `graph-admin`, no `maint.*`
  or `admin.*` codes, no data root option, no absolute or home paths.
- Every prefixed name (CURIE) with one of the model's prefixes names a
  real type, relation, enum, lifecycle or datatype of the pinned release.
- Every `graph <command>` in code names a real command and only its
  options.
- Aliases with the graph's prefix have the live format (`ITEM-0001`, not
  `ITEM-1`).
- Each block name is known and used once.

When a later ontology release removes a name the curated text uses, agents
get the warning `skills.source_unknown_names`: fix the text and push.

## The loop

1. Edit `skill/graph-<graph>/` in the graph's folder.
2. Preview: `graph-admin graph skill render <graph> <folder>/skill/graph-<graph>
   --out <scratch dir> --force`, then read `<scratch dir>/graph-<graph>/`
   as an agent would: is it clear when to use the graph, and what to do?
3. Fix what `render` reports (exit 1 lists the problems; the preview is
   written anyway).
4. Publish with `graph-admin graph push <graph> <folder> --dry-run`, then
   with `--reason "<why>"`, and `graph-admin audit verify`.
5. Agents get it with `graph self update` or `graph skills install` where
   they work.

Test it: give an agent with the installed skill a few real requests and
watch whether it loads the skill, picks the right types and follows the
rules. Turn every mistake into a rule or a sharper description.
