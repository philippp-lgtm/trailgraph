# Access control

Two policy layers decide what `graph` may do; maintenance stays the
entrypoint question it was (the app never gets `maint.*`). What this
protects against, and what it does not, is in the
[security model](security_model.md).

## Identity

Every invocation has one actor (`graph_cli.catalog.identity`). The OS user
is the verified principal. Agent and session are asserted, from the
`GRAPH_CLI_AGENT_ID`, `GRAPH_CLI_AGENT_TYPE`, `GRAPH_CLI_SESSION_ID` and
`GRAPH_CLI_PARENT_SESSION_ID` variables first, then from the coding agent
harness. The agent is the exception: a subagent inherits the variables of
the session that started it, so the harness's own agent (id and type, e.g.
a read-only exploring subagent) comes first. Malformed values are dropped
with a warning and never block. The project comes from the
[pointer file](configuration.md#project-pointer). `graph whoami` shows the
actor and what the policy allows it.

Requests to the server API have the entrypoint `server`. Their verified
principal is the bearer token (`token:<name>`); agent, session, device and
project are asserted by the client in the request. A token bound to a
project verifies the project: a request naming another project is refused
(`access.project_mismatch`). A token fetched with an enrollment code
(`graph connect`, [Enrollment](server.md#enrollment)) is always bound to
the code's project. The exchange itself is no policy decision: the
maintainer authorized it by creating the code (`maint.token_create`), and
its event names the enrollment as principal (`enrollment:<id>`, source
`enrollment`) and decision rule.

An app token may hold **roles** (see [Roles](#roles)). A request with it
carries them in the actor, as `actor.roles` (sorted, left out when the token
has none), and they are verified: they come from the server's token record, a
client cannot assert them (a request whose `actor` object names `roles` is
refused as `usage.invalid_input`). A local invocation and an admin token have
none. `graph whoami` over the server shows them in `actor`, and so do the
audit events and the transaction records of the request.

Requests to the admin API (`graph-admin` commands over HTTP, an admin
token) have the entrypoint `maint` and the principal `token:<name>`: the
installation policy's `maintainers` decide as for a local maintainer, so
an installation that lists maintainers names the token there
(`{id: "token:<name>"}`).

An admin token bound to a project is a **project admin** instead: it
maintains the graphs its project's scope says `maintain` for and reaches
nothing else (see [Project admins](#project-admins)). Locally the
project comes from the pointer and is only asserted, so a local
`graph-admin` never acts as a project admin.

## Policy layers

- The **installation policy** grants, per principal, a level on graphs and
  narrows it per project: `scopes` names graphs per project (the pointer's
  `project`), the required `no_project` scope covers invocations without a
  project and with a project it does not know (warning
  `access.project_unknown`). Levels are `none` (the graph is invisible),
  `read` and `write` (read plus writes and extension actions). A project
  scope may also say `maintain` for a named graph: `write` for the
  project's callers, plus maintenance by the project's admin tokens.
- A **graph policy** narrows inside one graph for everyone: types that are
  read-only, obligations (`require_reason`, `max_batch`) and deny rules.
  `max_batch` bounds the operations of one change set, that is one
  transaction, not the total: a larger batch is split into several
  documents, each committed on its own.
  Types are checked for writes only, so type limits, deny rules on types and
  obligations name write actions. A type limit matches the exact type, so it
  must name concrete types (or `prefix:*`); an abstract type or one with
  subclasses is refused. A deny rule may also name single attributes or
  relations of a type ([Write allow lists](#write-allow-lists)).
- Deny rules may name the agent type, agent, session, device, project or
  role; grants only the verified principal, a verified project and a role.
  An explicit deny wins, the default is deny.
- A grant is for `graph` (`entrypoint: app`, the default), for the server
  API (`entrypoint: server`) or for both (`entrypoint: [app, server]`).
  Scopes and deny rules apply to every entrypoint. `principal: {id:
  token:<name>}` grants one token, `principal: {project: <project>}` every
  token bound to that project (only a bound token verifies a project, so
  such a grant needs `entrypoint: server`): a new token for the project
  needs no policy change.
- A **role** is a subject like a token or a project, and its rights are the
  rules that name it. The installation policy declares the roles (`roles:`,
  each with a required `description`; a name is lowercase letters, digits and
  `_`, up to 64 characters). A grant names one in `principal: {role: <name>}`,
  alone or with `name`, `id` or `project` (all must match); only a server
  token carries roles, so the grant needs `entrypoint: server`. A deny rule of
  either layer names one or several in `role: [<name>, ...]`. A rule naming
  roles matches when the token holds at least one of them. Roles grant, scopes
  and deny rules narrow, and an explicit deny wins, as for every subject. A
  role the installation policy names in a grant or deny rule without declaring
  it is `policy.invalid`. A graph policy is applied independently, so a role
  that the installed installation policy does not declare is only the warning
  `policy.role_unknown` (on `policy check` and `policy apply`). A policy
  without roles compiles to the same document and hash as before roles existed.
- A grant with `level: read` may carry **`writes`**, an allow list of exactly
  what its subject may write, down to attributes and relations
  ([Write allow lists](#write-allow-lists)).

```yaml
schema: graph_cli.policy/1
id: installation
roles:
  reader: {description: "Reporting apps: read every graph"}
  importer: {description: "Importers: write into example_graph"}
grants:
  - {principal: {name: owner_name}, graphs: "*", level: write}
  - {entrypoint: server, principal: {role: reader}, graphs: "*", level: read}
  - {entrypoint: server, principal: {role: importer}, graphs: [example_graph], level: write}
  - {entrypoint: server, principal: {project: example_project}, graphs: [example_graph], level: write}
scopes:
  example_project: {example_graph: maintain, other_graph: read}
no_project: {"*": read}
deny:
  - {id: no_explore_writes, agent_type: [explore], action: ["write.*"]}
  - {id: importer_no_retract, role: [importer], action: ["write.retract", "write.supersede"]}
```

```yaml
schema: graph_cli.policy/1
id: graph
types:
  "files:File": read
obligations:
  - {action: ["write.*"], max_batch: 200}
deny:
  - {id: importer_no_files, role: [importer], action: ["write.*"], type: ["files:File"]}
```

### Write allow lists

`level: write` lets a subject write everything the scopes and deny rules
leave. A grant that should write less is `level: read` plus a `writes` block:
the grant reads the graph and may write what the list names, nothing else.

```yaml
schema: graph_cli.policy/1
id: installation
roles:
  importer: {description: "Importers: findings and their sources in example_graph"}
grants:
  - entrypoint: server
    principal: {role: importer}
    graphs: [example_graph]
    level: read
    writes:
      ex:Finding:
        create: [title, summary, status]
        update: [status, notes]
        link: [ex:about]
        transition: true
      ex:Source:
        create: true
        link: true
scopes:
  example_project: {example_graph: write}
no_project: {"*": read}
```

An importer may create an `ex:Finding` giving `title`, `summary` and `status`
(and an inline `ex:about` edge, see below), update its `status` and `notes`,
link `ex:about` but not unlink it, move it through its lifecycle, and create
and link `ex:Source`. Everything else is refused: another attribute, another
relation, `unlink`, `retract`, any other type. The scope still has to allow
writing: a token of no project gets `no_project`, which here denies every
write, so the grant would write nothing.

- **Grammar.** `writes` maps concrete type CURIEs to verbs (no `prefix:*`; a
  type that is abstract or has subclasses is `policy.invalid` once the graph's
  model shows it, as for type limits, because the rule matches the exact
  type). `create` and `update` take `true` (every attribute) or a non-empty
  list of attribute names, `link` and `unlink` `true` or a list of relation
  CURIEs, `transition` `true` or a list of lifecycle attribute names;
  `supersede`, `retract` and `alias` are `true`. An absent or `false` verb is
  not granted, and a type entry grants at least one verb.
- **Where it applies.** `writes` needs `level: read` (`level: write` already
  allows every write, so the combination is `policy.invalid`) and graphs named
  one by one (`"*"` is refused): the terms belong to an ontology, and they are
  checked against each named graph. Extension actions (`ext.*`) are not part of
  a `writes` grant; they write as a whole and need `level: write`.
- **Compiled rules.** The grant keeps its read rule `grant.<n>` and gains one
  allow rule per granted verb, `grant.<n>.<verb>`, on the type resource and,
  for a list, on the resources of the listed attributes or relations
  ([Decisions](#decisions-on-attributes-and-relations)). `link` adds a second
  rule, `grant.<n>.edge`, that allows `write.create` on the listed relation
  resources only. The compiled document never depends on a model, so an
  ontology release does not change a policy's hash, and a policy without
  `writes` compiles as before.
- **Rights add up.** The rights of a subject are the grants that name it, so a
  token with several roles may do what any of them allows (a token with the
  roles `importer` and `tagger` may write what either grant lists), and a
  broader grant of another role or of the token itself adds to a list rather
  than being cut down by it. Scopes, deny rules, type limits and obligations of
  both layers still narrow the result, and an explicit deny wins. A `writes`
  grant therefore never exceeds what they allow.
- **Inline edges.** An edge given with a `create` is decided as `write.create`
  on the relation (so a broad deny on `write.link` or `write.create` still stops
  it) and needs two rights: the create right on the created type, and the link
  right on the relation, from the same grant or from another one of the
  caller. A relation is thus writable by one right, whichever op adds the edge.
  `unlink` is its own right; removing an edge by its id is decided on the
  relation of that edge.

**Deny rules on attributes and relations.** A rule of the graph layer with
`type` may add `attribute: [names]` or `relation: [curies]` (not both, concrete
types only). It then denies on those terms of every listed type instead of on
the type, for the write actions that can decide them (`write.create`,
`write.update`, `write.transition` for attributes, `write.create`,
`write.link`, `write.unlink` for relations, or `write.*`). A `relation` rule on
`write.link` also denies `write.create`, which adds the inline edges of a
create. It is the natural partner of an unlimited `level: write` grant: "the
importer may update anything but `salary`" is one rule.

```yaml
schema: graph_cli.policy/1
id: graph
deny:
  - {id: no_salary, role: [importer], action: ["write.update"], type: ["ex:Person"], attribute: [salary]}
```

**Warnings.** `policy check`, `diff`, `apply` and `explain --file` load the
pinned model of every graph a `writes` grant or term deny rule names and warn;
these are warnings, not errors, because a later release may add what is
missing, and the policy stays closed meanwhile:

- `admin.policy_unknown_type`: a type the graph does not have (`details`
  `graph`, `type`).
- `admin.policy_unknown_term`: an attribute or relation the type does not
  have, or a `transition` attribute that is no lifecycle attribute (`details`
  `graph`, `type`, `term`, `kind`). Nobody can write a term that does not
  exist.
- `admin.policy_unusable_write`: a write no call can satisfy, because `update`
  names a lifecycle attribute (a transition moves it) or a `create` list leaves
  out an attribute the create must give: a required one without a default, or a
  lifecycle attribute with several initial states (`details` `graph`, `type`,
  `verb`, `attributes`). For a type that stores files the warning also names the
  attributes `graph file add` sets itself ([Decisions](#decisions-on-attributes-and-relations)).

`ontology release` gives the same two warnings when the new version drops or
renames a type or term that an installed policy names, so that a rename does
not silently take writes away. It also warns `admin.policy_unknown_type` when a
type named in a `writes` grant becomes abstract or gains subclasses: the exact
rule then misses their entities, so grant the subclasses too.

## Roles

Roles make two apps of one project differ, and one set of rights apply across
projects, without a policy edit for every new app. The policy declares the
roles and grants and denies by them ([Policy layers](#policy-layers)); the
graph admin gives them to tokens:

- `graph-admin token create NAME --role R` (repeatable) creates an app token
  with roles, and `enroll create --project P --role R` a code whose token
  gets them ([Enrollment](server.md#enrollment)). `token role add NAME ROLE...`
  and `token role remove NAME ROLE...` change a token later. A token holds at
  most 16 roles.
- A role is assigned only when the installed installation policy declares it,
  else `admin.role_unknown` (the answer lists the declared roles and the
  closest names): declare it, `policy apply`, then assign. Removing a role
  needs no declaration, so one the policy dropped can still be taken off.
- Roles are for app tokens. An admin token has none (`usage.invalid_argument`).
- A change that changes nothing, such as adding a role the token holds,
  writes no event and answers `changed: false`.
- Every change is audited as `maint.token_update` and counts from the next
  request ([Security model](security_model.md#prevention)).
- `policy apply` of an installation policy that no longer declares a role
  that a token holds, or that an installed graph policy names, warns
  `admin.role_orphaned`: the membership stays, and no grant can name the
  role any more, while deny rules of graph policies that name it still apply
  to the tokens that hold it, until it is declared again or taken off.
- `token list` shows each token's `roles`, `token list --role R` only the
  tokens that hold `R`, and `enroll list` the roles of each code.
- `graph-admin policy explain --graph G --role R` (repeatable, with an
  optional `--project P`, which is then verified as for a bound token) shows
  what a server token with these roles gets and by which rule; it excludes
  `--token`, which already includes the token's own roles. A role that the
  installation policy used for the explanation does not declare warns
  `policy.role_unknown` and is explained all the same, so a policy file
  (`--file`) can be tried before it is applied. The answer names the `roles`.
- The [live view](live_view.md) shows a token's roles in the access list of a
  graph and next to the principal of a revision.

## Project admins

`scopes.<project>.<graph>: maintain` lets an admin token bound to the
project (`graph-admin token create NAME --scope admin --project P`) run
the graph-level maintenance of that graph: `ontology release` (release
and pin), `graph skill publish`, `graph functions publish` and `graph push`
(`graph_configure`),
`graph check` and `files verify` (`audit_verify`) and `graph reindex`;
reads that show the graph's configuration (`graph show`, `graph pull`,
`graph skill render`, `graph functions show`, `ontology diff --graph`) need `graph_configure`
too. Check and release findings quote the graph's data, so `maintain`
includes `read.get` on the graph, and `graph check`, `ontology release`
and a `graph push` that releases also authorize it for a project admin: a
deny rule on reads (by project, for example) holds there too. Everything
else stays with the `maintainers`: creating, archiving
and restoring graphs, migrations, purges and repairs, policies, tokens
and the installation audit. `maintain` names graphs one by one (`"*":
maintain` is refused), so a new graph is never maintained implicitly.

A project admin is held to this by a hard-coded ceiling, like the app:
any other action, or any resource but a graph, is `access.ceiling`, and
of the policy's allow rules only those naming its project count, so the
`maintainers` (and the built-in policy, which lets every maintenance
caller maintain) never reach it. Over the admin API it sees only the
graphs it maintains: `graph list` lists those, any other graph is
`graph.not_found` as if it did not exist (the refusal is audited),
warnings about other graphs' manifests are left out, and every other
command is refused. `graph-admin policy explain --graph G
--token NAME` shows what a token's requests get, `maintains` included.

## Decisions

`graph_cli.access_control.authorize()` is the one decision point. It is pure
and answers for one (action, resource) pair: the app and server entrypoints
never get a `maint.*` action and a project admin only the graph-level
maintenance of its `maintain` graphs, then the first matching deny rule of any policy layer
wins, then the first matching allow rule, else deny, then the obligations
of every layer (`require_reason`, `max_batch`).

A graph the caller may not see is `graph.not_found`, exactly like one that
does not exist, and the refusal is audited; on a visible graph a refusal is
`access.denied` with the rule. A broken installed policy stops every
`graph` call (`policy.invalid`); `graph-admin` warns and decides without
it, so the policy can be replaced.

### Decisions on attributes and relations

Below a type, a write is also decided on the attributes and relations it
touches. Their resources are `graph:<id>/type:<curie>/attr:<name>` and
`graph:<id>/type:<curie>/rel:<curie>`, on the type of the entity whose revision
changes (for a `create` the created type, for a `link` the type that owns the
edge). Rules on them name write actions only, like rules on types, and the
globs `graph:<id>*`, `graph:<id>/type:*` and `graph:<id>/type:<prefix>:*` cover
the terms of the types they cover, so scopes decide a term as they decide its
type. Which terms an operation is decided on, in addition to its decision on the
type, which always stays:

| Operation | Decided on |
| --- | --- |
| `create` | each attribute given (`attrs`, `attrs_text`) as `write.create`; each inline edge as `write.create` on its relation |
| `update` | each attribute in `set`, `set_text`, `unset`, `add`, `add_text`, `remove`, `remove_text` as `write.update` |
| `link` | the relation of the edge as `write.link` |
| `unlink` | the relation of the edge as `write.unlink`; an edge given by its id is read from the entity's revision (an `@local` subject, one without a revision or an unknown edge has none to decide, and the write is refused as before) |
| `transition` | the lifecycle attribute (the op's `attr`, else the type's lifecycle) as `write.transition` |
| `supersede`, `retract`, `set_alias` | none, the type decision decides |

- **Only what the writer gives.** Attributes the caller did not choose are not
  decided: defaults, the single initial state of a lifecycle, values the
  normalizer derives. Attributes a command sets for the caller count as given:
  `graph file add` sets `title`, `content`, `media_type` and `file_name` (and
  `origin` when a root matches on that machine, `source_url` and `retrieved_at`
  when given), so the `create` list of a file type has to name them.
- **Recorded only where a rule can tell.** The decisions on terms are made and
  recorded for the types some rule of the graph's policies names terms of (a
  resource below the exact type, in either layer, allow or deny). For every
  other type, only the type is decided and recorded, so a graph whose policies
  have no `writes` and no term deny rules decides, records and audits exactly
  as before.
- **Exact type rules do not match terms.** A type limit (`types: {T: read}`)
  or a deny rule with `type` matches the type resource only. That never changes
  an outcome, because the decision on the type is still required, and the
  places that report what a caller may write (`whoami`, `policy explain`) join
  the decision on a term with the decision on its type.
- **Refusals name the term.** A refused write is `access.denied`; `details`
  name the action, resource and rule of the first denial, sorted by action and
  then resource, so a type denial is named before a term denial. The audit
  event records every decision, with the resources of the terms
  ([Audit](audit.md)). For a limited caller (a `writes` grant, or a rule that
  names terms of the type), an attribute the type does not have is refused as
  `access.denied` on `attr:<name>` rather than as an unknown attribute, because
  authorization comes before validation; `graph describe fields <type>` and
  `graph whoami` show what exists and what is allowed.

### What a caller may write

`graph whoami` gains, per graph, a `writes` object when the caller's level is
below `write` and a write allow list applies to it: per type, each verb it may
use, as `true` when every term of the type is allowed, else the sorted list of
the allowed attributes (for `create`, `update` and `transition`) or relations
(`link`, `unlink`), which is empty when a deny rule leaves none. A type with no
verb left is omitted. `create` lists attributes only: an inline edge follows the
link right. Each term counts only when its own decision and the decision on its
type allow it. Callers with level `write`, or without a list, get no `writes`
key, so their answers stay as they were; a term deny on such a caller is not
shown by `whoami` (`policy explain` shows it, and the refusal names the term).
`obligations` also covers the allowed types, so a limited writer sees its
`require_reason` and `max_batch`.

`graph-admin policy explain` answers the same for roles, tokens and projects:
without `--action` the answer gains `writes`, and `--attribute A` or
`--relation R` with `--type` and `--action` explains one decision on a term and
reports `type_decision`, the decision on the type, next to it
([`graph-admin`](cli_graph_admin.md)). The [live view](live_view.md) lists the
rules `grant.<n>.<verb>` with their resources and shows `writes` next to the
level of a caller.

## History

Earlier revisions are part of reading: the `read` level includes
`read.history`, so whoever reads a graph reads the history of all its
entities (`graph history`, `graph get --rev`, every read with `--as-of`).
Read access is per graph, not per entity. A deny rule on `read.history`
withholds the history and leaves the current state readable; `whoami`
then reports a lower level and names the action in `withheld`. Like every read, history is decided
under the policy installed now, not the one in effect when a revision was
written. `history` shows of the writer only whether it was the caller's
own session; who wrote a revision is shown by `graph-admin` (the
[live view](live_view.md) and the audit). Retracting keeps earlier values
readable; hiding history is not a way to remove data.

## Queries

`graph query` (Cypher) has its own action, `read.query`, in the `read`
level. It shows nothing `find` and `neighbors` could not show, but in one
call; a deny rule on `read.query` withholds that expressive path from, for
example, an agent type, while `find`, `neighbors` and `lookup` keep working
(`whoami` names the action in `withheld`). Levels expand when a policy is
compiled, so a policy installed before `read.query` existed grants it only
after `policy apply` runs again with the same source.

## Functions

Named functions of a graph ([Functions](cli_graph.md#functions)) have their
own action, `read.function`, in the `read` level.

- **Running.** `graph run` is decided by `read.function` on the graph, before
  any storage is read. A denial is audited like that of any other read (the
  `args_digest` covers the function name and the parameters; the values
  themselves are never written).
- **Listing.** `describe functions`, `describe function`, `describe module`,
  `describe modules` and `describe search` show functions only to callers
  who may `read.function` on the graph. For everyone else the graph reads
  as if it had none, and a function is `function.not_found`. Listing is no
  recorded decision: it writes no denial event, like `graph graphs`.
- **Graph skill.** The generated graph skill is the same for every reader of
  the graph (it is read with `read.describe`) and lists the featured
  functions: their names and summaries are schema-level text like type
  names. Running one still needs `read.function`.
- **Separate from queries.** A deny rule on `read.function` withholds the
  functions and leaves ad-hoc queries readable (`whoami` names the action
  in `withheld`); a deny on `read.query` leaves the functions. This allows
  curated functions without ad-hoc Cypher for an agent type or a machine
  actor.
- **Grants are per graph**, not per function. Levels expand when a policy is
  compiled, so a policy installed before `read.function` existed grants it
  only after `policy apply` runs again with the same source.
- **Publishing** the catalog is `maint.graph_configure`, as for the graph
  skill (see [Project admins](#project-admins)).

## Actions

Named write actions of a graph ([Actions](cli_graph.md#actions)) have no
action of their own: no new entry in the taxonomy and nothing to apply again
after an update.

- **Running.** `graph do` expands the action to ops and authorizes each one
  as `apply` does: one decision per (`write.*` op action, type), with the
  `require_reason` and `max_batch` obligations of the policy (and, where the
  policy has rules on the attributes or relations of a type, one more per
  attribute or relation written, [Decisions](#decisions-on-attributes-and-relations)).
  An action therefore never writes what the caller could not write by hand. A
  deny on the type or on a term of one op refuses the whole action with
  `access.denied`, audited like any write denial, and nothing is written; a
  replay or the details of a failed precondition do not reach a caller without
  the rights. When the expansion has no op (an `all: true` unlink with nothing
  to remove), the action is authorized on what it declares to need, by the rule
  of the listing.
- **Needs.** What an action needs is derived when its catalog is loaded
  against the graph's pinned ontology, so nothing is republished when the
  ontology or a policy changes. A need on an abstract class or a superclass
  stands for its concrete subtypes; a dynamic unlink of incoming edges on a
  relation without a domain needs `write.unlink` on every concrete type. Below
  the type, an op needs the attributes and relations its template names (a
  transition without `attr` the lifecycle attribute of the type, from the
  model), and an optional parameter's entry counts as written. `describe action`
  shows the needs as published.
- **Listing.** `describe actions`, `describe action`, `describe module`,
  `describe modules` and `describe search` show an action only when, for each
  need, at least one of its concrete types allows it, with the attributes and
  relations the action writes there where the policy has rules on terms of that
  type (any-of per need). So an action no argument could run is hidden, and a
  listed action can still be refused for an entity of a type the caller may not
  write; the refusal names the resource. The check
  records no decision (no denial event), like the listing of functions, and
  obligations do not hide an action: one that needs `--reason` is listed.
  For everyone else the graph reads as if it had no such action, and `do`
  or `describe action` of it is `action.not_found` with `did_you_mean` over
  the visible actions only.
- **Graph skill.** Unlike the functions, the generated graph skill lists
  only the featured actions the installing reader may run
  ([Skills](skills.md#blocks)).
- **Publishing** the catalog is `maint.graph_configure`, as for the functions.
- **Not available: writes only through actions.** A policy that lets an agent
  type run reviewed actions while it may not use the primitives would let an
  action grant what its op would not; that is not built, and the policy
  language cannot express it.

## Authoring and installing policies

Authored policies live in the maintainer workspace. `graph-admin policy
check` compiles them (`access_control.compiler`): levels become actions,
graph names become ids, and every problem is reported at once
(`policy.invalid`). `policy apply` installs the compiled document; without
an installed installation policy the built-in policy applies (every graph
readable and writable). `policy show`, `policy diff` and `policy explain`
inspect the installed and authored policies (see
[`graph-admin`](cli_graph_admin.md)).

## Policy integrity

Every decision records the policy hash, and `graph-admin audit verify`
reports a policy file edited, placed or deleted by hand and a write made
under another policy than the installed one. A writer reads the policies
again under its lock and decides again if one changed, so a concurrent
apply is no finding. How the policy history is recorded and checked:
[Audit: policy verification](audit.md#policy-verification).
