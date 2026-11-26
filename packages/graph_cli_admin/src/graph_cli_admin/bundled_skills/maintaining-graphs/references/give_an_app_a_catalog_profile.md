# Giving an app a catalog profile

A catalog profile limits a server token to some published functions, actions and views of a
graph and nothing else: it lists the graph, reads its model, runs those entries (`run`, `do`,
`view --name`, `call`) and sees no other entry; `get`, `find`, `search`, `query`, `neighbors`,
history and the card view are refused. Use it for an app or agent harness that should do a few
reviewed things, not explore. Only when the user asks, only a maintainer.

## Choose the entries

Each granted entry is a security boundary, so settle the list with the user first:

- A function or view reads, and an action writes, whatever it does for the token, although the
  token could not `get`, `find` or write the same by hand. Read each entry with `graph describe
  function <name>`, `graph describe action <name>` or `graph describe view <name>` (or its file in the
  workspace) and ask what it returns, what an
  action writes, and what its refusals and failed preconditions show (an action's preconditions
  quote the values they check; see `authoring-graph-actions`, `references/catalog_callers.md`).
- Prefer few, narrow entries. Granting a name never grants the entries it runs inside (a view's
  `rows` functions, the types an action touches need not be granted themselves).
- A name is granted only while the graph publishes it: check `graph-admin graph functions show
  <graph>`, `graph-admin graph actions show <graph>` and `graph-admin graph views show <graph>`.

## The grant

```yaml
grants:
  - entrypoint: server
    principal: {role: <role>}
    graphs: [<graph>]
    level: catalog
    catalog:
      functions: [<function>]
      actions: [<action>]
      views: [<view>]
    reads: [<prefix>:<Type>]
    markings: [<prefix>:<marking>]
```

- `level: catalog` needs `catalog` (at least one name), `entrypoint: server`, graphs named one by
  one and `reads` (the only types the token sees, exact types: a `<prefix>:*` is refused, so a type a later release
  adds never widens it).
  `markings` as for any grant; `writes` is not allowed (an action writes through its own grant).
- A reader that should also write through some actions keeps `level: read` and adds `catalog:
  {actions: [...]}`.
- Rights add up. A token that also holds another grant of the graph (a read role, a grant without
  a principal) is not limited by the catalog, and its `reads` and `markings` widen. `policy check`
  warns `policy.catalog_widened` for the certain cases; read it.
- Deny rules, type limits, scopes and obligations still hold inside every entry and win over the
  grant.

## Install and check

1. `graph-admin policy check <file>`, then `graph-admin policy diff <file>` and `graph-admin
   policy apply <file> --reason "<why>"`. `policy.entry_unknown` names a granted entry the graph
   has not published (it grants nothing until it does).
2. A token bound to the app's project, so the project is verified and its scope applies:
   `graph-admin token create <name> --project <project> --role <role>`, or `graph-admin enroll
   create --project <project> --role <role>`. `admin.token_not_granted` means the policy gives
   the token nothing on any graph (a scope that takes the graph away, a role no grant names);
   `admin.token_not_catalog_only` means another role of the token reads the graph too, so the catalog
   limits nothing: give the app one role.
3. `graph-admin policy explain --graph <graph> --project <project> --role <role>` shows `level:
   catalog` and `catalog`: each granted name with `published` and the entry's current `hash`
   (`published: false` grants nothing yet), `catalog_only` and, when it is false, the
   `reader_rules` that make the token a reader. `--function <name>`, `--view <name>` and
   `--entry-action <name>` explain the decision on one entry and name the rule that allowed it.
4. A server that holds catalog tokens should run with `[audit] access_log = true` in its `config.toml`:
   allowed reads are not in the audit trail, and the access log is what shows a token that runs one
   entry many times (`graph-admin usage report`); it records commands, entries and sessions, no values.
5. What the app will see: `graph whoami` with the token (`level: catalog`, the names it sees) and
   `graph describe tools --json`, which lists exactly its tools.

## Republishing a granted entry

The grant names an entry, not its content: whoever publishes the graph's catalog (maintainers,
and project admins with `maintain` on the graph) changes what the token reads or writes under
that name. Publishing warns, never refuses, and the dry run shows the warnings:

| Warning | Meaning | What to do |
| --- | --- | --- |
| `catalog.granted_entry_changed` | the entry stays and its hash changes (sources, model it resolves against, status), or a granted name gets an entry it did not have (granted ahead, or removed before; `hash_before` is null); `details` give the grants and both hashes; for a view whose row function changed or went, `details.functions` lists the function and its hashes | review the new definition as a new grant (what it reads or writes now), tell the policy's owner, then publish; `policy explain` shows the new hash |
| `catalog.granted_entry_removed` | the grant now gives nothing; `details.replaced_by` names the successor the old entry declared, if any | a replacement or a renamed entry is never granted by the old name: publish it, then name it in the policy and apply; do this before removing the old entry if the app must not lose a step |

The publish commands (`graph-admin graph functions publish`, `graph-admin graph actions publish`,
`graph-admin graph views publish`) and `graph-admin graph push` warn in local and remote mode alike.
No warning means no granted entry was touched, none of its row functions either. A project admin
sees the grant ids and the principal of its own project's grants only. An `ontology release` (also
in `graph-admin graph push`, also with `--dry-run`) that compiles the catalogs again for renamed
terms republishes the same sources against the new model and warns `catalog.granted_entry_changed`
for each granted entry whose hash changes; read the dry run before releasing.
