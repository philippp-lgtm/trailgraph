# Security model

Every agent runs as the same OS user, and only the program (`graph` or
`graph-admin`) is a verified difference between callers. The project,
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

Prevention needs a separate OS user for the data (a local service) or a
server; both would reuse the same policies. A coding agent's own
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
