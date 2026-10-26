# The ontology format `graph_cli.ir/1`

Reference of every key. The complete example at the end compiles without
findings; start from it. Judgment about what to model is in
[modeling.md](modeling.md).

## Files

- One module per file, `<module>.yaml`; the file name is the module id.
- One directory per version, for example
  `data/<graph>/ontology/1.2.0/<module>.yaml`. A directory may hold
  several modules of one graph; name the main one with `--module` then.
- A released version is never edited: copy the directory to the next
  version and change the copy. Releasing a version again with other
  content is refused.
- Commands: `graph-admin ontology validate <dir>`, `graph-admin ontology
  diff <dir> --graph <graph>` (or `--against <old dir>`), `graph-admin
  ontology release <graph> <dir>`, `graph-admin graph create <graph>
  --ontology <dir>`.

## YAML rules

The loader is strict:

- No duplicate keys, anchors, aliases, tags or multiple documents.
- No implicit typing: `true`, `1` and `null` are read according to the key
  they belong to.
- A comma ends a plain value inside `{...}`: `{summary: Used, works.}` is
  an error. Quote the value or use block style when a summary contains a
  comma or a colon.
- Unknown keys are errors (`model.structural.unknown_key`); the finding
  lists the allowed keys.

## Names and references

| Name of | Grammar |
| --- | --- |
| module id | `lower_snake_case`, up to 63 characters |
| module `prefix` | `lower_snake_case`, up to 16 characters |
| class, datatype, enum, lifecycle | `UpperCamelCase`, letters and digits |
| attribute, relation, qualifier, enum value, state, guard and rule id | `lower_snake_case` |
| `display_prefix` | upper case letters and digits, up to 8, starting with a letter |

Inside a module, refer to its own terms by local name (`Recipe`) and to
imported terms by CURIE, `<prefix>:<Name>` (`files:File`,
`provenance:supported_by`), also in conditions. Data and the `graph` CLI
always use CURIEs (`rcp:Recipe`).

## Top level

A module file has the keys `format`, `module` and, each optional,
`datatypes`, `enums`, `classes`, `relations`, `lifecycles` (each a map
from term name to definition), `rules` (a list), `mappings` and
`extensions`.

## Module header

| Key | Meaning |
| --- | --- |
| `format` | always `graph_cli.ir/1` |
| `module` | the header below |
| `id` | module id, equal to the file name |
| `prefix` | CURIE prefix of the module's terms; changing it later is major |
| `version` | `MAJOR.MINOR.PATCH` |
| `namespace` | IRI namespace for exports; default `urn:graph_cli:onto:<module>#`; set it only for a namespace you control |
| `imports` | list of `{module, version}`; `version` is a range: exact `1.2.3`, caret `^1.2`, tilde `~1.2` |

