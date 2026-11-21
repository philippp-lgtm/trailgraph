# Recipe lint

`graph-admin graph skill render`, `graph-admin graph skill publish` and `graph-admin graph push` read
the curated text of a template (`SKILL.md` and the curated `references/*.md`) and warn about text that
teaches what the tool already carries. Nothing is refused: a warning is a question to the maintainer,
and a finding that teaches something about this graph stays. Each code is one record per command with
`details.findings` (at most 20, `details.more` counts the rest; each finding has `file` and `line`) and
the first three findings in the message. The lint never breaks the command it runs in: what it cannot
read leaves its finding out.

## `admin.skill_recipe_covered`

A `graph query` recipe (fenced or inline code) that a published, not deprecated function answers. The
finding names the function and the `graph run` call that replaces the recipe (`<ref>` where the recipe
gives an entity, the value where it compares with a constant, and the boolean switches the call must
set). Only the best function per recipe is named.

A function answers a recipe when all of these hold:

- Every parameter the function requires (no `default`) is supplied by the recipe. A `ref` is supplied
  by an anchor of the recipe on a field that names one entity (`id`, `alias`, a unique field or a label;
  a status, an enum or a number does not) compared with text or a parameter, whose class fits the class
  of the parameter. A `boolean` is always supplied. Any other parameter is supplied by a comparison of
  the same field with the same operator (`=` and `IN` alike; `CONTAINS`, `STARTS WITH` and the ordering
  operators each their own).
- The relationship pattern is the recipe's: every relationship the recipe requires is one of the
  function's, and every relationship the function requires is one of the recipe's. Without relationships
  on either side, the classes of the recipe's nodes must fit the function's.
- The function returns every column the recipe returns (a whole node counts for all its fields; a `max`
  of a field is not the field, nor a `collect` of it), aggregates when the recipe aggregates (also in a
  `WITH`), and is no bare total (a `RETURN` of aggregates only) unless the recipe is one. A `collect` or
  `count` next to a grouping column keeps a row per entity and answers a listing.
- Every filter the recipe applies is shown by the function (a column) or applied by it, so the caller
  can narrow the rows itself.
- The function filters no more than the recipe does: its filter fields are the recipe's, with the same
  constants (an `=` or `IN` of the function may list more values than the recipe asks for); it has a
  `LIMIT` only when the recipe has one; it negates no pattern the recipe does not (`WHERE NOT (a)-[:r]->()`
  would drop rows). A filter that only a boolean switch of the function applies
  (`WHERE $include_ended = true OR ...`) does not count: the call sets the switch, and the finding
  names it when the value is not the default. A relationship the recipe negates must be negated or walked
  by the function, so that its rows show whether it is there.

What to do with a finding: point the text at the function (one line in the rules or the module map) or
drop the recipe. Agents find functions with `graph start` and `graph describe search`; a `featured`
function is listed in the skill itself. If the function does not quite answer the question, change the
function, not the recipe.

## `admin.skill_generic_example`

A fenced `graph <command>` example that names none of: a type or relation (CURIE or local name), a field
(not `id`, `alias` and the other system fields), an enum value, a lifecycle state, a module id, a
function or an action of the graph. Only what names things counts: positional words, the values of
`--type`, `--rel`, `--where`, `--for` and similar options, and the field of `--set field=value`. Free
text (`--reason`), placeholders of the tool's own examples (`<type>`, `<id>`, `<file>`, `<name>`,
`<state>`, ...), the `--graph` value, data values, aliases and numbers name nothing. A placeholder the
tool's examples do not use (`<title> <kind>`, also in a `--title`) says something about this graph in
your words, so that example is kept. Inline code is not judged.

The `graph-cli` skill and the `start` block teach the commands themselves; an example earns its place
by showing something about this graph: which type to look up first, which relation to follow, which
value a field takes in practice.

## `admin.skill_recipe_broken`

A `graph query` recipe that does not parse or resolve against the model the graph will have. The finding
carries the engine's `code` and message (`term.unknown`, `query.syntax`, ...). A recipe with a
`<placeholder>` or an ellipsis where Cypher has none (`(n:<type>)`, `n.<attribute>`, `...`) is a
generic example of the command, not a broken recipe. Open parameters get a sample value and `--param`
numbers are read as `graph query` reads them, so a recipe that runs does not warn.

## What is not judged

Commands that read their input elsewhere (`--file`, `-`, a heredoc), shell comments, static skills and
installs. `graph-admin graph functions publish` alone does not lint the skill; a push that publishes
functions but neither releases nor publishes the skill lints the published template against the pushed
functions.
