# Actions for callers limited to a catalog

A server token whose grant names an action (a catalog grant, see `maintaining-graphs`,
`references/give_an_app_a_catalog_profile.md`) runs it with `graph do` although it could not write
the same ops by hand: the action is the one way it may change those types. Every deny rule, type
limit, scope, ceiling and obligation of the policy still refuses the whole action, and a type or
attribute the token does not see is never written. Inside that, the file decides what the token
does, so review it as a grant of write access.

## What the file decides

- **Template.** The ops are everything the token can do through the name. Keep each action
  narrow: fixed types, relations and attributes, and no parameter that stands for a wider
  change than the request needs. A parameter is a reference or a value, never a type or a
  term. Two requests that need different rights are two actions, so that a grant can name
  one and not the other.
- **Preconditions.** They are the only check of the current state those callers get, and a
  failed one quotes the actual values of the attributes its condition looks at, on the
  entities the caller named. Write conditions you would show to a caller that may read those
  entities. What must not reach a caller belongs in the policy (`reads`, markings), not in a
  condition: a condition on a marked attribute is warned about (`admin.marking_in_expression`)
  because its refusals are a covert channel.
- **Outputs and refusals.** The answer names the ids of what the action created or changed,
  and an integrity conflict names the visible data it conflicts with. They are not filtered:
  the caller needs them to correct a call. Judge an action by what that answer lets a caller
  with only the grant learn.
- **Summaries.** The `summary` of each precondition is what the caller reads when refused;
  say what to do instead, not what the data holds.

## Republishing

The grant names the action, not its text: whoever publishes the graph's actions changes what
its tokens write. `graph-admin graph actions publish` and `graph-admin graph push` warn
`catalog.granted_entry_changed` when a granted action's hash changes and
`catalog.granted_entry_removed` when it is gone (a `replaced_by` is named, but the grant
does not follow it), also on a dry run. Before such a change goes live:

1. Read the dry run's warnings; each names the grants and the tokens' roles behind them.
2. Review the new template as if it were granted now, compared with the old one.
3. Tell the owner of the policy; a replacement or a renamed action is granted only by a
   policy change (`graph-admin policy apply`), after you published it.
4. `graph-admin policy explain --graph <graph> --role <role>` shows the granted names with
   their current hash.
