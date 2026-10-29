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
  subclasses is refused.
- Deny rules may name the agent type, agent, session, device or project;
  grants only the verified principal and a verified project. An explicit
  deny wins, the default is deny.
- A grant is for `graph` (`entrypoint: app`, the default), for the server
  API (`entrypoint: server`) or for both (`entrypoint: [app, server]`).
  Scopes and deny rules apply to every entrypoint. `principal: {id:
  token:<name>}` grants one token, `principal: {project: <project>}` every
  token bound to that project (only a bound token verifies a project, so
  such a grant needs `entrypoint: server`): a new token for the project
  needs no policy change.

```yaml
schema: graph_cli.policy/1
id: installation
grants:
  - {principal: {name: owner_name}, graphs: "*", level: write}
  - {entrypoint: server, graphs: "*", level: write}
  - {entrypoint: server, principal: {project: example_project}, graphs: [example_graph], level: write}
scopes:
  example_project: {example_graph: maintain, other_graph: read}
no_project: {"*": read}
deny:
  - {id: no_explore_writes, agent_type: [explore], action: ["write.*"]}
```

```yaml
schema: graph_cli.policy/1
id: graph
types:
  "files:File": read
obligations:
  - {action: ["write.*"], max_batch: 200}
```

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
