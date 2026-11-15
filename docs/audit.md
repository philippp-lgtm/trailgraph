# Audit

`graph_cli.audit` builds and verifies `graph_cli.audit/1` events. It is
pure: storage appends and chains the events, audit never touches files.
Each graph has its own stream; installation-wide events go to the
installation stream.

| Module | Content |
| --- | --- |
| `audit.events` | builders for write intents, refusals, maintenance events (the `change` of a release, pin and migration intent), graph genesis and anchors; each routes its event to the graph or the installation stream |
| `audit.schema` | the event shape; builders refuse their own output if it misses it |
| `audit.verify` | `verify_stream` over raw lines, `cross_check` (tx log against write and migration intents), `check_migrations` (migration txs: kept identity, versions, digest, the pin that follows), `check_maintenance` (release and pin intents against their effects, pin events against the manifest, skill, function catalog and action catalog events against the published slots), `check_policy` and `check_policy_hashes` (installed policies and the policy behind every graph-stream decision), `check_anchors`, and `verify_graph` / `verify_installation` composing them |
| `audit.pins` | the chain of versions a graph was pinned to, read from the `maint.ontology_pin` intents of its stream (the name map's input; not verified, `audit verify` does that) |
| `audit.finding_codes` | `integrity.audit.*` |

## Verification

`graph-admin audit verify` runs the verifier. It reads raw lines
(`AuditSink.read_lines`) and reports damage as findings instead of stopping
at it. Each damaged line gives at most one chain finding, so one deleted or
edited line does not flood the report. A write intent without a committed
transaction is the benign trace of an interrupted write (info); a
transaction without its intent is an error. Events carry ids, types, hashes,
sizes and reason text, never attribute values.

The actor of an event, identical to the one in the transaction record, names
the roles of the token a server request came with: `actor.roles`, a sorted
list of role names, present only when the token held any. Events of
requests without roles and of local invocations have no such key and are
unchanged. The shape check rejects malformed roles
(`integrity.audit.event_malformed`). The roles a token holds now are the
result of its create and role-change events, which `audit verify` replays
against the token file ([Token verification](#token-verification)).

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

The decisions of an event (`authz.decisions`: action, resource, effect, rule) name
resources of a graph, a type or, below a type, one attribute (`graph:<id>/type:<curie>/attr:<name>`)
or one relation (`.../rel:<curie>`). A write is decided on the attributes and relations it
touches for the types some rule names terms of ([Decisions on attributes and
relations](access_control.md#decisions-on-attributes-and-relations)), and the intent or the
refusal records those decisions next to the one on the type; a policy without such rules
records the decisions on types alone, so its events are unchanged. The shape check takes any
resource string, and the policy hash of these decisions is verified like that of any other
([Policy verification](#policy-verification)).

For a graph with [read limits](access_control.md#read-limits) a write also decides
`read.get` on the type of every operation (the visibility of the type, see [What a caller
can see](access_control.md#what-a-caller-can-see)) and records it next to the write decision;
other graphs record none, so their events are unchanged. A write of a hidden type is refused
as for a type that does not exist, and the event is a `denied` event in the installation
stream with the decisions that hid the type, even when the caller was answered with the
validation error `data.structural.unknown_type`: the caller is told nothing about the type,
the audit says why. A refusal as `write.invalid` (an `invalid` event) records the findings
the caller was shown, so a finding about hidden data is recorded concealed as well.

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
| `promote` | `file add` handing the staged file to the graph's file store once the write is authorized (the upload when the graph keeps its files in an object store) |
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
expiry, an admin token with `scope: admin`, a token with
[roles](access_control.md#roles) with `roles`, sorted) before they replace
the file, never the token itself. `token role add|remove` appends
`maint.token_update` with `change.token{name, hash_prefix, roles}`, where
`roles` is the token's **full set after the change** (empty when the last
role was taken off), so replaying it is an assignment; a change that changes
nothing appends no event. `audit verify` replays these events: a token in the
file that they do not create, or that differs in prefix, project, expiry,
scope or roles, is
`integrity.audit.token_mismatch` (added or widened by hand, roles added or
removed by hand, or a revoke that
was interrupted: revoke it again); a created token missing from the file is
an interrupted create (`intent_without_effect`, info). A missing `roles` key
means none, in the file and in an event.

Enrollment codes live in `tokens/enrollments.json` as SHA-256 hashes, by
id, until they are used or revoked. `enroll create` appends
`maint.token_create` with `change.enrollment{id, hash_prefix, project,
scope, expires_at}` (and `roles` when the code carries any), `enroll revoke` `maint.token_revoke` with
`change.enrollment{id, hash_prefix}`, each before the file is replaced. An
exchange appends one `maint.token_create` whose actor is the enrollment
(principal `enrollment:<id>`, project verified, decision rule
`enrollment:<id>`) with both `change.token` and `change.enrollment{id,
hash_prefix}`, then removes the code and only then adds the token, so an
interrupted exchange never leaves a code that works twice. `audit verify`
replays these events against the file: a listed code they do not create,
that they removed, or that differs (its roles included) is
`integrity.audit.enrollment_mismatch`
(revoke it again), and so is an exchange of a code that was not live, of
another project, scope or set of roles than the code's, or after the code expired; a
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

## Migration verification

A release with a data migration commits one transaction whose changeset has a `migration`
key (`from`, `to`, `migration_hash`, `summary`, the derived renames and the steps). It is
announced like a write: before the commit an `ok` `maint.migrate` event is appended to the
graph stream with the transaction's `event_id` and `change.migration{tx_id, tx_seq,
changeset_hash, changeset_size, from, to, migration_hash, revisions_hash,
counts{entities, edges}}`. `revisions_hash` is the hash of `[id, rev, content_hash]` per
revision, sorted by id, so the event stays constant in size and lists no entity (the
transaction has them). A `maint.migrate` event of a graph stream without that payload is
malformed (`event_malformed`); the installation stream's `maint.migrate` events (another
payload) are not constrained.

`cross_check` matches transactions and intents by kind: a transaction with a `migration`
key needs a migration intent and a write transaction a write intent. An intent of the
other kind with the transaction's event id does not match it (`tx_without_intent`, "differs
in kind") and stays an intent without effect. The id, seq, changeset hash and size of a
migration intent are compared like those of a write intent.

`check_migrations` judges every migration transaction:

| Code | Severity | When |
| --- | --- | --- |
| `integrity.audit.migration_changed_identity` | error | a revision's `alias`, `record_status`, `created_at`, `valid_from` or `valid_to` differs from the entity's previous revision, or it has no previous revision |
| `integrity.audit.migration_version_mismatch` | error | a revision's `ontology_version`, the transaction's pin, the changeset's `migration.to` and the intent's `from` and `to` are not the same versions |
| `integrity.audit.migration_revisions_mismatch` | error | the intent's `revisions_hash` or `counts.entities` differs from the transaction |
| `integrity.audit.migration_unpinned` | error | no `maint.ontology_pin` intent to `to` follows the migration intent in the stream, and no migration marker names the transaction |
| `integrity.audit.migration_incomplete` | warning | the migration marker names the transaction and no pin intent to `to` follows: the crash between the commit and the pin switch, completed by running the release again |

Each finding is one per transaction; the identity finding lists the first 20 revisions with
the fields that differ. A migration whose pin intent follows but whose marker is still
there is clean (the crash between the pin switch and the marker removal). `audit verify`
reads the marker from the store; a damaged marker counts as none.

A release and a pin intent carry two optional keys next to `change.release` and
`change.pin`, never inside them: `change.migration_hash` (the hash of the release's
migration file) on a release with a migration, and `change.alias_prefixes{added, moved}`
(prefixes given to classes, and prefixes that moved with a renamed or retyped class, new
CURIE to `{from, prefix}`) on a pin that changes alias prefixes. Without them the events are
unchanged. A verifier from before migrations reports a migration transaction as
`tx_without_intent` and no more: upgrade it before the first migration.

What verification cannot detect: [Security model](security_model.md#detection).
