# Modeling

How to decide what becomes a class, attribute, relation, enum, lifecycle
or rule. The keys themselves are in [ir_format.md](ir_format.md).

## Start from questions

- Write the competency questions first, each as a question a user or an
  agent would ask the graph ("Which tested recipes use ingredient X?").
- For every question, name the path through the model that answers it:
  start type, relations, filters. A question without a path shows a
  missing term; a term on no path is a candidate to drop.
- Model the smallest slice that answers the questions; add the rest when a
  real question needs it. A minor release later is cheap, a wrong term in
  data is not.

## Reuse before inventing

| Need | Use |
| --- | --- |
| a newer entity replaces an older one, an entity is withdrawn | `graph supersede` and `graph retract` (`core:supersedes`, `core:retracts`); no own "obsolete" state or flag |
| where a fact came from | `core:source`, or `core:derived_from` for a specific revision |
| a statement with evidence and confidence | `provenance:Claim` with `provenance:supported_by` and `provenance:contradicts` |
| stored documents, pages inside them | `files:File` (subclass it for a domain meaning) and `files:from_file` with `page`, `last_page`, `locator` |
| when and why an entity changed, who wrote it, how a value was read | nothing: every revision is kept with the `reason` of its write, `graph history` shows them |
| a date of which only the year or the month is known | the date with a precision attribute (an enum such as day, month, year), as Wikidata does; never a silent stand-in date |
| names, other names, misspellings | label roles (`label`, `alt_label`, `hidden_label`; SKOS prefLabel, altLabel, hiddenLabel) |
| a concept tree | a self relation such as `broader` with `acyclic` (SKOS broader) |
| alignment with schema.org, Dublin Core, PROV-O, SKOS | `mappings` on the term |

## Choosing the construct

| It is... | Model it as |
| --- | --- |
| a kind of thing with members that others point to | a class |
| one concrete thing | an entity in the data, never a class |
| a value describing one entity | an attribute |
| a link between two entities | a relation |
| a fact about one link (amount, page, role in it) | a qualifier of the relation |
| a link with its own lifecycle, several attributes or links of its own | a class in between (a reified relation) |
| a small, closed classification only used to filter | an enum |
| a classification whose values need names, other names, a hierarchy, links of their own, or that grows without a release | a class (with a tree relation if needed) |
| a state with allowed moves between states | a lifecycle |
| a value that follows from other values (a sum, a count, an inherited value) | nothing stored; a rule checks it, a query or script computes it |
| a responsibility (owner, reviewer) | a relation to the entity that holds it, never a name in a text field |

Test for enum versus class: would anyone ever want to `lookup` the value
by another name, link something to it, or add a value without asking the
maintainer? Then it is a class.

