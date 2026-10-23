# Names and descriptions

Agents use a graph through what the ontology says about itself: `graph
describe` and the generated graph skill show names and summaries, and
nothing else explains the model to them. Write for a reader who has
never seen the domain.

## Names

- Classes, enums, datatypes and lifecycles: `UpperCamelCase`, singular
  nouns (`Recipe`, not `Recipes`).
- Attributes, relations, qualifiers, enum values, states and ids:
  `lower_snake_case`.
- A relation reads as a sentence from source to target: `Step step_of
  Recipe`, `Recipe uses Ingredient`. Its `inverse_label` reads the other
  way: `Recipe has_step Step`, `Ingredient used_in Recipe`.
- Attribute names say what the value is, with the unit if there is one
  (`total_minutes`, `weight_kg`).
- Identifiers are English, even when the data is in another language. The
  domain's own word goes into the summary in parentheses when users will
  use it ("A paper that comes with a delivery (German: Lieferschein).").
- No version, status, date, person or tool name inside a term name; they
  change, the name must not.
- Avoid names that mean different things to different readers; if one
  word has two meanings in the domain, use two terms with a qualifying
  name.
- A released name stays. A rename is major and needs `renamed_from`.

## Summaries are instructions

`summary` is required on every term and is often the only help an agent
gets. Good summaries:

- **One sentence** that says what the term is, in the domain's words.
- **Distinguish** neighbors: say what makes it this and not the similar
  term (`Ingredient`: "independent of the amount"; the amount is on the
  edge).
- **Relations**: describe one edge from source to target ("The step
  belongs to the recipe.").
- **Attributes**: say what goes in, the unit, and what absent means when
  it matters ("Time from start to serving, in minutes.").
- **Enum values and states**: the criterion for choosing it, not a
  synonym of the name.
- **Rules and guards**: the condition that must hold, stated positively.
  The rule summary is the message of the finding when the rule is
  violated ("The steps of a recipe take no longer than its total time.").
- **Deprecated terms**: name the replacement ("Use `rcp:Ingredient`
  instead."). The flag itself is shown: `graph describe` carries
  `deprecated` and a generated graph skill puts "Deprecated." before the
  summary.
- No examples taken from real data, no values of one particular case;
  describe the criterion so it holds for every case.

`description` holds what does not fit one sentence: the boundary to
another term, how to fill it in difficult cases. It shows on the type
pages of a generated graph skill.

Changing a summary or description is a patch: improve them whenever a
reader misunderstood one.

## Label roles and lookup

`graph lookup` finds entities by their names. It searches attributes with
a label role:

| Role | Holds | Shown |
| --- | --- | --- |
| `label` | the main name, single, one per class (inherited counts) | yes |
| `alt_label` | other names: abbreviations, short forms, synonyms, former names | yes |
| `hidden_label` | misspellings and spellings that should still be found | no |

Give every class people refer to by name a `label`. Add `alt_label` where
people use abbreviations or the data uses several names. Roles work on
`string` attributes and datatypes over `string`, not on `text`.

Name the attributes by their role, the same way in every class of the
ontology: `alt_labels` for the `alt_label` role and `hidden_labels` for
the `hidden_label` role (many-valued, as SKOS `altLabel` and
`hiddenLabel`). The main name keeps a domain name (`name`, `title`). One
name per role tells agents and people at a glance where other names go;
names chosen per class drift apart and can read like something else
(a person's names, abbreviations only).
Renaming a released attribute is a major change, so settle this before
the first release.

Label roles do more than `lookup`: `graph search` ranks matches in them
before other fields, and a write that gives an entity a name another
entity of the same class family already carries gets the warning
`data.semantic.possible_duplicate`. A class whose entities legitimately
share names (one per year, one per edition) needs a main name that tells
them apart, or the warning fires on every new one.

## Aliases

`display_prefix` gives new entities an alias such as `REC-0001`: short,
upper case, one per concrete class (`ontology validate` refuses two
concrete classes with the same prefix), readable in conversations and
logs.

- Set it on every concrete class people refer to; it is not inherited.
- Choose it for good: once entities have aliases, the prefix stays. A
  class renamed with `renamed_from` keeps it and its aliases count on; a
  new class cannot take over the prefix of a removed one (the release
  warns `admin.alias_prefix_taken` and the class gets no aliases).
- Aliases are for people. Change sets use them or `@local` names; nothing
  should parse meaning out of the number.

## Mappings to standard vocabularies

When a term matches a term of a known vocabulary, record it under
`mappings` with the kind of match (`exact`, `close`, `related`, `broad`,
`narrow`). It costs nothing (a patch), documents the meaning and helps a
later export. Common sources: schema.org (`schema:`), Dublin Core Terms
(`dcterms:`), PROV-O (`prov:`), SKOS (`skos:`), FOAF (`foaf:`).