`core` is imported by every module. `provenance` and `files` are opt-in
(see [Built-in modules](#built-in-modules)).

## Documentation keys

Every term (module, datatype, enum, class, attribute, relation,
qualifier, lifecycle, rule, guard) takes:

| Key | Meaning |
| --- | --- |
| `summary` | required one sentence; what agents read (see [naming_and_descriptions.md](naming_and_descriptions.md)) |
| `description` | optional longer text; shown on the type pages of a generated graph skill |
| `deprecated` | `true` marks a term as on its way out; `graph describe` shows it, a generated graph skill puts "Deprecated." before the summary, and a write that uses a deprecated class, attribute, datatype, enum, lifecycle or relation gets the warning `data.structural.deprecated_term` |
| `annotations` | free JSON under a namespace key (`annotations: {ui: {color: green}}`); for other tools, ignored by graph_cli |

Enum values and lifecycle states take only a `summary`.

## Datatypes

A named datatype narrows a `base` type with facets:

```yaml
datatypes:
  Rating:
    summary: A rating from 1 (poor) to 5 (excellent).
    base: integer
    min: 1
    max: 5
```

| Base type | Values in data | Facets |
| --- | --- | --- |
| `string` | text, usually one line | `min_length`, `max_length`, `pattern`, `single_line` |
| `text` | longer text | `min_length`, `max_length`, `pattern`, `single_line` |
| `lang_string` | text in a named language, `{"lang": <BCP 47 tag>, "value": <text>}` | `min_length`, `max_length`, `pattern`, `single_line` (on the text) |
| `lang_text` | longer text in a named language, same shape | `min_length`, `max_length`, `pattern`, `single_line` (on the text) |
| `integer` | a JSON integer, no floats | `min`, `max` |
| `decimal` | a decimal string such as `"12.5"` | none |
| `boolean` | `true` or `false` | none |
| `date` | `YYYY-MM-DD` | none |
| `datetime` | an ISO 8601 date and time with offset | none |
| `duration` | an ISO 8601 duration such as `PT20M` | none |
| `iri` | an IRI with a scheme | `min_length`, `max_length`, `pattern` |
| `json` | any JSON value | none |
| `external_ref` | `{"graph": <graph id>, "entity": <entity id>}`, a reference into another graph, never checked | none |
| `blob` | the hash of a stored file; attributes only | none |
| `local_path` | a file path relative to a named root; attributes only | none |

`pattern` is a regular expression that must match the whole value; anchor
it (`^...$`). `json`, `external_ref` and `local_path` are not scalars: no
`unique`, no `default`, no facets.

`lang_string` and `lang_text` hold one language per value, as RDF
language-tagged strings: a tag such as `de`, `en` or `de-CH`, stored in
canonical case. A value written without a tag takes the graph language
(`graph-admin graph create --language`); a graph without one refuses it. A
`default` needs a tag: `default: '"unknown"@en'`. `unique` compares text
and tag together.

## Enums

```yaml
enums:
  Course:
    summary: Where a dish belongs in a meal.
    values:
      starter: {summary: Served first.}
      main: {summary: The main dish.}
```

`values` maps each value name to its `summary`; at least one value.

## Classes

| Key | Meaning |
| --- | --- |
| `attributes` | name to attribute (below) |
| `is_a` | one parent class (single inheritance); attributes and relations are inherited |
| `abstract` | `true`: no entities of this class itself, only of subclasses |
| `display_prefix` | alias prefix of new entities (`REC` gives `REC-0001`); not inherited, ignored on abstract classes; a class without one gets no aliases; unique among concrete classes (`validate` refuses a duplicate); a class renamed with `renamed_from` keeps the prefix of its old name, and a release does not give out a prefix another class holds (warning `admin.alias_prefix_taken`, the class gets no aliases) |
| `renamed_from` | the old name of a renamed class (major) |

A subclass may not redefine an inherited attribute. A class may extend an
imported class (`is_a: files:File`).

## Attributes

| Key | Meaning |
| --- | --- |
| `type` | a base type, datatype or enum; exactly one of `type` and `lifecycle` |
| `lifecycle` | a lifecycle; the attribute holds the state |
| `required` | must be present on every entity |
| `many` | a list of values |
| `unique` | no two active entities of the declaring class and its subclasses share the value; scalars only |
| `default` | a scalar used when a create omits the value; not with `many` or `lifecycle` |
| `mutability` | `content` (default), `immutable` (write once: cannot change or be removed once set), `operational` (housekeeping values that may change any time, such as a local path), `lifecycle` (implied by `lifecycle`; changes only through transitions) |
| `role` | `label` (the main name, one per class, single; on `lang_string` it may be `many`, one value per language), `alt_label` (other names, abbreviations), `hidden_label` (misspellings, never shown); on `string` and `lang_string` attributes only; `graph lookup` searches them |
| `renamed_from` | the old name of a renamed attribute (major) |

A lifecycle attribute with a single initial state is set to it on create.

## Relations

```yaml
relations:
  step_of:
    summary: The step belongs to the recipe.
    domain: Step
    range: Recipe
    cardinality: {out: {min: 1, max: 1}}
    inverse_label: has_step
```

| Key | Meaning |
| --- | --- |
| `domain` | class or list of classes an edge may start from; omitted: any class |
| `range` | class or list of classes an edge may point to; omitted: any class |
| `cardinality` | `out`: edges per source, `in`: edges per target, each `{min, max}`; `max` omitted is unbounded |
| `characteristics` | list of `acyclic` (no cycles; checked on write), `symmetric`, `transitive` (both declarative: recorded and exported, not computed by queries) |
| `on_target_inactive` | what a write meets that retracts or supersedes a target while active entities still point at it over the relation: `allow` (default), `warn`, `refuse`; see [rules_and_lifecycles.md](rules_and_lifecycles.md#edges-to-inactive-targets) |
| `inverse_label` | name for reading the relation backwards (`has_step`); unique across the bundle |
| `qualifiers` | name to `{type, required, many}` plus documentation; values on the edge; not `blob` or `local_path` |
| `renamed_from` | the old name of a renamed relation (major) |

`out` cardinality is checked on every write. `in` cardinality is checked
only in a batch check, `graph-admin graph check` or a major release (see
[rules_and_lifecycles.md](rules_and_lifecycles.md#when-checks-run)).

## Lifecycles

```yaml
lifecycles:
  RecipeLifecycle:
    summary: How far a recipe has been tried out.
    states:
      draft: {summary: Written down but not cooked yet., initial: true}
      tested: {summary: Cooked at least once and works.}
      retired: {summary: No longer cooked., terminal: true}
    transitions:
      - from: draft
        to: tested
        summary: Mark the recipe as cooked successfully.
        guards:
          - id: has_ingredients
            summary: A tested recipe lists its ingredients.
            require:
              - count: {rel: uses}
                minimum_value: 1
      - {from: [draft, tested], to: retired, summary: Stop cooking the recipe.}
```

| Key | Meaning |
| --- | --- |
| `states` | state name to `{summary, initial, terminal}`; at least one `initial`; every state reachable from one |
| `transitions` | list of `{from, to, summary, guards}`; `from` is a state or a list of states, `to` the state the move leads to |
| `guards` | list of `{id, summary, require}`; `require` is a list of [conditions](#conditions) that must all hold for the move |

A `terminal` state has no outgoing transition.

## Rules

```yaml
rules:
  - id: tested_recipe_has_time
    summary: A tested recipe states its total time.
    severity: error
    when: write
    scope: Recipe
    if:
      - {attr: status, equals_string: tested}
    then:
      - {attr: total_minutes, value_presence: PRESENT}
```

| Key | Meaning |
| --- | --- |
| `id` | rule name, unique in the module |
| `severity` | `error` refuses the write, `warning` reports it |
| `when` | `write` (every write of a matching entity) or `batch` (`graph-admin graph check` and a major release only) |
| `scope` | the class the rule applies to, subclasses included |
| `if` | optional conditions; the rule applies only when all hold |
| `then` | conditions that must all hold |

What to use when: [rules_and_lifecycles.md](rules_and_lifecycles.md).

## Conditions

A list of conditions holds when every item holds. An item is one of:

| Item | Form |
| --- | --- |
| attribute test | `{attr: <name>, <operator>: <value>}`; several operators in one item must all hold |
| count of edges | `{count: {rel, direction, where}, <operator>: <n>}` |
| sum over edges | `{sum: {rel, direction, attr, where}, <operator>: <n>}` |
| combination | `all_of`, `any_of` or `none_of` with a list of items: `{any_of: [...]}` |

| Operator | On | Holds when |
| --- | --- | --- |
| `value_presence` | attribute, sum | `PRESENT` or `ABSENT` |
| `nonempty` | attribute | `true`: present and not empty |
| `equals_string` | attribute | equal to the string (also enum values and states) |
| `equals_string_in` | attribute | one of a list of strings |
| `equals_number` | attribute, count, sum | equal to an integer or `{attr: <name>}` |
| `minimum_value` | attribute, count, sum | at least an integer or `{attr: <name>}` |
| `maximum_value` | attribute, count, sum | at most an integer or `{attr: <name>}` |

In `count` and `sum`: `rel` is the relation (CURIE if imported),
`direction` is `out` (default, edges of the entity) or `in` (edges
pointing to it), `where` filters the entities at the other end by
`record_status` (`active`, `superseded`, `retracted`) and `type`. `sum`
adds `attr`, a single `integer` or `decimal` attribute declared on every
class at the other end; the sum is absent when one of them lacks the
value, so test `value_presence: PRESENT` in `if` first. `{attr: <name>}`
compares with an attribute of the entity in scope.

## Mappings

```yaml
mappings:
  Recipe: {exact: ["schema:Recipe"]}
```

Term name to one or more of `exact`, `close`, `related`, `broad`,
`narrow` (SKOS mapping kinds), each a list of external CURIEs. Mappings
document how a term relates to a standard vocabulary; changing them is a
patch.

## Extensions

Top-level `extensions` holds free JSON under namespace keys, like
`annotations` but for the whole module. graph_cli ignores it.

## Reserved names

Attributes never use the system field names `id`, `type`, `rev`,
`record_status`, `alias`, `created_at`, `recorded_at`, `tx_id`, `tx_seq`,
`valid_from`, `valid_to`, `content_hash`, `ontology_version`. Qualifiers
never use `edge_id`, `rel`, `target`, `target_rev`, `valid_from`,
`valid_to`.

## Built-in modules

| Module | Holds | Use when |
| --- | --- | --- |
| `core` (always) | relations `core:supersedes`, `core:retracts` (written only by `graph supersede` and `graph retract`), `core:derived_from`, `core:source` | always available |
| `provenance` | class `provenance:Claim` (`statement`, `confidence`), relations `provenance:supported_by`, `provenance:contradicts` | statements whose evidence and confidence matter |
| `files` | class `files:File` (stored bytes, `title`, `media_type`, origin), relation `files:from_file` with qualifiers `page`, `last_page`, `locator` | the graph stores documents or points into them |

Import with `imports: [{module: files, version: "^1.0"}]`. Subclass
`files:File` for a domain meaning of a file (a scan, a contract).

## Complete example

```yaml
format: graph_cli.ir/1
module:
  id: recipes
  prefix: rcp
  version: 1.0.0
  summary: Recipes, their ingredients and steps, and each time one was cooked.
  imports:
    - {module: files, version: "^1.0"}

datatypes:
  Minutes:
    summary: A duration in whole minutes.
    base: integer
    min: 0
  Rating:
    summary: A rating from 1 (poor) to 5 (excellent).
    base: integer
    min: 1
    max: 5
  Name:
    summary: A name on one line.
    base: string
    single_line: true
    max_length: 200

enums:
  Course:
    summary: Where a dish belongs in a meal.
    values:
      starter: {summary: Served first.}
      main: {summary: The main dish.}
      dessert: {summary: Served last.}

lifecycles:
  RecipeLifecycle:
    summary: How far a recipe has been tried out.
    states:
      draft: {summary: Written down but not cooked yet., initial: true}
      tested: {summary: Cooked at least once and works.}
      retired: {summary: No longer cooked., terminal: true}
    transitions:
      - from: draft
        to: tested
        summary: Mark the recipe as cooked successfully.
        guards:
          - id: has_ingredients
            summary: A tested recipe lists its ingredients.
            require:
              - count: {rel: uses}
                minimum_value: 1
      - {from: tested, to: draft, summary: Rework the recipe.}
      - {from: [draft, tested], to: retired, summary: Stop cooking the recipe.}

classes:
  Recipe:
    summary: Instructions for preparing one dish.
    description: >-
      A recipe is the prescription; each time it is actually cooked is a
      separate Cooking.
    display_prefix: REC
    attributes:
      title:
        summary: Name of the dish as the recipe gives it.
        type: Name
        required: true
        role: label
      alt_labels:
        summary: Other names of the dish, including short names.
        type: Name
        many: true
        role: alt_label
      course:
        summary: Where the dish belongs in a meal.
        type: Course
        required: true
      total_minutes:
        summary: Time from start to serving, in minutes.
        type: Minutes
      servings:
        summary: Number of portions the recipe makes.
        type: integer
        default: 2
      status:
        summary: How far the recipe has been tried out.
        lifecycle: RecipeLifecycle
        required: true
  Step:
    summary: One step of a recipe, in the order it is done.
    display_prefix: STEP
    attributes:
      position:
        summary: Place of the step within its recipe, counted from 1.
        type: integer
        required: true
      instruction:
        summary: What to do in this step.
        type: text
        required: true
      minutes:
        summary: Time the step takes, in minutes.
        type: Minutes
  Ingredient:
    summary: Something a recipe uses, independent of the amount.
    display_prefix: ING
    attributes:
      name:
        summary: Usual name of the ingredient.
        type: Name
        required: true
        unique: true
        role: label
      alt_labels:
        summary: Other names of the ingredient, including regional ones.
        type: Name
        many: true
        role: alt_label
  Cooking:
    summary: One occasion on which a recipe was actually cooked.
    display_prefix: COOK
    attributes:
      cooked_on:
        summary: Day the dish was cooked.
        type: date
        required: true
        mutability: immutable
      rating:
        summary: How well the dish turned out.
        type: Rating
      comment:
        summary: What went well or should change next time.
        type: text
  PrintedRecipe:
    summary: A stored scan or photo of a printed recipe.
    is_a: files:File
    display_prefix: PRINT

relations:
  uses:
    summary: The recipe needs the ingredient.
    domain: Recipe
    range: Ingredient
    inverse_label: used_in
    qualifiers:
      amount:
        summary: Quantity with its unit as the recipe states it.
        type: Name
  step_of:
    summary: The step belongs to the recipe.
    domain: Step
    range: Recipe
    cardinality: {out: {min: 1, max: 1}}
    inverse_label: has_step
  cooking_of:
    summary: The cooking prepared the recipe.
    domain: Cooking
    range: Recipe
    cardinality: {out: {min: 1, max: 1}}
    inverse_label: cooked_as
  variant_of:
    summary: The recipe is a variation of the target recipe.
    domain: Recipe
    range: Recipe
    cardinality: {out: {max: 1}}
    characteristics: [acyclic]
    inverse_label: has_variant
  replaces:
    summary: The ingredient can stand in for the target ingredient.
    domain: Ingredient
    range: Ingredient
    characteristics: [symmetric]
    inverse_label: replaced_by

rules:
  - id: tested_recipe_has_time
    summary: A tested recipe states its total time.
    severity: error
    when: write
    scope: Recipe
    if:
      - {attr: status, equals_string: tested}
    then:
      - {attr: total_minutes, value_presence: PRESENT}
  - id: steps_fit_total_time
    summary: The steps of a recipe take no longer than its total time.
    severity: warning
    when: write
    scope: Recipe
    if:
      - {attr: total_minutes, value_presence: PRESENT}
      - sum: {rel: step_of, direction: in, attr: minutes}
        value_presence: PRESENT
    then:
      - sum: {rel: step_of, direction: in, attr: minutes}
        maximum_value: {attr: total_minutes}
  - id: ingredient_used
    summary: Every ingredient is used by at least one recipe.
    severity: warning
    when: batch
    scope: Ingredient
    then:
      - {count: {rel: uses, direction: in}, minimum_value: 1}

mappings:
  Recipe: {exact: ["schema:Recipe"]}
  uses: {close: ["schema:recipeIngredient"]}
```
