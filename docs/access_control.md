# Access control

Two policy layers decide what `graph` may do; maintenance is decided by the
entrypoint (the app never gets `maint.*`). What this
protects against, and what it does not, is in the
[security model](security_model.md).

## Identity

Every invocation has one actor. The OS user
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
  read-only or hidden, obligations (`require_reason`, `max_batch`) and deny
  rules. `max_batch` bounds the operations of one change set, that is one
  transaction, not the total: a larger batch is split into several
  documents, each committed on its own.
  Type limits and obligations name write actions, and so do deny rules on
  attributes and relations. A deny rule on a type may also name `read.get`,
  which is whether the type is visible ([Read limits](#read-limits)). A type
  limit matches the exact type, so it must name concrete types (or
  `prefix:*`); an abstract type or one with subclasses is refused. A deny rule
  may also name single attributes or relations of a type
  ([Write allow lists](#write-allow-lists)).
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
  ([Write allow lists](#write-allow-lists)), and **`reads`**, an allow list of
  the types its subject sees ([Read limits](#read-limits)).

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
  The same code warns when a grant with `reads` and a `create` of a type leaves
  that type's required relation (a cardinality minimum) with a range none of the
  grant's own `reads` cover: a caller with only that grant sees no target, so
  every create is refused; another grant may still supply one, as rights add up
  across roles (`details` add `relation` and `range`).

`ontology release` gives the same two warnings when the new version drops or
renames a type or term that an installed policy names, so that a rename does
not silently take writes away. It also warns `admin.policy_unknown_type` when a
type named in a `writes` grant becomes abstract or gains subclasses: the exact
rule then misses their entities, so grant the subclasses too.

Allow lists fail closed on a rename, deny rules fail open: a rule that hides or
protects a type or term by its old name applies to nothing once a release
renames it (`renamed_from`) or moves its entities to another class (a migration's
`retype`). `ontology release` therefore refuses such a release
(`admin.policy_renamed_term`, `details.rules` names each rule, the old and the new term)
when a deny rule (a `deny` entry of a graph policy, `types: {T: none}` or `read`, a glob
such as `prefix:*` that the new name leaves) matches the old type or term and no deny rule
matches the new one for the same actions. Apply a graph policy that names both the old and
the new term first (a policy may name terms the pinned release does not have yet), release,
then remove the old names. `ontology diff --graph` and `graph-admin graph check --ontology` show the same
before the release (`admin.policy_renamed_term_affected`, `references`). An unfinished
migration is completed even if the policy fails this check.

### Read limits

A grant reads a whole graph: every entity of every type. To let a subject see
only some types, give its grant `reads`, or hide a type in the graph policy. For
a caller the graph then reads as if the other types, and their entities, did not
exist ([What a caller can see](#what-a-caller-can-see)).

```yaml
schema: graph_cli.policy/1
id: installation
roles:
  reporting: {description: "Reporting apps: findings and sources of example_graph"}
  auditor: {description: "Auditors: every type of example_graph"}
grants:
  - entrypoint: server
    principal: {role: reporting}
    graphs: [example_graph]
    level: read
    reads: [ex:Finding, ex:Source]
  - {entrypoint: server, principal: {role: auditor}, graphs: [example_graph], level: read}
scopes:
  example_project: {example_graph: read}
no_project: {"*": none}
```

A token with the role `reporting` reads `ex:Finding` and `ex:Source` entities
and nothing else; a token with `auditor` reads everything; a token with both
sees everything, because rights add up.

- **`reads` on a grant.** A list of concrete type CURIEs, or `prefix:*` for
  every type of a module. The grant still reads the graph as a whole (every
  read command is decided as before) and sees the listed types. It needs
  `level: read` (`level: write` reads every type, so the combination is
  `policy.invalid`), graphs named one by one (`"*"` is refused) and a non-empty
  list. A type that is abstract or has subclasses is `policy.invalid` once the
  graph's model shows it, as for `writes`, because the rule matches the exact
  type: such a type cannot be listed, name its concrete subtypes. A type the
  graph does not have only warns (`admin.policy_unknown_type`). A grant with
  `reads` and `writes` must list every `writes` type in `reads`: a grant cannot
  write a type it hides.
- **Rights add up.** The types a subject sees are those of every grant that
  names it. A subject with an unlimited grant (or `level: write`) sees every
  type, so a role with `reads` narrows only subjects that hold no broader
  grant. Scopes and deny rules narrow the result, and an explicit deny wins.
- **New types stay hidden.** An ontology release that adds a type changes
  nothing for a subject with `reads`: the new type is not in its list. A
  subject without a limit sees the new type at once. <!-- claim: access_control.new_types_stay_hidden -->
- **Hide a type for everyone.** In the graph policy, `types: {ex:Person: none}`
  hides the type from every caller of `graph` and of the server API, for reads
  and writes. Maintenance is not hit: the `graph-admin` commands, project admin
  tokens and the [live view](live_view.md) read everything, because they
  maintain the graph.
- **Hide a type for some subjects.** A deny rule of the graph policy with
  `type` may name `read.get` (alone or with write actions) and the usual
  subject keys:

  ```yaml
  schema: graph_cli.policy/1
  id: graph
  deny:
    - {id: hide_people, role: [reporting], action: ["read.get"], type: ["ex:Person"]}
  ```

  Concrete types only, as for type limits. A deny rule that names a type with
  `read.get` hides it from the subjects it matches, whatever their grants say.
- **Visibility is one decision per type.** A type is visible to a caller when
  `read.get` on `graph:<id>/type:<curie>` is allowed to it: the decision that
  already decides the content of a stored file. No rule may name another read
  action on a type (`policy.invalid`); the rules on attributes and relations
  stay write-only.
- **A deny of `read.get` on a whole graph.** A rule that denies `read.get` on
  the graph (no `type`) matches every type of it as well. It withholds the
  `get` command and, once some rule decides the visibility of a type in the
  graph, also hides every type from the subjects it names. Check such rules
  before adding a read limit to the graph.
- **Compiled rules.** A grant with `reads` keeps its read rule as
  `grant.<n>` but on the graph itself instead of the glob `graph:<id>*`, which
  would cover every type, and gains `grant.<n>.reads`: `read.get` on each type
  resource. `none` compiles to the deny rule `type.<curie>` with `read.get` and
  `write.*` for the entrypoints `app` and `server`. A graph without any such
  rule is not read-limited: nothing is filtered, and its answers, events and
  hashes are what they were. The compiled document never depends on a model.
- **Warnings.** `policy check`, `diff`, `apply` and `explain --file` warn
  `admin.policy_unknown_type` for a type in `reads`, `none` or a `read.get`
  deny rule that the graph does not have, and `ontology release` warns when the
  new version drops a named type, when a type in `reads` becomes abstract or
  gains subclasses (the subclass stays hidden: grant it too) and when a type
  hidden by a deny rule gains subclasses (the deny names the exact type, so the
  subclass is visible: deny it too).

### Markings

Read limits hide whole types. A **marking** hides single attributes inside a
type that stays visible. The ontology author declares markings and puts them on
attributes (`markings: [ex:personal]`, for example personal data; see the
[`ontology-best-practices`](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/ontology-best-practices/SKILL.md)
skill and its format reference). An attribute with several markings needs all
of them (they are conjunctive). The policy grants markings to subjects: a
caller that holds every marking of an attribute sees it, and for every other
caller the attribute reads as if the type did not declare it ([What a caller
can see](#what-a-caller-can-see)). The entity itself stays visible.

```yaml
schema: graph_cli.policy/1
id: installation
roles:
  hr: {description: "HR: personal data of example_graph"}
  reporting: {description: "Reporting apps: example_graph without personal data"}
grants:
  - entrypoint: server
    principal: {role: hr}
    graphs: [example_graph]
    level: write
    markings: [ex:personal]
  - {entrypoint: server, principal: {role: reporting}, graphs: [example_graph], level: read}
scopes:
  example_project: {example_graph: write}
no_project: {"*": none}
```

A token with `hr` reads and writes the attributes marked `ex:personal`; a token
with `reporting` sees the same entities without them; a token with both roles
sees everything, because rights add up.

- **`markings` on a grant.** A list of marking CURIEs (`prefix:name`, lower
  case), on a grant of any level, with graphs named one by one (`"*"` is
  `policy.invalid`). Rights add up across grants and roles, as for every other
  right. A marking the graph's pinned ontology does not declare only warns
  (`admin.policy_unknown_marking`). There is no graph-layer form and no deny
  rule on a marking: a marking already hides by default, so a new marking stays
  hidden until a grant names it, and a marking moved to another module (a
  different CURIE) is hidden again until the grant names the new one.
- **One decision per marking.** A caller holds a marking when `read.get` on
  `marking:<graph id>/<curie>` is allowed to it. No existing grant, scope,
  `no_project` entry or deny rule covers that resource (it is not below
  `graph:<id>`), so a marked attribute is hidden from every grant until a grant
  names the marking. The maintainers hold every marking. A project admin token
  never does, because its ceiling refuses every resource but a graph
  ([Project admins](#project-admins)); the maintenance commands it may run
  still read raw data, as they do for [read limits](#read-limits). A rule on a
  marking resource may name `read.get` only (`policy.invalid` otherwise).
- **Compiled rules.** A grant with `markings` keeps its other rules and gains
  `grant.<n>.markings`: `read.get` on each marking resource of each named graph.
  A policy without `markings` compiles to the same document and hash as before.
  A graph whose pinned ontology uses no marking is not marking-limited: nothing
  is filtered, and its answers, events and hashes are what they were.
- **A marked artifact must match its pin.** Reads go on with a warning
  (`ontology.pin_mismatch_read`) when the pinned compiled ontology no longer
  matches its hash, but not when that file shows markings: only the pin vouches
  for which attributes are marked, so every read of the graph, a holder's
  included, fails with `ontology.pin_mismatch` until the file is restored, and
  `whoami` shows `markings: []` for it (nothing for a maintainer, who holds
  every marking).
- **The built-in policy grants no marking.** Without an installation policy no
  caller of `graph` reads or writes a marked attribute, and a type with a
  required marked attribute without a default cannot be created locally
  (`graph-admin` has no data write path). A local installation that uses
  markings installs a small installation policy with a grant for the local
  app that names them; locally it stays a guardrail, like every policy:

  ```yaml
  schema: graph_cli.policy/1
  id: installation
  grants:
    - {entrypoint: app, graphs: [example_graph], level: write, markings: [ex:personal]}
  no_project: {"*": write}
  ```

- **Warnings.** `policy check`, `diff`, `apply` and `explain --file` warn
  `admin.policy_unknown_marking` for a named marking the graph does not declare
  and `admin.policy_unusable_write` (a message of its own) when a grant's
  `writes` name a marked attribute, or cover one with `true`, without the same
  grant holding its markings (another grant of the caller may add them), and
  when a grant creates a type whose required marked attribute without default it
  does not hold, which can then never be created. An installation policy that
  names no grant for a marking some graph's pinned model uses warns
  `admin.marking_ungranted` there as well, so a policy change that drops such a
  grant is not silent until the next release. `ontology release`,
  `graph-admin graph push` and `ontology diff --graph` warn `admin.marking_ungranted` (a
  marking the new version uses that no installed grant names: only maintainer
  surfaces show those attributes), `admin.marking_blocks_create`,
  `admin.marking_in_expression` (a rule, lifecycle guard or action precondition
  names a marked attribute; `graph-admin graph actions publish` warns it for the
  preconditions it publishes: its refusals are a covert channel, see the
  [security model](security_model.md#field-visibility)), `admin.marking_removed`
  (a marking was removed from an attribute, and the values, history included,
  become readable for every caller that sees the type) and
  `ontology.marking_name_returned` (a once-marked name returns unmarked while its
  old values map away by rename). A release that would expose old values of a
  marked name through a reused name is refused (`ontology.marking_name_reused`);
  the ontology skill explains when a name counts as reused.

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
and pin), `graph-admin graph skill publish`, `graph-admin graph functions publish`, `graph-admin graph actions publish`,
`graph-admin graph views publish` and `graph-admin graph push` (`graph_configure`),
`graph-admin graph check` and `files verify` (`audit_verify`) and `graph-admin graph reindex` (`files extract` is
decided as `maint.reindex` too, but the admin API serves it to maintainers only);
reads that show the graph's configuration (`graph-admin graph show`, `graph-admin graph pull`,
`graph-admin graph skill render`, `graph-admin graph functions show`, `graph-admin graph actions show`,
`graph-admin graph views show`, `ontology diff --graph`) need `graph_configure` too. Check and release findings quote the
graph's data, so `maintain`
includes `read.get` on the graph, and `graph-admin graph check`, `ontology release`
and a `graph-admin graph push` that releases also authorize it for a project admin: a
deny rule on reads (by project, for example) holds there too. Everything
else stays with the `maintainers`: creating, archiving
and restoring graphs, migrations, purges and repairs, policies, tokens
and the installation audit. A release that carries a migration (a
`migration.yaml`, or renames) is also authorized `maint.migrate`, before
the lock and for a dry run too; a project admin has no such right, so
that release is `access.ceiling` and a project admin releases only
versions that need no data migration. `maintain` names graphs one by one (`"*":
maintain` is refused), so a new graph is never maintained implicitly.

A project admin is held to this by a hard-coded ceiling, like the app:
any other action, or any resource but a graph, is `access.ceiling`, and
of the policy's allow rules only those naming its project count, so the
`maintainers` (and the built-in policy, which lets every maintenance
caller maintain) never reach it. Over the admin API it sees only the
graphs it maintains: `graph-admin graph list` lists those, any other graph is
`graph.not_found` as if it did not exist (the refusal is audited),
warnings about other graphs' manifests are left out, and every other
command is refused. `graph-admin policy explain --graph G
--token NAME` shows what a token's requests get, `maintains` included.

## Decisions

One decision point decides every request
([Architecture](architecture.md#access-control-modules)). It is pure and
answers for one (action, resource) pair: the app and server entrypoints
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
`require_reason` and `max_batch`. For a caller with [read limits](#read-limits)
`writes` lists only the visible types and the terms that exist for it
([Inspecting what a caller sees](#inspecting-what-a-caller-sees)).

`graph-admin policy explain` answers the same for roles, tokens and projects:
without `--action` the answer gains `writes`, and `--attribute A` or
`--relation R` with `--type` and `--action` explains one decision on a term and
reports `type_decision`, the decision on the type, next to it
([`graph-admin`](cli_graph_admin.md)). The [live view](live_view.md) lists the
rules `grant.<n>.<verb>` with their resources and shows `writes` next to the
level of a caller.

## What a caller can see

A caller whose reads are limited to some types ([Read limits](#read-limits))
sees the graph as if the other types did not exist: no entity of them, no edge
into them, no name, count or term. Anything it asks about a hidden entity, type
or relation is answered as about one that does not exist, with the same code,
message and details, so the answer cannot tell "hidden" from "not there". A
caller that sees every type, and every graph without read rules, is answered
exactly as before. The graph itself stays as visible as the grant makes it
(`graph.not_found` is unchanged); a limit narrows inside a visible graph.

- **Entities.** `get`, `find`, `lookup`, `search`, `neighbors`, `query` and
  `run` return entities of visible types only, and totals count what is left. A
  hidden entity is `entity.not_found` in every form of reference: id, alias and
  the end of an id (a suffix that matches only hidden ids is unknown, and the
  candidates an ambiguous suffix lists are visible ones). A filter with a hidden
  id (`find --rel <relation>=<id>`) is an empty page, as with an id that is not
  there.
- **Edges.** An entity is shown without its edges into hidden entities, and
  `get --full` shows a content hash computed over what is shown, so nothing
  marks an entity as having hidden edges. Edges from hidden entities are not
  incoming edges, a relation filter or an `exists` test counts an edge to a
  hidden entity as absent, and `neighbors` never walks onto or through a hidden
  entity, at any depth.
- **Cypher.** A label or relationship type of a hidden type is an unknown
  term (`term.unknown`, with suggestions from what the caller sees). Matches,
  counts, paths and variable-length patterns run over the visible entities, and
  a returned node has no hidden edges.
- **Time.** Visibility is decided under the policy installed now and the
  ontology pinned now, also for `--as-of`, `get --rev`, `history` and `changes`
  (like every read, [History](#history)). An entity is visible in a past state when its type
  there is visible, and the history of a hidden entity is
  `entity.not_found`. Each past revision of a visible entity is shown when its own type is
  visible too: a migration's `retype` can move an entity out of a class the caller cannot
  see into one it sees, and the revisions it had before the move are absent for that caller.
  `get --rev` of such a revision is `entity.revision_not_found`, exactly as for a revision
  that was never stored, and `history` and `changes` skip it. The other revisions keep the
  numbers they were stored under, so the numbers can have a gap. The first revision shown is
  a creation for the caller (`changed` is `null`, every edge is a target of the feed item),
  and every later one is compared with the latest revision shown before it. An entity moved
  into a hidden class is hidden as a whole. A major release shows the revisions before it
  under the current names, and a `retype` shows the old class as the new one
  ([Earlier revisions under current names](cli_graph.md#earlier-revisions-under-current-names)).
  The caller's view judges such a revision by that mapped class and also by the class it was
  stored under, followed through renames only: when the pinned model still has that class and
  the caller cannot see it, the revision is absent, and so are the values in it, though the
  entity reads as the new class today. The same holds for `--as-of`: an entity that was of
  the hidden class then is absent in that state. A class the pinned model no longer has judges
  nothing, so those revisions read as the class the entity was moved to.
- **The model.** `describe types|type|fields|relations|lifecycle|modules|
  module|schema|search` show a model without the hidden types and what only they
  carry: a relation whose domain or range has no visible type, attributes
  declared only on hidden types, an abstract type with no visible subtype, enum
  values, lifecycles and modules nothing visible uses. Where a relation or an
  abstract type names several types, only the visible ones are listed. A hidden
  type, relation or field is unknown, and the suggestions of `term.unknown`,
  the fit hints of `lookup --for` and the relations and transitions of
  `get --affordances` come from what the caller sees. The descriptive text of a term that
  stays (its summary and description) is the maintainer's prose and shown as
  written ([Security model](security_model.md#type-read-limits)).
- **Files.** A hidden stored file is `entity.not_found` for `file path`,
  `file check` and the download route, before any message that would name its
  type. A caller that sees none of the file types is answered as for a graph
  without the files module.
- **Writes need visibility.** A caller writes only types it sees, and a
  reference to an entity it cannot see is `entity.not_found`. An edge to a
  hidden entity given by its id is a missing target (`target_missing`), not a
  range violation that would name its type. A create of a hidden type is
  answered as a create of a type the graph does not have: `access.denied` with
  `default.deny` for a caller whose write rights come from `writes` grants only,
  else `write.invalid` with `data.structural.unknown_type` (its `expected` list
  holds visible types). The refusal is audited with the real decisions,
  including the deny of `read.get` on the type ([Audit](audit.md)). An update
  keeps the entity's edges to hidden entities, an `unlink` never matches such an
  edge, and `graph do` with `unlink ... all: true` removes visible edges only,
  because the caller cannot touch what it cannot see. The entities a write
  reports are the ones of visible types.
- **Integrity holds for the whole graph.** Validation judges the graph as it
  is, so a write can fail because of what the caller cannot see: a unique value,
  an alias or a file's bytes held by a hidden entity, a retract blocked by a
  hidden referrer, a cardinality reached by hidden edges. Such a finding keeps
  its code and severity and says only "conflicts with data you cannot see": no
  id, alias, type, name or value of the hidden entity. `possible_duplicate`
  warnings about hidden entities are dropped, and the values of a rule finding
  are dropped when it counts a relation that reaches a hidden type. What this
  still shows is in the [security model](security_model.md#type-read-limits).
- **Skills.** The generated graph skill is rendered per reader
  ([Skills](skills.md#per-reader)).

### Functions and actions of a limited reader

A named function or action is shown to a limited caller only when it fits what
the caller sees; to everyone else it does not exist (it is *hidden*). What a
caller sees in full but lacks a right for is *not allowed*, not hidden
([next section](#functions-actions-and-views-you-may-not-use)).

- **Functions** are hidden when a type or relation they touch, the class of a
  `ref` parameter, an enum parameter or the module that holds them is hidden (a module
  whose terms are all hidden; a main module that holds no terms of its own is not), or
  when they compile only against the full model (an unlabelled node that reads
  an attribute only a hidden type has). A hidden function is not listed or
  described, not featured in the skill, not counted in `describe modules`, not
  suggested by `did_you_mean`, and `run` of it is `function.not_found`. Every
  other function runs over what the caller sees, so its answer holds visible
  entities only.
- **Actions** need, for each of their needs, a visible type: a need is met only
  through a type the caller sees and may write ([Actions](#actions)). An action
  is also hidden when a parameter class, enum or relation it names is hidden, so
  `describe action` and the skill never name a hidden term. `do` and
  `describe action` of a hidden action are `action.not_found`.

### Functions, actions and views you may not use

Every published function, action and view is, for one caller, in one of three states.

- **Hidden.** It names a type, relation, attribute, enum, module or function the caller cannot see
  (read limits, markings). It does not exist for that caller: not listed, not counted, not found by
  `describe search`, not suggested, and its name, authored text or hints are never in an answer or
  an error. Hidden depends on what the caller sees and never on the rights it holds; a caller who
  loses a right does not see more or fewer hidden entries.
- **Allowed.** The caller sees everything the entry names and holds the rights it needs.
- **Not allowed.** The caller sees everything the entry names but lacks a right. Only such an entry
  is named as unusable, so nothing hidden can leak through it. The rights are `read.function` on the
  graph (a function, and a view with a `rows` section), `read.get` on the graph (every view),
  `read.find` (a set view) and, for an action,
  per unmet need the first denied write decision: the write action on the type, or on the attribute
  or relation where a write allow list or a term rule decides one. An obligation (`require_reason`,
  `max_batch`) never makes an entry not allowed: an action that needs `--reason` is allowed. An
  action whose unmet need has no right to name, because every type that could meet it is outside
  what the caller sees, is hidden.

What each command does with a not-allowed entry:

| Command | Answer |
| --- | --- |
| `describe functions\|actions\|views` | lists the allowed entries; `not_allowed` is the number of the others (module and deprecation filters apply); `--include-not-allowed` lists them after the allowed ones, each with `allowed: false` and `missing` |
| `describe modules`, `describe module M` | counts of the allowed entries per kind (`functions`, `actions`, `views`) and, per kind, the number not allowed in `not_allowed`; `describe module` lists them with `--include-not-allowed` |
| `describe search` | finds them like allowed ones, marked `allowed: false` with `missing`, ranked after the allowed hits of the same tier; they match on name, summary and what they are for (`use_when`, parameter names) only, not on example texts, section titles or touched terms |
| `describe function\|action\|view NAME` | what the entry is for and how it is called, with `allowed: false` and `missing`: name, module, summary, `use_when`, signature, status and the parameters (name, type, class or enum, `many`, summary, `required`), `replaced_by` only when the caller may use the replacement. Not the query, the operations, conditions, sections, example texts or values, parameter defaults, touched terms, needed rights, hash or call command: a definition can carry literals of the maintainer's, and the right that is missing is the one that protects them |
| `run`, `do`, `view --name` | `access.denied`, recorded as a denial (the first denying decision, so the audit event names the right), message naming the missing right and the hint to ask the maintainer for a role |

`missing` is a list of `{action, resource}`; the resource is graph-relative (`graph`,
`type:<curie>`, `type:<curie>/attr:<name>`, `type:<curie>/rel:<curie>`) and always names something
the caller sees. It is what the caller is told, not the complete list: for an action it holds the first denied
write of each unmet need, so a right granted may reveal the next one missing. Listing, counting, describing and
searching record no decision, like `graph graphs`.
The graph skill still lists the allowed featured entries only ([Skills](skills.md#per-reader)).

### Attributes a caller lacks a marking for

A caller that lacks a marking of an attribute ([Markings](#markings)) is
answered as if the type did not declare the attribute: the same code, message
and details as for a name the type does not have. Every other caller, and
every graph whose ontology uses no marking, is answered as before.

- **Entities and queries.** `get`, `find`, `lookup`, `search`, `neighbors`,
  `query` and `run` show an entity without the attribute, and `get --full`
  shows a content hash computed over what is shown. A filter, a sort or a
  field list that names it is `term.unknown` (with suggestions from what the
  caller sees); `exists`, `ne` and negation treat it as absent, also on
  sibling types that declare an attribute of the same name, and a Cypher
  property of it is absent (`null`) on a node without a label. Totals count what
  is left. A hidden label is no label: the entity shows no label, `lookup` and
  `search` do not match it and `lookup` facts leave it out.
- **Time.** `history`, `changes`, `get --rev`, `--as-of` and the migration items of
  `history` show past revisions mapped to current names and stripped the same
  way: `changed` never names the attribute, and an attribute whose name no
  longer maps (dropped, or an old release that cannot be read) is hidden for a
  caller without markings. A rename hint never prints a hidden name.
- **The model.** `describe`, the schema search and the generated skill omit the
  attribute and what only it uses (an enum or datatype, a rule or lifecycle
  guard that reads it); a reader that holds the markings sees them as a note on
  the field. A function or action that reads or sets the attribute, or takes an
  enum only hidden attributes use, is hidden like one that touches a hidden type
  ([Functions and actions of a limited reader](#functions-and-actions-of-a-limited-reader)).
  The claim of an installed skill ([Skills](skills.md#per-reader)) goes stale when
  a marking grant or a release changes what the reader sees.
- **Writes.** A write that names the attribute (`create`, `set`, `set_text`,
  `unset`, `add`, `add_text`, `remove`, `remove_text`, a transition's `attr`, an
  action's operations)
  is answered exactly like a write of an attribute the type lacks, a guess of
  the current value and a dry run included: `write.invalid` with
  `data.structural.unknown_attribute` when the caller's rights cover attribute
  names, `access.denied` when its `writes` list does not. The decision is made
  from the names the write carries, before the write is normalized, so a guess
  never gets a "nothing changed" answer. Refusals that name a hidden attribute
  are audited as `denied` events that also record the denied `read.get` on the
  marking resource ([Audit](audit.md)). An update keeps the values the caller
  cannot see, and validation still judges the whole graph, so a finding that
  stems from a hidden value keeps its code and severity and says "conflicts with
  data you cannot see" (what this still shows is in the [security
  model](security_model.md#field-visibility)).

### Inspecting what a caller sees

- **`graph whoami`** gains, per graph, `reads`: the sorted concrete types the
  caller sees, when the graph has read limits and the caller does not see every
  type. `types` lists only limits of visible types, and `writes` only visible
  types and terms. A caller that sees every type gets no `reads` key, so its
  answer is unchanged. A hidden type is never named to the caller that cannot see it.
- **`graph-admin policy explain`** answers the same for roles, tokens and
  projects: without `--action` the answer gains `reads`, and `--type T --action
  read.get` explains whether `T` is visible. The compiled rules `grant.<n>.reads`
  show as grants, `type.<curie>` of a `none` as a type limit.
- **`markings` in `graph whoami` and `explain`.** `whoami` lists, per graph,
  the sorted markings the caller holds, when the graph's ontology uses markings
  and the caller does not hold every one of them. `markings: []` means that
  something is marked and the caller holds nothing; a marking the caller lacks
  is never named. The key is absent when the caller holds every marking in use or
  the graph uses none, so those answers are unchanged. When the graph's model
  cannot be read, a caller that does not hold every marking gets `markings: []`
  (fail closed). `writes` lists only attributes the caller sees.
  `graph-admin policy explain` answers the same for roles, tokens and projects
  (`markings` next to `reads` and `writes`), and `--action read.get --marking
  <curie>` explains one marking: the compiled rule is `grant.<n>.markings`, a
  grant kind. A project admin token is refused with `access.ceiling` and lists
  `markings: []`; a token with no project that is a maintainer holds every
  marking.
- The [live view](live_view.md) shows `reads` and `markings` next to the level
  of a caller.

## History

Earlier revisions are part of reading: the `read` level includes
`read.history`, so whoever reads a graph reads the history of all its
entities (`graph history`, `graph changes`, `graph get --rev`, every read with
`--as-of`).
Read access is per graph, not per entity. A deny rule on `read.history`
withholds the history and leaves the current state readable; `whoami`
then reports a lower level and names the action in `withheld`. Like every read, history is decided
under the policy installed now, not the one in effect when a revision was
written. `history` shows of the writer only whether it was the caller's
own session; who wrote a revision is shown by `graph-admin` (the
[live view](live_view.md) and the audit). Retracting keeps earlier values
readable; hiding history is not a way to remove data. A caller with
[read limits](#read-limits) reads the history of the entities it sees, over
their revisions without edges into hidden entities: a revision whose only change
was such an edge shows an empty `changed` ([What a caller can
see](#what-a-caller-can-see)). The same holds for a revision whose only change was
an attribute the caller lacks a marking for.
`graph changes` lists the same history merged over the whole graph: the
changes of the entities the caller sees, nothing of the others ([Changes since a
position](cli_graph.md#changes-since-a-position)).

## Views

`graph view` ([An entity at a glance](cli_graph.md#an-entity-at-a-glance), [Authored views and set
views](cli_graph.md#authored-views-and-set-views)) is a `read.get` command: whoever may read an
entity may read its card or an entity view of it, and a deny rule on `read.traverse` does not reach
it. The card shows the counts of an entity's relations and its first neighbours with their labels,
which `graph neighbors` (`read.traverse`) would show too, so a rule that denies `read.traverse` but
allows `read.get` still leaves that much of the neighbourhood readable. Read limits and markings
shape every view like every read: what the caller may not see is not counted or named.

Published views add three rules.

- **A view needs `read.get`, a set view `read.find` as well**, because it lists entities as `find` does;
  a view with a `rows` section needs `read.function`, and the function must be one the caller sees.
- **A view is visible exactly when everything it names is**: its classes, attributes, relations and
  enums, and the functions it runs. Otherwise it does not exist for the caller: `view.not_found`
  (a hidden view is never `access.denied`), absent from `describe views`, `describe search` and the affordances, and
  the default view of a class falls back to the next one up the class chain or to the card. The whole
  view is hidden, not a section, because its summary, purpose and section titles are authored text
  that may name what the caller may not see. A view the caller sees in full but that needs a
  `read.get`, `read.find` or `read.function` it lacks is *not allowed*
  ([Functions, actions and views you may not use](#functions-actions-and-views-you-may-not-use)):
  counted in `not_allowed`, found and described with `allowed: false` and `missing`, and `view --name`
  of it is `access.denied`, recorded like `graph find` without `read.find` (a listing never records a
  denial, as for functions). A default view the caller may not use is skipped like a hidden one.
- **Rows, neighbours and counts come from the caller's own snapshot and model.** An entity the caller
  does not see is not a row, an edge to it is not a neighbour, a hidden source is not counted, and an
  attribute the caller may not see is absent from `fields: all`. A cursor is tied to what the caller
  sees: after a policy change it is `view.cursor_invalid`. A view that no longer fits the ontology
  (an interrupted release) is `view.stale` for a caller who sees everything and not found for a
  caller with read limits, because the check's problems may name terms the caller must not see. A
  deprecation warning never names a replacement the caller cannot see.

## Queries

`graph query` (Cypher) has its own action, `read.query`, in the `read`
level. It shows nothing `find` and `neighbors` could not show, but in one
call; a deny rule on `read.query` withholds that expressive path from, for
example, an agent type, while `find`, `neighbors` and `lookup` keep working
(`whoami` names the action in `withheld`). Levels expand when a policy is
compiled, so a policy installed by a program that did not know `read.query`
grants it only after `policy apply` runs again with the same source.

## Functions

Named functions of a graph ([Functions](cli_graph.md#functions)) have their
own action, `read.function`, in the `read` level.

- **Running.** `graph run` is decided by `read.function` on the graph, before
  any storage is read. A denial is audited like that of any other read (the
  `args_digest` covers the function name and the parameters; the values
  themselves are never written).
- **Listing.** `describe functions`, `describe module`, `describe modules`
  and `describe search` show functions as usable only to callers who may
  `read.function` on the graph. For everyone else they are *not allowed*
  ([Functions, actions and views you may not use](#functions-actions-and-views-you-may-not-use)):
  counted in `not_allowed`, listed with `--include-not-allowed`, found by
  search and shown by `describe function` marked `allowed: false` with
  `missing: read.function`. A name that is not a function is
  `function.not_found`. Listing is no recorded decision: it writes no denial
  event, like `graph graphs`; `run` is.
- **Graph skill.** The generated graph skill lists the featured functions only
  to a reader who may `read.function` (their names and summaries are
  schema-level text like type names, but a function the reader may not run is
  not worth its context). A reader without the right gets one line saying that
  functions are not for this token, and no name, unless its read limits hide every
  function (then the right changes nothing: it learns of none). With [read limits](#read-limits)
  it lists the functions that reader sees and none that names a hidden term.
  The claim of an installed copy records a digest of what that reader's render
  shows of the catalogs, so granting or taking `read.function` makes the copy
  stale ([Skills](skills.md#per-reader)).
- **Read limits.** A caller who sees only some types does not see a function
  that names a hidden term, and the others run over what the caller sees
  ([Functions and actions of a limited reader](#functions-and-actions-of-a-limited-reader)).
- **Separate from queries.** A deny rule on `read.function` withholds the
  functions and leaves ad-hoc queries readable (`whoami` names the action
  in `withheld`); a deny on `read.query` leaves the functions. This allows
  curated functions without ad-hoc Cypher for an agent type or a machine
  actor.
- **Grants are per graph**, not per function. Levels expand when a policy is
  compiled, so a policy installed by a program that did not know `read.function`
  grants it only after `policy apply` runs again with the same source.
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
  shows the needs as published. For a caller with [read limits](#read-limits)
  a need is also met only through a type the caller sees.
- **Listing.** `describe actions`, `describe action`, `describe module`,
  `describe modules` and `describe search` show an action as one the caller may
  run only when, for each need, at least one of its concrete types allows it, with the attributes and
  relations the action writes there where the policy has rules on terms of that
  type (any-of per need). So an action no argument could run is not listed, and a
  listed action can still be refused for an entity of a type the caller may not
  write; the refusal names the resource. The check
  records no decision (no denial event), like the listing of functions, and
  obligations do not hide an action: one that needs `--reason` is listed.
  An action that fails the check is *not allowed*: counted in `not_allowed`,
  listed with `--include-not-allowed`, found by search and shown by
  `describe action` with `allowed: false` and the first denied write per unmet
  need as `missing`, and `do` of it is `access.denied`, recorded (nothing is
  written). An action that names a hidden type, relation or enum is *hidden*
  from a caller with read limits: it is `action.not_found` with `did_you_mean`
  over the actions the caller sees
  ([Functions and actions of a limited reader](#functions-and-actions-of-a-limited-reader)).
- **Graph skill.** Like the functions, the generated graph skill lists
  only the featured actions the installing reader may run
  ([Skills](skills.md#per-reader)).
- **Publishing** the catalog is `maint.graph_configure`, as for the functions.
- **No writes only through actions.** The policy language cannot let an agent
  type run reviewed actions while it may not use the primitives: that would
  let an action grant what its op would not.

## Reconcile documents

A document that hands over the desired state of a source
([Reconciling a source](cli_graph.md#reconciling-a-source)) has no action of
its own: nothing in the taxonomy and nothing to apply again. It can do
nothing its caller could not do with ops, and it is decided like the ops it
expands to.

- **Declared needs, before the lock.** Each scope declares what it may do,
  and the write is authorized on that before anything is planned, so a token
  that lacks a right fails on its first run, not on the day the right is
  needed: `write.create` and `write.update` on the scope's type with the
  attributes `fields` plus the key attribute (a create also with the
  relations `edges`, an inline edge being decided as `write.create`);
  `write.link` and `write.unlink` with the relations `edges`, for a scope that
  sets any; for a complete scope `write.retract` (or, with a lifecycle
  retire, `write.transition` with the lifecycle attribute), and
  `write.transition` for a scope that revives. A need on an abstract type
  stands for its concrete subtypes and is met when one of them is allowed
  (any-of, as for [actions](#actions)). A deny is `access.denied`, audited
  like any write denial in the installation stream, and it names the
  source. A dry run is denied unaudited.
- **Concrete ops, under the lock.** The plan's ops are authorized again as
  any change set is: one decision per (`write.*` op action, type) and, for a
  type with rules on terms, per attribute and relation written, with the
  `require_reason` and `max_batch` obligations on the expanded ops (a
  document without a reason gets the default reason
  `reconcile <name> <version> run <run>`). The decisions of the event record
  the declared needs and the concrete ops.
- **Read limits.** A scope type the caller cannot see is `term.unknown`, as
  for a type the model lacks; the plan reads only what the caller sees, so a
  key held by an entity of a hidden subtype is not found and the create
  fails on the unique check with the finding concealed
  ([Type read limits](security_model.md#type-read-limits)).
- **Markings.** A key attribute or field the caller lacks the marking for is
  refused by name, before anything is planned, as an unknown attribute
  (`write.invalid`, or `access.denied` for a type whose policy has rules on
  terms), whether or not an item names it; an attribute outside `fields` is
  never read or touched, so a pipeline without the marking leaves marked
  values as they are.
- **The source grants nothing.** `source` is recorded with the actor and
  decides no right ([Security model](security_model.md#documents-from-a-source)).
  A role for a pipeline lists `writes` on its types (`create` and `update`
  with the key attribute and its fields, `link` and `unlink` with its
  relations, `retract` or `transition` for complete scopes) and the `reads` and
  `markings` its fields need.

## Metadata records

The metadata records of stored files ([Files: Metadata](files.md#metadata)) are written as
the caller who triggers the extraction, so they are decided like any write; what is special
is who may trigger it and who may write them at all.

- **Nobody writes a record by hand.** Every op on a record or part is refused
  (`data.semantic.managed_class`) unless it is an extraction run
  ([Security model](security_model.md#derived-file-metadata)); this holds whatever the
  policy allows, so `write.*` grants on the record classes matter only for the extraction.
- **`graph file add`** extracts the file it stored with the caller's own rights. A role that
  adds files needs `write.create`, `write.update`, `write.link`, `write.unlink` and
  `write.retract` on the record classes of the kinds the graph imports (`files:FormatMetadata`
  and for example `files_pdf:PdfMetadata`, with `files:metadata_of`); a role without them stores
  the file with the warning `file.extraction_incomplete`, which names the missing action and
  resource. The add finds that out with a dry run of each record's document, which the write
  service denies unaudited: no `denied` event is written per add. A maintainer's `files extract`
  writes the records of such files later. A caller who does not see a file type neither extracts nor learns
  of those files: a reference to one is `entity.not_found`.
- **`graph-admin files extract`** (a run over the whole graph or over named files) needs the
  maintain permission on the graph, decided as `maint.reindex` (the maintain-level action
  nearest to refreshing derived data); the `graph` program has none, and a denial is audited
  in the installation stream. The records it writes are the maintainer's writes, so a policy
  that denies the maintainer `write.create` on a record class stops the run at that denial.

## Authoring and installing policies

Authored policies live in the maintainer workspace. `graph-admin policy
check` compiles them: levels become actions,
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
