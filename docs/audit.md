# Audit

`graph_cli.audit` builds and verifies `graph_cli.audit/1` events. It is
pure: storage appends and chains the events, audit never touches files.
Each graph has its own stream; installation-wide events go to the
installation stream.

| Module | Content |
| --- | --- |
| `audit.events` | builders for write intents, refusals, maintenance events, graph genesis and anchors; each routes its event to the graph or the installation stream |
| `audit.schema` | the event shape; builders refuse their own output if it misses it |
| `audit.verify` | `verify_stream` over raw lines, `cross_check` (tx log against write intents), `check_maintenance` (release and pin intents against their effects, pin events against the manifest), `check_policy` and `check_policy_hashes` (installed policies and the policy behind every graph-stream decision), `check_anchors`, and `verify_graph` / `verify_installation` composing them |
| `audit.finding_codes` | `integrity.audit.*` |

## Verification

`graph-admin audit verify` runs the verifier. It reads raw lines
(`AuditSink.read_lines`) and reports damage as findings instead of stopping
at it. Each damaged line gives at most one chain finding, so one deleted or
edited line does not flood the report. A write intent without a committed
transaction is the benign trace of an interrupted write (info); a
transaction without its intent is an error. Events carry ids, types, hashes,
sizes and reason text, never attribute values.

A write intent records how much the write stored: `change.changeset_size`
is the byte length of the canonical changeset (the form `changeset_hash`
covers), and every file the write stored is listed as `{hash, size}`.
Verification compares the changeset size with the transaction log, like
the ids and the hash; intents written before the size was recorded carry
none and are matched without it. Reads are not audited; the size of
what a read returns is in the [access log](#access-log), when it is on.

## Access log

`[audit] access_log = true` in `config.toml` (off by default) makes
`graph` append one line per command to
`$XDG_STATE_HOME/graph_cli/logs/access.jsonl` (format
`graph_cli.access/1`, canonical JSON, file mode 0600). Every command that
opened the installation is recorded, reads and writes alike, also when it
fails:

| Key | Content |
| --- | --- |
| `ts` | when the command finished |
| `command` | the command, e.g. `get`, `find`, `create` |
| `actor` | the actor, as in audit events |
| `graph` | `{id, name, seq}` of the graph state the command used, or `null` |
| `result` | `{status: ok}` or `{status: error, code}` |
| `output` | `{format: json\|text, bytes}`: the bytes the caller received on stdout |
| `timing_us` | `{total, phases}`: how long the command took, in microseconds, and where the time went |

`phases` names only the phases the command went through. Each phase counts
its own time (a phase inside another is taken out of the outer one), a
phase entered more than once adds up, and `other` is the time no phase
claimed, so the phases add up to `total`:

| Phase | Time spent |
| --- | --- |
| `startup` | loading the program's modules and building its commands (the interpreter's own start before that is not measured) |
| `context` | reading the config, roots, project pointer and actor |
| `model` | reading and checking the graph's compiled ontology |
| `snapshot` | opening the graph's current state |
| `authorize` | policy decisions (and loading the policy) |
| `query` | a read answering from the state: `get`, `history`, `find`, `lookup`, `neighbors` |
| `parse` | `query` reading the Cypher text |
| `resolve` | a write resolving its references; `query` resolving names, fields and parameters against the model |
| `execute` | `query` matching patterns and computing the rows |
| `stage` | `file add` hashing and staging the file |
| `lock` | a write waiting for and taking the graph lock, including the recovery of an interrupted write |
| `normalize` | applying the ops to a working copy |
| `validate` | checking the result against the ontology |
| `audit` | appending the write intent or a refusal to an audit stream |
| `commit` | writing the transaction |
| `render` | building and writing the envelope |
| `other` | everything else |

A slow `lock` points at a concurrent writer, a slow `startup` or `context`
at the installation rather than the graph. The line itself is written
after `total` is taken.

Like audit events it never holds a value, an argument, an entity id, a
reason or a message: it records how much a command returned, not what.
It is not chained, so it is a record of use, not evidence: whoever can
write the state directory can edit it. Writing it is best effort; a
failure never fails the command. It is not rotated; rotate or delete it
from outside. `graph-admin` does not write it (its changes are audited).

## Policy verification

Every decision records the policy hash. `audit verify` walks the policy
events of each stream against the installed files and reports
`integrity.audit.policy_mismatch` for a file edited, placed or deleted by
hand, also after a later apply. An installation policy apply also appends
a mirror of the change to every graph stream (under every graph's lock,
anchored by the confirmation), so each graph stream records which policy
was in effect. `verify` checks every graph-stream decision against it:
a write made without the installed policy (a policy moved away and back,
another data root that links the streams) is
`integrity.audit.policy_hash_mismatch`, and a cut installation history
contradicts the mirrors. A writer reads the policies again under its lock
and decides again if one changed, so a concurrent apply is no finding.
graph-admin decisions under its fallback are a warning
(`policy_fallback`); decisions before a graph's first mirror (policies
applied before mirrors existed) are checked loosely (info). Applying the
installed policy again records the mirrors such a graph lacks.

What verification cannot detect: [Security model](security_model.md#detection).
