# Audit

`graph_cli.audit` builds and verifies `graph_cli.audit/1` events. It is
pure: storage appends and chains the events, audit never touches files.
Each graph has its own stream; installation-wide events go to the
installation stream.

| Module | Content |
| --- | --- |
| `audit.events` | builders for write intents, refusals, maintenance events, graph genesis and anchors; each routes its event to the graph or the installation stream |
| `audit.schema` | the event shape; builders refuse their own output if it misses it |
| `audit.verify` | `verify_stream` over raw lines, `cross_check` (tx log against write intents), `check_maintenance` (release and pin intents against their effects, pin events against the manifest, skill, function catalog and action catalog events against the published slots), `check_policy` and `check_policy_hashes` (installed policies and the policy behind every graph-stream decision), `check_anchors`, and `verify_graph` / `verify_installation` composing them |
| `audit.finding_codes` | `integrity.audit.*` |

## Verification

`graph-admin audit verify` runs the verifier. It reads raw lines
(`AuditSink.read_lines`) and reports damage as findings instead of stopping
at it. Each damaged line gives at most one chain finding, so one deleted or
edited line does not flood the report. A write intent without a committed
transaction is the benign trace of an interrupted write (info); a
transaction without its intent is an error. Events carry ids, types, hashes,
sizes and reason text, never attribute values.

A write by `graph do` names its action: `request.action` is `{name, hash}` in the
intent, in a refusal event and in the transaction record (`request.command` is `do`;
`args_digest` covers the action, its bound parameters, the dry-run flag, the reason and the
idempotency key). The key is optional and appears only for such writes, so the bytes of
every other write are unchanged; verification checks its shape. An action writes one
transaction, so it has one intent, whatever the number of ops it expanded to. Its
refusals are audited like other writes' refusals: `access.denied` in the installation
stream, a failed precondition (`action.precondition_failed`) as an `invalid` refusal in
the graph's stream whose target lists the entities of the failed preconditions, with the
action in `request.action`. Not audited: `--dry-run`, an action with nothing to do
(`action.nothing_to_do`), usage errors such as a missing parameter or a missing reason,
and an unknown or hidden action. `history` shows the `action` next to the `command` of a
revision it wrote.

In a [sealed graph](storage.md#sealed-graphs) the hashes in events are keyed,
so they cannot be checked against guessed values: `change.changeset_hash`,
`request.args_digest` and the entities' `after` and `before` content hashes of
a write intent are HMACs under the keys of its tx and entities, the same values
the transaction log holds. A write intent carries no `reason` (it is sealed in
the transaction). A refusal (`denied`, `invalid`, `conflict`) of a request to a
sealed graph carries no `reason` either, and its `args_digest` is keyed under a
one-time key that is discarded at once: it keeps the hash shape and matches
nothing. Maintenance events keep their reason in the clear; it cannot be
erased, so it should name a ticket, not a person.

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
| `output` | `{format: json\|text, bytes, spilled_bytes}`: the bytes the caller received on stdout, and the size of an answer that went to a file instead (null otherwise) |
| `timing_us` | `{total, phases}`: how long the command took, in microseconds, and where the time went |
| `function` | only for `run`: `{name, hash}` of the function that ran (`hash` is `null` when the run failed, except for `function.stale`); never a parameter value |
| `action` | only for `do`: `{name, hash}` of the action that ran (`hash` is `null` when it failed, except for `action.stale`); never a parameter value |
| `query` | only for `query` (also with `--explain`; a parse or resolve error has none): `{shape_hash, shape, covered_by}`. `shape` is the canonical [query shape](#query-shape), or `null` above 4 KiB; `covered_by` lists at most three `{name, list_fit}`, the published functions that cover the query and that the caller may run |

If the envelope names no graph, `graph` still names the graph a read opened,
and a failed `run` still names its `function`.

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
`graph-admin usage report` reads it ([`cli_graph_admin.md`](cli_graph_admin.md)).

### Query shape

A shape is the query with everything the caller chose removed, so the log
shows how queries are built and never what was asked. Model terms (types,
relations, fields) stay; every literal and parameter becomes a typed
placeholder: `$s` (text), `$n` (number), `$b` (boolean), `$list` (a list of
constants or parameters) and `$map` (a map parameter). `null` stays `null`.
Variables, aliases, map keys and property keys that are no model field are
renamed to numbered names; `SKIP` and `LIMIT` are dropped. The shape is
hashed (`shape_hash`) and logged as text up to 4 KiB.

## Token verification

Bearer tokens of the server API live in `tokens/tokens.json` as SHA-256
hashes; `token create` and `token revoke` append an installation event
(`change.token{name, hash_prefix}`, a created token also with project and
expiry, an admin token with `scope: admin`) before they replace the file,
never the token itself. `audit verify` replays these events: a token in the
file that they do not create, or that differs in prefix, project, expiry or
scope, is
`integrity.audit.token_mismatch` (added or widened by hand, or a revoke that
was interrupted: revoke it again); a created token missing from the file is
an interrupted create (`intent_without_effect`, info).

Enrollment codes live in `tokens/enrollments.json` as SHA-256 hashes, by
id, until they are used or revoked. `enroll create` appends
`maint.token_create` with `change.enrollment{id, hash_prefix, project,
scope, expires_at}`, `enroll revoke` `maint.token_revoke` with
`change.enrollment{id, hash_prefix}`, each before the file is replaced. An
exchange appends one `maint.token_create` whose actor is the enrollment
(principal `enrollment:<id>`, project verified, decision rule
`enrollment:<id>`) with both `change.token` and `change.enrollment{id,
hash_prefix}`, then removes the code and only then adds the token, so an
interrupted exchange never leaves a code that works twice. `audit verify`
replays these events against the file: a listed code they do not create,
that they removed, or that differs is `integrity.audit.enrollment_mismatch`
(revoke it again), and so is an exchange of a code that was not live, of
another project or scope than the code's, or after the code expired; a
created code missing from the file is an interrupted create
(`intent_without_effect`, info). The token of an exchange is replayed with
the other tokens. The server reads the same events before an exchange: a
code they used or revoked is refused even while the file still lists it.

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

## Slot verification

Publishing the graph skill, the function catalog or the action catalog is one maintenance
operation: an intent in the graph stream with `change.skill{from, to}`,
`change.functions{from, to, compiled_hash}` or `change.actions{from, to, compiled_hash}`
(the tree hash of the published files before,
`null` for the first publish, and after; for the catalog also the hash of the compiled
catalog the app reads), the slot switch, then an
installation event that anchors the intent. `audit verify` walks these
intents from "no slot" and compares the end with the slot on disk: the
published hash must equal the last committed `to`, and for the function and
action catalogs the `compiled_hash` of their slot (whose file content is hash-checked on
read) must equal the one the last committed event recorded; a replaced compiled
catalog with an adjusted `slot.json` is caught this way. A slot published,
changed, damaged or removed by hand, or a skill or catalog that differs
from its recorded hash, is `integrity.audit.slot_mismatch` (its target names
`graphs/<id>/skill`, `.../functions` or `.../actions`); an intent
whose slot switch never happened is an interrupted publish
(`intent_without_effect`, info).

What verification cannot detect: [Security model](security_model.md#detection).
