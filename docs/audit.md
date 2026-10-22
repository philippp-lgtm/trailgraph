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
none and are matched without it. Reads are not audited, so the size of
what a read returns is not recorded.

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
