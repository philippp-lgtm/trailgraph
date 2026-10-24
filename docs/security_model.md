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
rebuilds it (see [Storage](storage.md#filestore)). Reads are not audited, so a read under
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
  ontologies, and every change is audited under its name.
- A token bound to a project (`token create --project`) verifies the
  project, so that project's scope is a boundary for the token; without a
  binding the client asserts the project and the scope stays a guardrail.
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
  or the server's answers runs code on every client that updates. The
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

## Data in backups and audit events

Audit events carry ids, types, hashes and reason text, never attribute
values. Stored files and their origins are instance data; backups of the
data root may therefore contain personal data (see
[Files: backups](files.md#backups)).
