# Security model

Locally, every agent runs as the same OS user, and only the program
(`graph` or `graph-admin`) is a verified difference between callers. The project,
agent and session are asserted, so locally [access control](access_control.md)
is a guardrail plus detection, not prevention: it stops well-behaved agents
and mistakes (the wrong project, the wrong graph, a write where only reading
was meant), but an agent with a shell can edit the files. The project
selects a scope rather than only narrowing one: a pointer that names a
project gets that project's scope.

## Detection

Detection covers policy files edited, placed or deleted by hand and
decisions made under another policy (see
[Audit: policy verification](audit.md#policy-verification)), but not a
consistent rewrite of every stream (all tails, all hashes); that needs a
head kept outside the data root. Reads trust the derived files (revision
files, the checkpoint, the index) and check the transaction log only
after them, so an edited derived file changes what reads return until
`graph-admin audit verify` reports it and `graph-admin graph reindex`
rebuilds it (see [Storage](storage.md#filestore)). Writers trust the index
only after its SHA-256 digest of the transaction log matches and check
what they copy from it, so an edited log is found by the next writer; an
index edited consistently with its digest is found by `audit verify` and
replaced by the full replay a writer runs at least every 200
transactions. Reads are not audited, so a read under
another policy leaves no trace in the audit; the opt-in
[access log](audit.md#access-log) records reads, but it is not chained
and can be edited.

## Prevention

Prevention needs the data out of the agents' reach. With
[`graph-server`](server.md) owning the data root (in the container, or as
another OS user), callers hold only a bearer token: they cannot edit the
files, the policy or the audit, and the same policies decide. What it
prevents depends on what is verified:

- The token is verified: a grant or deny rule on `principal_id:
  token:<name>` holds for every request with it. An app token reaches no
  maintenance action; an admin token (scope `admin`, only with the
  [admin API](server.md#admin-api) turned on) reaches only maintenance, as
  one of the policy's maintainers. Treat an admin token like a shell in
  the container: whoever holds it can change policies, tokens and
  ontologies, and every change is audited under its name. An admin token
  bound to a project is narrower: it maintains only the graphs the
  project's scope says `maintain` for (release, publish, push, check,
  reindex) and sees no other graph; a hard-coded ceiling keeps it from
  every installation and destructive action and from the maintainer
  rules ([Project admins](access_control.md#project-admins)). Whoever
  holds it can still change those graphs' ontology, skill, named
  functions and named actions, and reads their data through check findings.
  The skill, the functions and the actions steer every agent that uses the
  graph, in every project:
  `maintain` on a graph other projects read lets one project's admin write
  instructions for their agents and choose which queries and which reviewed writes they run.
- A token bound to a project (`token create --project`) verifies the
  project, so that project's scope is a boundary for the token, and a
  grant to the project (`principal: {project: P}`) reaches it; without a
  binding the client asserts the project, the scope stays a guardrail and
  no project grant or `maintain` level applies.
- Roles ([Roles](access_control.md#roles)) are verified only through a
  server token: the server reads them from the token record, a client cannot
  assert them, and a local invocation has none, so a rule naming a role holds
  only with the server. Assigning roles is maintenance
  (`maint.token_update`, `token role add|remove`, `--role` of `token create`
  and `enroll create`), so whoever holds an admin token can widen what an app
  token may do within what the policy grants to roles; an app token cannot
  assign any, and a project admin is kept from it by its ceiling. A role
  change counts from the next request, as a revoke does, and every change is
  audited ([Audit](audit.md#token-verification)).
- Write allow lists ([Write allow lists](access_control.md#write-allow-lists))
  narrow what a subject may write to the attributes and relations its grant
  names. They hold like any rule: only through server tokens, where the
  caller cannot edit the policy or the data; locally they are a guardrail that
  stops well-behaved agents and mistakes, and an agent with a shell can edit the
  files. Decisions on attributes and relations are audited like decisions on
  types: the events of a write and of its refusal record them, with their
  resources, and `audit verify` checks their policy hash as for any decision. A
  `writes` grant cannot exceed what scopes and deny rules allow, because they
  narrow it and an explicit deny wins. Reading is not limited by it: whoever
  may write a type may still read what the read rules leave, which is all of the
  graph unless it has read limits (next item).
- Read limits ([Read limits](access_control.md#read-limits)) hide whole types
  from a subject: for it the graph reads as if their entities, relations and
  names did not exist ([What a caller can
  see](access_control.md#what-a-caller-can-see)). They hold like any rule: only
  through server tokens, where the caller cannot edit the policy or the data;
  locally they are a guardrail that stops well-behaved agents and mistakes,
  and an agent with a shell can read the files. They narrow inside a graph the
  caller may see; whether a graph is visible at all (`graph.not_found`) is
  unchanged. The maintainers, the `graph-admin` commands, project admin tokens
  and the [live view](live_view.md) are not limited: they read everything
  because they maintain the graph, so their findings, usage report and
  views show hidden data. What a limit still shows is listed under [Type read
  limits](#type-read-limits).
- Enrollment codes ([Enrollment](server.md#enrollment)) are bearer
  secrets too, but narrow ones: one use, at most 24 hours, always bound to
  a project, so a code never yields an unbound token (a maintainer's). Who
  holds a code gets that project's token, app or project admin, with the
  roles of an app code, as the maintainer chose; hand it over like a password, out of band, and revoke
  it (`enroll revoke`) when unsure. The holder picks only a label: the
  token is named `<project>.<label>` and never takes a name a policy rule
  grants something to. The exchange needs no token, answers the same for
  every code it refuses, takes no lock for a wrong code and never logs a
  code; it is audited under the enrollment's id. Revoking a maintainer's
  token leaves the codes it created valid (`enroll list`). `graph connect`
  checks the client's folder before it spends the code and keeps the token
  outside the folder, which may be in git, per server, so a token is never
  sent to another server.
- Agent, session and device are still asserted by the client: deny rules
  on them stop well-behaved clients only.
- The transport: the server speaks plain HTTP on `127.0.0.1`; beyond the
  machine it belongs behind TLS (the compose `tls` profile), and the
  client refuses plain HTTP to another host unless told otherwise. Tokens
  are stored as hashes and never logged or audited; a request without a
  valid token is logged but reaches nothing.
- Program updates: `graph self update` installs the wheel the server hands
  out after checking its hash against the server's manifest. Both come
  over the same connection, so the check catches a broken transfer, and
  TLS is what keeps them authentic: `self update` installs nothing over
  plain HTTP to another host. Whoever can replace the wheels in the image
  or the server's answers runs code on every client that updates (the
  wheels are not signed: trusting the server is trusting its code). The
  `graph` wheel is public (program code without data); the `graph-admin`
  wheel needs an admin token ([Self update](server.md#self-update)).

Whoever can run `graph-admin` where the data lives (`docker compose exec`),
or holds an admin token of a server with the admin API on, remains the
maintainer. Without a server, a coding agent's own
permissions narrow the normal path further, for example in a project's
Claude Code settings:

```json
{"permissions": {"deny": ["Bash(graph-admin:*)", "Edit(.graph_cli.toml)", "Write(.graph_cli.toml)"]}}
```

plus `Edit`/`Write` denies for the data root. Such settings are not
installed by `graph`.

## Type read limits

A limited caller is answered as if the hidden types did not exist
([What a caller can see](access_control.md#what-a-caller-can-see)). The
protection is complete for data: no entity, id, alias, label, value, edge,
count or term of a hidden type reaches it through a read command, a file
download, a function, an action, the generated skill or `whoami`. What is left
tells that something exists or that something happened, never what it is. These
are accepted and known:

- **Integrity refusals tell that something hidden exists.** Validation judges the
  whole graph, because judging the filtered state would let a caller write
  data that breaks the graph. A write can therefore be refused because of what
  the caller cannot see: a unique value, an alias or the exact bytes of a file
  held by a hidden entity, a retract blocked by a hidden referrer, a cycle
  through hidden entities, a cardinality that hidden edges have reached, a rule
  that counts a relation reaching a hidden type. The refusal keeps its code and
  severity and says "conflicts with data you cannot see"; it never names the
  entity, type, relation or value, and `file add` of bytes a hidden file holds is
  a concealed `unique_violation` on the content, never `file.already_present`
  with the holder. This is the covert channel of row-level security in
  databases (PostgreSQL documents the same for unique constraints). A caller
  can probe for the existence of a value by trying to write it; it learns
  nothing else.
- **A required relation with a hidden range blocks creates.** When a type must
  have an edge to a type the caller cannot see (a cardinality minimum on a
  relation whose range is hidden), every create of that type is refused for the
  caller, because it cannot give a target. Hide such types together, or give
  the caller a role that sees the range.
- **Activity shows, data does not.** The envelope's `seq` counts every
  transaction of the graph, hidden ones included. A revision whose only change
  was an edge to a hidden entity appears in `history` with an empty `changed`.
  The `reason` of a revision is the writer's text and shown as written, and so
  is the `action` name of a history item, even when the action is hidden from
  the caller. The `superseded_by` of an entity that a
  hidden entity superseded is empty. Timing differences are not hidden either.
- **Prose is not filtered.** The summary and description of an ontology term
  that stays visible, the text of a curated graph skill and the `reason` of
  a change are text the maintainers and writers wrote, shown as they wrote
  them. If that text names a hidden type, the caller reads the name. Write
  ontology prose and curated skill text so that it holds for every reader
  ([Skills](skills.md#blocks)); the generated blocks are rendered per reader
  and name nothing hidden.
- **What it does not cover.** Single attributes and rows of a visible type are
  visible to whoever sees the type; read limits stop at whole types. A type
  cannot be write-only: a caller writes only types it sees.

## Data in backups and audit events

Audit events carry ids, types, hashes and reason text, never attribute
values. Stored files and their origins are instance data; backups of the
data root may therefore contain personal data (see
[Files: backups](files.md#backups)).
