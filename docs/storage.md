# Storage

`graph_cli.storage` implements the kernel's storage ports (read, write
session, maintenance, audit sink). It knows no ontology semantics: services
hand it fully computed transactions, and it checks only structural
invariants (sequence, revision chain, hashes, aliases, edge targets).

| Package | Content |
| --- | --- |
| `storage.common` | chained JSONL codec, fault points, commit checks, tx-log replay view (the reference query evaluator is `kernel.query_eval`) |
| `storage.memory` | `InMemoryStore`, `InMemoryAuditLog`: the reference backend |
| `storage.files` | `FileStore` (one graph directory), `FileAuditLog` (installation audit stream), lock, durable writes, recovery |
| `storage.files.manifest`, `storage.files.releases` | the `manifest.toml` codec; reading manifests and installed ontology releases |
| `storage.files.index` | the disposable index: building and updating it, binding it to the tx log, its log digest, the view over it for readers and writers |
| `storage.files.skill_slot` | reading a graph's published skill (`skill/source.json` and `skill/files/<hash>/`) |
| `storage.files.kinetic_slot` | reading a graph's published function or action catalog, one implementation for both kinds (`<kind>/slot.json`, `<kind>/compiled/<hash>.json`, and with `sources=True` `<kind>/files/<hash>/<name>.yaml`); the app reads only the compiled document |
| `storage.files.policy_files` | installed policies: `policy/installation.json` and `graphs/<id>/policy.json`, read lock-free, replaced by one rename |
| `storage.files.token_files` | the token file `tokens/tokens.json` of the server API and the enrollment codes `tokens/enrollments.json`, read lock-free, replaced by one rename under the registry lock |
| `storage.files.blobs` | stored files by hash: staging, publishing, reading, listing, blob verification and removal |
| `storage.files.maintenance` | `FileStoreMaintenance`: graph creation, manifest switch, release install, migration tx and marker, skill publish, function and action catalog publish, policy install, `verify`, `verify_blobs`, `remove_blobs`, `recover`, `reindex` (admin only) |

## FileStore

POSIX only, local filesystems only.

- The per-graph transaction log is the source of truth. Each line is the
  canonical JSON of one transaction, chained by hash. Revision files and the
  `head.json` checkpoint are derived and can be rebuilt from the log.
- One exclusive `flock` lock per graph serializes writers; readers take no
  lock and see a consistent snapshot (the last complete log line).
- Scans (`find`, `lookup`, `search`, `neighbors`, `query`, aliases,
  incoming edges, tx records) read the disposable index (below) plus the
  transactions after it. Without a usable index, and for a snapshot
  before its seq (`--as-of`), they verify every log line (canonical form,
  hash, chain) and replay the log into a view that lives as long as the
  process, so their time grows with the size of the log. The
  canonical-form check runs on the C JSON encoder where it agrees with
  JCS and on the reference encoder otherwise; both accept the same lines.
- The index `data/index/index.sqlite` (SQLite, `graph_cli.index/2`) holds
  the state at one seq: the current revision of every entity, every alias
  binding, the incoming edges of current revisions, the position of
  every tx line and the first tx of each idempotency key per principal.
  Its `meta` table records the SHA-256 of the tx log up to the index
  position, per segment (`log_digest`), and the seq of the last full
  replay behind it (`full_verified_seq`). Readers also use an index of
  format 1, which has neither; writers replace it. Queries decode only
  the types they ask for. The index is
  never changed in place: after the checkpoint, a writer copies it (or
  builds a new one after a full replay) into
  `index.sqlite.tmp`, applies its transactions, syncs it and renames it
  over the old one. Readers open it read-only and immutable, so they take
  no lock and create no journal; a reader keeps its file while a writer
  replaces it. A failed update leaves the old index and adds
  `storage.materialization_deferred` to the result.
- A reader binds the index to the log as it binds `head.json`: the tx at
  the index seq must carry the recorded hash and end at the recorded
  position; a tx read through the index is checked against the chain. So
  scans verify the log only from the index position on, as point reads
  do from the checkpoint. Damage before it is found by the next writer
  (below) and by `graph-admin audit verify`, which also compares the index
  and its log digest with the log (`integrity.storage.index_inconsistent`). A missing index is normal (a
  new graph before its first write, a graph last written by an older
  version) and falls back silently; an index that exists but cannot be
  used (another graph, unreadable, ahead of or not matching the log)
  falls back with the warning `storage.index_stale`. `graph-admin graph
  reindex` rebuilds it, as does the next write.
