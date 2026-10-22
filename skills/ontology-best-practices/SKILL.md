---
name: ontology-best-practices
description: Design, write, review and release the ontology of a graph_cli graph (YAML format graph_cli.ir/1, checked by graph-admin ontology validate, diff and release). Use when planning a new graph, adding or changing classes, attributes, relations, enums, lifecycles or rules, deciding how to model something, choosing a version bump, or reviewing an ontology change.
---

# Ontology best practices

This skill is for whoever authors the ontology of a graph: what kinds of
entities a graph holds, their attributes, relations, states and rules.
Reading and writing the data of a graph is the `graph-cli` skill and the
graph's own skill; this one changes what a graph can hold. Ontologies are
authored by maintainers with `graph-admin` from a checkout of graph_cli.

Rules use MUST, SHOULD and MAY. Where this skill and the requirements of a
concrete graph disagree, the graph wins; record why.

## When an ontology pays off

A graph is worth it when three or more of these hold:

1. People or agents name the same thing differently, or one word means two things.
2. The questions are about relations: what depends on X, what belongs to Y.
3. Facts must be checked: required values, allowed states, sums that must add up.
4. New kinds of things and relations keep appearing.
5. Several sources are merged under one meaning.
6. An agent needs facts it can look up instead of guessing.

If a table, a tag list or a naming convention answers every question, use
that instead. A graph without someone who decides about its terms drifts.

## Workflow

1. **Purpose and competency questions.** Write 5 to 10 concrete questions
   the graph must answer, in a `README.md` next to the ontology, together
   with the design choices (step 7). No question, no term.
2. **Collect terms.** Nouns are candidate classes, verbs candidate
   relations, values candidate attributes. Check the built-in modules
   (`core`, `provenance`, `files`) and standard vocabularies before
   inventing ([modeling](references/modeling.md#reuse-before-inventing)).
3. **Smallest slice.** Model only what the questions need
   ([modeling](references/modeling.md)).
4. **Write and validate.** One directory per version, one file per module
   (`<module>.yaml`, the [format](references/ir_format.md)):
   `graph-admin ontology validate <dir>` until it reports no findings.
5. **Try it on a throwaway graph** before any real data: create a graph in
   an empty data root, load sample data, run every competency question and
   provoke every rule and guard once
   ([recipe](references/versioning_and_release.md#trying-an-ontology-on-a-throwaway-graph)).
   Fix the model, not the questions.
6. **Release.** A first version becomes a graph with `graph-admin graph
   create`; later versions go through `ontology diff` and `ontology release`
   with a verified backup first
   ([release](references/versioning_and_release.md)).
7. **Record.** Design choices with their reason in the README: what was
   modeled how, what was left out on purpose, what is still open.

Before step 6, go through the [review checklist](references/review_checklist.md).

## Core rules

1. Every term answers a competency question. Do not model "just in case".
2. Every class, attribute, relation, enum value, state, guard and rule has
   a `summary`: one sentence an agent can act on. It is what `graph
   describe` and the generated graph skill show
   ([descriptions](references/naming_and_descriptions.md#summaries-are-instructions)).
3. Names: classes, enums, datatypes and lifecycles `UpperCamelCase`
   singular; attributes, relations, values and ids `lower_snake_case`;
   relations read as a sentence from source to target (`step_of`, not
   `steps`). Identifiers are English; the values follow the graph language.
4. Subclass only if "every X is a Y" holds without exception and the
   subclass changes the structure (other attributes or relations). A
   difference in name only is an enum attribute, not a class.
5. A prescription and its occurrences are different classes: a recipe and
   each time it was cooked, a plan and its runs.
6. A thing other entities point to, that needs names and `lookup`, or that
   grows without a release, is an entity. A small closed list that only
   classifies is an enum.
7. A state with allowed moves is a lifecycle, not an enum: its changes go
   through `graph transition`, can carry guards and show in history.
8. Relations are typed and directed. No catch-all relation such as
   `related_to`. Facts about a link (amount, page, role) are qualifiers;
   a link with its own lifecycle or several relations is an entity.
9. Never store what can be derived: sums, counts, statistics and inherited
   values are computed when read, or checked by a rule.
10. Required, unique, outgoing cardinality and `write` rules are checked
    whenever data is written; use them for what must always hold. A
    warning rule reports, an error rule refuses. `batch` rules and
    incoming cardinality are checked only by `graph-admin graph check`
    and a major release, never on a write
    ([rules](references/rules_and_lifecycles.md#when-checks-run)).
11. Term names are stable. A released name is renamed only with
    `renamed_from` in a major version; prefer deprecating and adding.
12. Changes are additive by default (new optional attribute, new class,
    new enum value, looser limit = minor). Anything that can make existing
    data invalid is major and passes only if no existing data breaks
    ([versioning](references/versioning_and_release.md)).
13. Provenance is modeled, not hidden in text: where a fact came from is
    an edge (`core:source`, `files:from_file` with a page), how sure it is
    belongs to a claim (`provenance:Claim`).
14. Split into several graphs by purpose or visibility, not by size. One
    graph per purpose keeps skills and permissions simple.

## What the tool checks and what it leaves to you

| Checked by `ontology validate` and `release` | Left to judgment (this skill) |
| --- | --- |
| keys, names, types, references, imports | whether a term is needed at all |
| a summary on every term | whether the summary is clear and usable |
| inheritance and import cycles, unknown terms | class vs enum vs entity vs lifecycle |
| lifecycle shape: initial state, reachable states | whether the states match reality |
| label roles, unique on scalars only | which names people and agents search for |
| the version bump against the changes | when to deprecate instead of remove |
| existing data under a new major version | sample data and competency questions |

## References

| Task | Read |
| --- | --- |
| Look up a key, type, facet or condition of the YAML format | [ir_format.md](references/ir_format.md) |
| Decide how to model something; patterns and anti-patterns | [modeling.md](references/modeling.md) |
| Name terms, write summaries, choose label roles and aliases | [naming_and_descriptions.md](references/naming_and_descriptions.md) |
| Write rules and guards, decide error vs warning, write vs batch | [rules_and_lifecycles.md](references/rules_and_lifecycles.md) |
| Choose a version bump, deprecate, try and release a change | [versioning_and_release.md](references/versioning_and_release.md) |
| Review an ontology change before release | [review_checklist.md](references/review_checklist.md) |
