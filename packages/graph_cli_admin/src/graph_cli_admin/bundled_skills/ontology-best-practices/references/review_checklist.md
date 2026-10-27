# Review checklist

Go through this before a first `graph create` and before every
`ontology release`. Every "no" is either fixed or written down as a
deliberate choice in the README next to the ontology.

## Purpose

- [ ] Every new term serves a competency question, and every question has
      a path through the model.
- [ ] Built-in modules and standard vocabularies were checked before
      inventing a term; matches are recorded under `mappings`.

## Terms

- [ ] Each class passes the subclass test and differs from its siblings
      in structure, not only in name.
- [ ] Prescriptions and occurrences are separate classes.
- [ ] Enums are small closed lists; values that need names, links or
      growth are classes.
- [ ] States with allowed moves are lifecycles; record state (withdrawn,
      replaced) is left to retract and supersede.
- [ ] No derived values are stored; no relations hide in strings; no
      catch-all relation.
- [ ] No catch-all note and no author or change-log attribute: each fact
      has its field, the work on the data is the history.
- [ ] Every relation has `domain`, `range`, an `inverse_label` and the
      cardinality the domain really has.
- [ ] Every relation has a decided `on_target_inactive`: `refuse` where a
      source makes no sense without its target, `allow` for history and
      provenance.
- [ ] Facts about a link are qualifiers; links with their own life are
      classes.

## Modules

Only for a graph with more than one module of its own.

- [ ] The model is cut into subject areas because it needs to be (several
      areas, owners or a size an agent cannot read at once), one module
      per area.
- [ ] Every module `summary` says what the area holds and what it does
      not.
- [ ] Imports point from the dependent area to the one it builds on; no
      cycles.
- [ ] A relation between two areas lives in the importing module, or in a
      small bridge module when both need each other.
- [ ] The main module holds no terms of its own and imports every area.
- [ ] Any change in a module is matched by a bump of the main module
      (the graph's version).
- [ ] A term that moved to another module is released as major with a
      data migration (removal plus addition; `renamed_from` only works
      within one module).
- [ ] No rule is a prose prohibition between areas: what may not be
      linked is left out of `domain` and `range`.

## Names and descriptions

- [ ] Names follow the conventions; relations read from source to target.
- [ ] Every summary is one sentence that tells a newcomer what the term
      is and how it differs from its neighbors.
- [ ] Classes people name have a `label`; abbreviations and other names
      are `alt_label`, misspellings `hidden_label`.
- [ ] Label-role attributes are named by role, the same in every class:
      `alt_labels`, `hidden_labels`.
- [ ] Entities of one class that share names in real data get a main
      name that tells them apart (else every write warns
      `possible_duplicate`).
- [ ] Concrete classes people refer to have their own `display_prefix`.
- [ ] No summary or description holds values from real data.

## Rules

- [ ] What must always hold is `required`, cardinality or an error rule;
      plausibility checks are warning rules.
- [ ] Anything someone must see while working is a `write` rule.
- [ ] Rules over `sum` guard against absent sums in `if`.
- [ ] Every rule and guard was made to fire once on a throwaway graph.

## Release

- [ ] `ontology validate` reports no findings.
- [ ] `ontology diff` levels were read one by one; the version bump
      matches.
- [ ] `graph-admin graph check <graph> --ontology <dir>` reports no
      errors, and its warnings were read.
- [ ] A major change was tried on a restored backup copy and the existing
      data passes.
- [ ] Deprecated terms name their replacement in the summary.
- [ ] Backup first, `audit verify` after, graph skill and README updated.