- A writer reads the index too, once the digest of the tx log up to the
  index position equals `log_digest`: equal bytes give the result of the
  full verification behind the index, so it verifies only the txs after
  it. Revisions it reads by id from the index must match their content
  hash, and the current revisions the commit checks rely on must equal
  the copies in their tx lines (each tx line is read and parsed once for all
  the revisions that come from it). Without a usable index of format 2, when
  one of these checks fails, and once 200 txs were written since
  `full_verified_seq`, it replays and verifies the whole log instead and
  rebuilds the index from it. A failed check adds `storage.index_stale`
  to the result, or refuses the commit (`internal.error`) when it was
  already built from the index; the index is rebuilt either way. So
  damage in the log before the index is found by the next writer
  (digest), and a writer verifies the canonical form and chain of every
  tx at least every 200 txs.
- A write appends the audit intent, then the transaction (the commit
  point), then materializes revision files and the checkpoint. Each step is
  synced (`F_FULLFSYNC` on macOS with `durability="full"`).
- A data migration (a major release with renames or a migration file) is one
  transaction whose changeset is `{"schema": "graph_cli.migration/1", "ops": [], "migration":
  {...}}`. It is committed by `MaintenanceSession.migrate`, which runs the steps of a commit
  with different checks: `commit` refuses a changeset with a `migration` key
  (`migration_via_commit`), `migrate` refuses one without. A migration tx needs an
  allow decision for `maint.migrate`, revises existing entities only, may change their type
  and keeps `alias`, `record_status`, `created_at` and the valid time
  (`migration_identity`); it has no ops and allocates no alias. The release brackets
  it with the marker `data/migration.json` (`graph_cli.migration_marker/1`: seq, tx id,
  from and to version, artifact and migration hash), written by one rename before the intent
  and removed by one unlink after the pin switch; `FileStore.migration_marker()` reads it
  lock-free. The marker's tx is committed when the tx at `marker.seq` has the marker's
  `tx_id`, not when `head.seq >= marker.seq`: after a crash before the commit an application
  write can take that seq. While a marker's tx is committed and the manifest still names
  the old version (a release running, or crashed between the tx and the pin switch), the
  app's catalog pins the graph to the marker's version, so reads use the model whose names
  the data has; writes are refused (`write.migration_incomplete`), and reads warn
  `graph.migration_incomplete` when no process holds the graph lock (`writer_active`, a shared
  non-blocking `flock` probe that creates nothing). A marker whose tx is not in the log is
  stale: reads ignore it and the next release removes or overwrites it.
- Every write session first recovers: it truncates a torn last line and
  writes missing derived files. Damage inside the log stops writes with
  `backend.corrupt` until repair.
- Stored files live in `blobs/sha256/<first 2 hex>/<hash>` of the graph
  directory, read-only and never changed. They are staged in
  `blobs/.tmp-*` before the lock and published by one rename under it;
  each transaction lists the hashes its revisions reference (`blobs`), so
  verification and cleanup need no ontology.
- Maintenance effects are published with one rename each: a new graph
  directory (staged as `graphs/.tmp-<id>`), an ontology release directory,
  the manifest, the graph skill's `source.json` (its files are written once
  under their hash before), the function and action catalogs' `slot.json` (their sources
  and compiled documents likewise), an installed policy. A failure before the rename leaves only the
  staging state, which readers ignore.
- Three published slots sit in a graph directory beside the manifest, each
  switched by one rename of its pointer file, with the content written once
  under its hash before and never changed:
  `skill/source.json` (`kind`, tree `hash`, `published_against`) with
  `skill/files/<hash>/`, and `functions/slot.json`
  (`graph_cli.function_slot/1`: the `hash` of the function sources,
  `compiled_hash` and `published_against`, the ontology pin at publish
  time) with `functions/files/<hash>/<name>.yaml` (the authored sources,
  read only by `graph-admin`) and `functions/compiled/<compiled hash>.json`
  (the catalog `graph_cli.functions/1`, the only part the app reads). A
  reader checks the hashes on read; a damaged functions slot is
  `functions.slot_invalid`. The action catalog is the same structure in
  `actions/` (`actions/slot.json` with `graph_cli.action_slot/1`, the sources
  `actions/files/<hash>/<name>.yaml` and the compiled catalog
  `graph_cli.action_catalog/1` in `actions/compiled/<compiled hash>.json`), a
  separate slot so that a broken action never blocks publishing functions; a
  damaged one is `actions.slot_invalid`. The slots are verified against the
  audit trail ([Slot verification](audit.md#slot-verification)); the format of a
  function file is in the [authoring skill](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/authoring-graph-functions/references/format.md), that of an action file in the [actions authoring skill](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/authoring-graph-actions/references/format.md).