**Roles are relations, not copies.** One person or organization acting in
several roles is one entity with one edge per role it holds (`teaches` to a
course, `leads` to a school), never one node per role joined by a `same_as`
or "identical with" link: such copies split every query, every count and
every history in two. A role becomes a class of its own only when it has a
validity period, attributes or links of its own (a term of office, a
position that outlives its holder); the entity then links to it. Agents add
a second role to the existing entity when they `lookup` its name before a
create, so give the class label fields
([identity and naming](#identity-and-naming-of-data)).

## Inheritance

- **Subclass test.** "Every X is a Y" holds without exception. "Part of"
  is a relation, never `is_a`.
- **Only when the structure differs.** A subclass earns its place with
  other attributes, relations or rules. Kinds that differ only by name are
  an enum attribute on one class.
- **One axis.** Inheritance is single. When things vary along two axes
  (for example where something happens and what form it takes), inherit
  along the axis that changes the structure and make the other an enum or
  a relation.
- **Abstract parents** carry shared attributes and relations; `validate`
  warns about an abstract class without subclasses.

## Prescription and occurrence

What is planned, prescribed or defined and what actually happened are
different classes: a recipe and each time it was cooked, a course
syllabus and a course run, a maintenance plan and a maintenance visit.
The definition carries what is the same every time; the occurrence
carries dates, results and who took part, and points to its definition.
Merging them forces optional fields on both and loses the second
occurrence.

## Trees and parts

- A tree is a relation from child to parent with `cardinality: {out:
  {max: 1}}` and `acyclic`. A child points only to its parent, not to the
  root: there is then one place to change when it moves.
- Give that relation `on_target_inactive: refuse` when a child makes no
  sense without its parent: a parent then cannot be retracted or
  superseded while children point at it
  ([rules](rules_and_lifecycles.md#edges-to-inactive-targets)).
- Store each node's own segment (a name, a relative path), never a value
  built from its ancestors: renaming a parent then changes nothing below.
- Values given once at a node apply to the nodes below it unless they
  override them. Do not copy them down; resolve them when reading.
- Load a tree in one `graph apply` change set when rules sum or count
  along it, so the check sees the whole tree.

## Identity and naming of data

- Mark natural keys `unique` (a code printed on a thing, a stable
  external number). Do not invent uniqueness that the real world lacks.
- Give every class people refer to a `display_prefix`, so its entities
  get short aliases.
- Give every class people search for a `label` attribute, and
  `alt_label` for abbreviations and other names
  ([naming_and_descriptions.md](naming_and_descriptions.md#label-roles-and-lookup)).

## Time

- The graph keeps every revision with its time; do not add "created on"
  or "changed on" attributes.
- Model a date when it belongs to the domain: when something happened,
  is due, or was valid.
- A value that changes and whose old values matter is a new entity that
  supersedes the old one, or an occurrence class, not an overwritten
  attribute.

## Provenance and documents

- A fact taken from a document points to the stored file with
  `files:from_file` and the page where it is. Each entity gets its own
  edge with its own page when parts of one document are spread over pages.
- Statements that can turn out wrong, with evidence for and against, are
  claims, not attributes.
- Uncertain or unreadable values stay absent; the `reason` of the write
  says why. Never guess a value to satisfy a required attribute.
- What a document says is subject matter in a graph about documents (a
  mark, a margin note, an error in a printed solution): give it a field.
  How it was read (a page image, a text extraction, what was checked) is
  not: it goes into the `reason` of the write.

## One graph or several

- One graph per purpose or visibility. Two purposes in one graph mix
  their permissions and their skill.
- A graph does not grow too big for one ontology through more data; it
  grows too big through unrelated questions.
- Edges stay inside one graph. A reference into another graph is an
  `external_ref` attribute, which is never checked; use it rarely.
- Several modules in one graph make sense when one part is reused by
  another graph or when the model is cut into subject areas (next
  section); otherwise keep one module.
- A subject area that needs other permissions or another owner or
  lifecycle is a separate graph, not a module: modules share one graph's
  policy, data and release.

## Splitting into modules

A module holds the terms of one subject area. The module `prefix` is the
CURIE prefix, so membership is visible in every name. Agents explore a
modular graph top down (`graph describe modules`, then `graph describe
module <module>`, then `graph describe type <type>`), so the cut decides
what they read and when.

When to split:

- the model has several subject areas that answer different questions;
- different people maintain different parts;
- the model no longer fits an agent's context in one piece.

A small model stays one module; splitting it only adds files and imports.

How to cut:

- **One module per subject area**, named for what it holds (`catalog`,
  `members`, `lending`), not for a layer (`base`, `misc`, `common`).
- **A thin main module** (the umbrella) imports the areas, holds no terms
  of its own and carries the graph's release version: the bundle version
  is the main module's version, so every release bumps it. Areas keep
  their own versions for their own history.
- **Imports point from the dependent area to the one it builds on**
  (`lending` imports `members` and `catalog`, which import nothing from
  it). No cycles; a cycle means the cut is wrong.
- **A relation between two areas lives in the importing module.** When
  both areas need each other, put the relation in a small bridge module
  that imports both, instead of a cycle.
- **The module `summary` is what agents read first** in `graph describe
  modules`. Say what the area holds and what it does not, and name the
  neighbor that holds the rest ("Items and their copies; not the people
  who borrow them.").
- **Prefer positive `domain` and `range` over prose prohibitions.** A
  sentence such as "a copy is never linked to a member" is not checked; a
  relation with a `domain` and `range` is, and the write path refuses a
  wrong edge and names the relations that do connect the two types.
- **Moving a term to another module changes its CURIE.** That is a removal
  plus an addition, so a major change with a data migration: add the term
  in the new module, move the data, then remove the old term.
  For a class or relation, `renamed_from` may name the old term by its CURIE
  (`renamed_from: old:Name`), which `ontology diff` reports as a rename
  instead of a removal plus an addition
  ([versioning](versioning_and_release.md#releasing-a-modular-graph)).

## Anti-patterns

| Anti-pattern | Problem | Instead |
| --- | --- | --- |
| everything "just in case" | cost without a question | competency questions |
| a catch-all relation (`related_to`) | no meaning, cannot be queried | a named relation per meaning |
| relations hidden in strings ("room 3, building B") | not linkable, not checkable | entities and relations |
| a list of names in a text field | no lookup, no links | a relation to entities |
| instances as classes | class explosion | entities |
| one class per value of a category | release for every new value | an enum or a category class |
| stored sums, counts and copies of parent values | drift from the source | rules and computed reads |
| a status enum with allowed moves | moves not checked | a lifecycle |
| an own "deleted" or "obsolete" flag | duplicates what retract and supersede do | `graph retract`, `graph supersede` |
| an attribute named after a person or a version | changes with people and releases | a relation, or a release |
| a catch-all `notes` or `remarks` text | collects to-dos, how data was read and copies of fields; nothing in it is checked or queried | a field per fact that matters; the work on the data in the `reason` of the write |
| an author, creator or "generated by" attribute | duplicates the history and is the same value on every entity | `graph history` (the writer is visible to the maintainer) |
| statements about the graph itself in entities ("topic X is still missing") | not subject matter, outdated once fixed | close the gap; keep open work outside the graph, such as in the README next to the ontology |

## Coming from RDF, OWL or SHACL

| Notion | Here |
| --- | --- |
| class, `rdfs:subClassOf` | class, `is_a` (single inheritance) |
| datatype property | attribute |
| object property, inverse | relation, `inverse_label` |
| individual (ABox) | entity in the graph's data |
| `owl:imports`, namespace, prefix | `module.imports`, `module.namespace`, `module.prefix` |
| SHACL shapes (closed world) | `required`, `many`, `unique`, cardinality, rules: checked when data is written |
| OWL inference | none; nothing is derived. `domain` and `range` are checks, not inferences |
| `owl:TransitiveProperty`, `owl:SymmetricProperty` | `transitive`, `symmetric`: declared only, queries do not follow them |
| SKOS labels, `skos:broader` | label roles, an own `acyclic` tree relation |
| `owl:equivalentClass`, SKOS mapping properties | `mappings` (`exact`, `close`, `related`, `broad`, `narrow`) |
| `owl:deprecated` | `deprecated` |
| `owl:versionInfo` | `module.version` (semantic versioning, checked on release) |
