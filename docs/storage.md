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
| `storage.files.manifest`, `storage.files.releases` | the `manifest.toml` codec (formats 1 and 2, [Manifest](#manifest)); reading manifests and installed ontology releases |
| `storage.files.store_format` | which graphs a `FileStore` opens: the one manifest check of the catalog and the maintenance port |
| `storage.files.index` | the disposable index: building and updating it, binding it to the tx log, its log digest, the view over it for readers and writers |
| `storage.files.skill_slot` | reading a graph's published skill (`skill/source.json` and `skill/files/<hash>/`) |
| `storage.files.kinetic_slot` | reading a graph's published function or action catalog, one implementation for both kinds (`<kind>/slot.json`, `<kind>/compiled/<hash>.json`, and with `sources=True` `<kind>/files/<hash>/<name>.yaml`); the app reads only the compiled document |
| `storage.files.policy_files` | installed policies: `policy/installation.json` and `graphs/<id>/policy.json`, read lock-free, replaced by one rename |
| `storage.files.token_files` | the token file `tokens/tokens.json` of the server API and the enrollment codes `tokens/enrollments.json`, read lock-free, replaced by one rename under the registry lock |
| `storage.files.blobs` | stored files by hash: staging, publishing, reading, listing, blob verification and removal |
| `storage.files.blob_backend` | the blob seam: the `BlobBackend` protocol through which `FileStore`, its write sessions, the commit check and maintenance reach stored files; `LocalBlobs` over `storage.files.blobs`; `BlobBackends`, which picks the backend from the manifest's blob location (parsed manifests cached per process by the file's identity) |
| `storage.files.s3_blobs` | `S3Blobs`: stored files in an S3-compatible object store ([Stored files in an object store](#stored-files-in-an-object-store)) |
| `storage.files.blob_move` | the copy pass of `files move`: both locations listed once, every blob the move covers copied to the location the manifest does not name, each verified ([Stored files in an object store](#stored-files-in-an-object-store)) |
| `storage.files.store_marker` | the object store's marker `graph_cli_store.json`: creating it, reading it, and the per-process cache of the identity check |
| `storage.files.maintenance` | `FileStoreMaintenance`: graph creation, manifest switch, release install, migration tx and marker, skill publish, function and action catalog publish, policy install, `verify`, `verify_blobs`, `misplaced_blobs`, `remove_blobs`, `copy_blobs` (the copy passes of `files move`), `recover`, `reindex` (admin only); the object store client for `files store check` |
| `storage.s3` | a small S3 client on the standard library: SigV4 signing, put, streamed and hash-verified get, head, delete, paged listing, retries and error mapping by S3 code; imports only `kernel` (`storage.files.s3_blobs` and `storage.files.store_marker` use it) |

## FileStore

POSIX only, local filesystems only.

```
<data root>/
  audit/                      installation audit stream (events-<first seq>.jsonl segments)
  policy/installation.json    installed installation policy
  tokens/                     tokens.json, enrollments.json
  graphs/<graph id>/
    manifest.toml             identity, state, ontology pin, [store] (see Manifest)
    .lock                     the writer lock
    ontology/<version>/       installed ontology releases
    policy.json               the graph's installed policy
    skill/  functions/  actions/  views/   published slots (see below)
    data/
      txlog/                  transaction log segments (<first seq>.jsonl), the source of truth
      entities/<xx>/<id>/     revision files r<rev>.json, derived
      head.json               checkpoint, derived
      index/index.sqlite      disposable index
      migration.json          migration marker, only during a release with a migration
    audit/                    the graph's audit stream (events-<first seq>.jsonl segments)
    blobs/sha256/<xx>/<hash>  stored files (local location)
```

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
  verification and cleanup need no ontology. Crash windows: after
  `blob.after_stage` or `blob.before_rename` only the staging file exists;
  after `blob.after_rename` the file is in place but no transaction names it
  (an orphan until `files gc`). A retry with the same idempotency key
  publishes or finds it and commits once.
- Stored files go through a blob backend (`storage.files.blob_backend`)
  chosen by the manifest's blob location, never by `[files] store`. Readers
  resolve it per call; a write session binds it on its first blob use under
  the lock (publish and the commit check) and keeps it, except across a
  manifest replaced in the same session. Staging is always a local file. A
  write hands its staged blobs to the store for promotion after authorization
  and before the lock; local blobs need none. A graph
  directory whose manifest is missing or unreadable is served locally.

### Stored files in an object store

A graph whose manifest names `blobs.s3` keeps its stored files in an
S3-compatible object store (`storage.files.s3_blobs`); everything else stays
in the graph directory.

- **Keys.** The bytes of one hash are the object
  `<prefix>graphs/<graph id>/blobs/sha256/<first 2 hex>/<64 hex>`
  (`application/octet-stream`, no user metadata). Finding targets name this
  key, never the endpoint or a credential. Other keys below the graph's
  prefix are not blobs.
- **Marker.** `<prefix>graph_cli_store.json`
  (`{"schema": "graph_cli.object_store/1", "store_id", "created_at"}`)
  identifies the store's content; the manifest names it by `store_id`. It is
  created only when absent, read back after writing and never overwritten
  (`ensure_store_marker`, called by the audited steps that establish a
  location). Every blob access checks that the configured bucket's marker
  carries the manifest's `store_id`; what a bucket's marker says is cached per
  process by `(endpoint, bucket, prefix)` for 10 minutes and read again on a
  404 of a referenced object. A missing marker or another id is
  `backend.unavailable` ("this graph's files are not in that bucket"), never
  an empty listing or missing files.
- **Fail closed on blob access only.** Building the backend does no I/O and
  checks nothing, so reads and writes that touch no file never need the
  object store. A blob access without usable settings (none, a
  `[files.s3]` error, no credentials) or against a foreign bucket is
  `backend.unavailable` naming the graph and what is missing. Every revision
  of a file lists its blob, so any write to a file entity needs the store.
- **Write path.** Staging is the local staging file. The promote (before the
  lock, after authorization) `HEAD`s each staged hash and uploads it when the
  object is absent, has another size, or its `LastModified` is older than 12
  hours by the store's clock (the answer's `Date`), so a gc cannot remove it
  before the commit; uploads are signed with the real SHA-256, which the store
  verifies. The publish under the lock confirms the object by `HEAD` (present,
  same size), uploads from the staging file only when it is missing, and then
  removes the staging file. The commit check answers from the hashes the
  session published or confirmed, else by `HEAD`; above 64 unconfirmed hashes
  one paged listing replaces the `HEAD`s, and a hash the listing lacks is
  confirmed by `HEAD` before it is reported missing (listings may lag).
- **Reads** stream the object hashed against its name; the last chunk is held
  back until the hash matches. An absent referenced object or bytes that
  differ from their hash are `backend.corrupt`, like a missing local blob.
  There is no local path (`blob_local_path` capability absent): `graph
  file path` answers a copy in this machine's
  [file cache](files.md#file-cache), and the server and the live view
  stream the verified bytes (`FileService.open`, `services.files.open_stored`).
- **Getting there.** A graph is always created with its files local; with
  `[files] store = "s3"`, `graph-admin graph create` then switches it in the
  same command: the marker is read or created before the graph exists,
  then a `change.blob_store` intent ([Blob location](audit.md#blob-location)),
  the manifest replaced by one rename as format 2, and the anchor. A switch
  that fails after the creation leaves the graph local (format 1, holding no
  file) and the command names it.
- **Moving a graph** (`graph-admin files move`, `storage.files.blob_move`,
  `services_admin.files_move`). A copy pass lists both locations once and
  copies to the location the manifest does not name every blob the move
  covers: each hash a tx names and every blob stored at the source (orphans
  and old copies too; `.tmp-` staging files never), skipping those the target
  holds with the same size (the name is the hash). A referenced hash a
  listing lacks is confirmed by `HEAD` or `stat` first; one in neither
  location refuses the move (`backend.corrupt`). Copies are verified
  (`BlobBackend.receive`): into an object store as a `PUT` signed with the
  hash, then a `HEAD` for the size; into the data root through a staging file
  hashed while written and synced, mode 0444, renamed into
  `blobs/sha256/<xx>/<hex>` with both directories synced (a present file of
  another size is damaged and replaced). Passes run without the graph lock,
  so a concurrent gc may remove a copy as misplaced; nothing depends on
  them. The switch runs one more pass under the lock (the tx log read whole
  by replay), so a file committed meanwhile or a copy gc removed is copied
  before the intent; then the `change.blob_store` intent, the manifest
  through `replace_manifest` (one rename, format 1 <-> 2; the session
  rebinds its blob backend), the anchor. `graph create`'s switch is the same
  code with nothing to copy. A file add racing the move commits where the
  manifest names: its session binds the location under the lock, and the
  publish uploads from, or renames, its local staging file when the bound
  location lacks the blob (the promote before the lock went to the location
  named then). A pass or switch with a time budget asks it before every copy
  after the first; a switch whose copies run out of time releases the lock
  without an intent. Nothing is removed: the source's copies wait for gc.
  Crash windows: `blob.after_copy` (after each verified copy) leaves extra
  copies and no intent; `manifest.before_rename` an intent without effect
  (the graph whole in the old location); after the rename the graph is
  switched, with or without its anchor.
- **Verify and gc.** Blob verification (`files verify`, and the check of
  referenced files in `audit verify`) lists the graph's prefix once and
  confirms a referenced hash the listing lacks by `HEAD` (`--rehash` streams
  every object); the finding codes are those of local blobs, plus:

  | Code | Severity | Meaning |
  | --- | --- | --- |
  | `integrity.storage.blob_misplaced` | info | a copy outside the location the manifest names: a file in `blobs/sha256/` of a graph in an object store, or, when an object store is configured with credentials, an object below `graphs/<id>/blobs/` of a local graph (`.tmp-` staging files are not copies) |
  | `integrity.storage.blob_store_unavailable` | error | the object store of a graph in an object store cannot be used: no usable settings, no credentials, a marker that is missing or carries another id, refused credentials, no answer; it stands for every other finding about the store (staging leftovers are still listed), and its `actual` carries the error's code and safe details |

  A local graph never fails because of the object store: copies there are
  looked for only when it is configured with credentials, answers and has
  a marker. They are the graph's own only when the marker's id is the
  `store_id` of the graph's last recorded `change.blob_store` switch, and
  only when that switch went to `local` (read from the graph's audit
  stream, which must verify, and only when there are such objects);
  otherwise a cloned or restored installation may share the bucket and the
  graph id, so they are reported ("not this graph's recorded store") and gc
  never deletes them. The message of a misplaced copy says whether the named
  location lists the same hash and size (with `--rehash`, also whether its
  bytes match). With an unreadable manifest the blob location is
  unknown, so `audit verify` judges no blob (the manifest is `pin_mismatch`).
  Only orphans older than 24 hours by the store's clock (the listing's
  `Date`) are offered to and removed by gc, since an add is an orphan between
  its promote and its commit. Staging files older than one hour go too, as for
  local graphs (a younger one may belong to a write in flight).
  Misplaced copies go only when the named location holds the same bytes:
  one listing shows their hash with the same size and the bytes there, read
  in full (a verified streamed `GET`, a local rehash), have that hash. The
  bytes are read once, before the graph lock (stored files are immutable),
  so writes are not blocked while a large store is read; under the lock one
  listing made right before the removal, before any orphan goes, checks
  presence and size again (and the marker's id for an object store). So
  neither an aborted move nor a damaged or unreadable copy in the named
  location loses a file; the others stay (`misplaced_kept`). The first gc
  after a move reads every moved file once and reports nothing until it is
  done (no progress line yet). A gc holds the graph lock for the rest, as
  the publish does. `graph-admin files store check` probes a store without any graph.
- **Crash windows** (in addition to the local ones above):

  | Point | Window | Durable after `kill -9` |
  | --- | --- | --- |
  | `blob.after_stage` | after staging, before any upload | the staging file |
  | `blob.after_upload` after the promote | after the promote's upload | the object (an orphan) and the staging file; no intent |
  | `blob.after_upload` after the publish fallback | after the upload under the lock | the same |
  | `blob.before_staging_removal` | after the publish confirmation | the same |

  A retry with the same idempotency key finds the object, commits once and
  uploads nothing; the staging file of the crash stays until gc, an object
  nobody commits is removed by gc after 24 hours.
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
  `compiled_hash`, `published_against`, the ontology pin at publish
  time, and `featured`, the digest of what a graph skill shows of the catalog, derived at publish
  so the freshness check reads no compiled catalog; a slot without it is computed once per
  process) with `functions/files/<hash>/<name>.yaml` (the authored sources,
  read only by `graph-admin`) and `functions/compiled/<compiled hash>.json`
  (the catalog `graph_cli.functions/1`, the only part the app reads). A
  reader checks the hashes on read; a damaged functions slot is
  `functions.slot_invalid`. The action catalog is the same structure in
  `actions/` (`actions/slot.json` with `graph_cli.action_slot/1`, the sources
  `actions/files/<hash>/<name>.yaml` and the compiled catalog
  `graph_cli.action_catalog/1` in `actions/compiled/<compiled hash>.json`), a
  separate slot so that a broken action never blocks publishing functions; a
  damaged one is `actions.slot_invalid`. The view catalog is the same again in
  `views/` (`views/slot.json` with `graph_cli.view_slot/1`, the sources
  `views/files/<hash>/<name>.yaml` and the compiled catalog `graph_cli.view_catalog/1` in
  `views/compiled/<compiled hash>.json`); a damaged one is `views.slot_invalid`. A graph
  without views has no `views/` directory. The slots are verified against the
  audit trail ([Slot verification](audit.md#slot-verification)); the format of a
  function file is in the [authoring skill](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/authoring-graph-functions/references/format.md), that of an action file in the [actions authoring skill](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/authoring-graph-actions/references/format.md).

## Manifest

`graphs/<id>/manifest.toml` holds the graph's identity, state, ontology pin,
alias prefixes, extensions and `[store]` (`backend = "files"`,
`format_version = 1`: the layout above). It is written by one rename, with a
fixed key order.

- **Format 1** is the manifest of a graph whose stored files are local. Such
  a graph is always written as format 1, byte for byte as before format 2
  existed.
- **Format 2** is format 1 plus a top-level `requires`: the storage features
  a reader must know to open the graph. A reader that does not know one
  refuses the graph with `format.unsupported_version` naming the feature; a
  program that knows only format 1 refuses it by the top-level
  `format_version`, so no older program reads or rewrites such a graph.
  A new storage feature gets a new feature name, not a new format number.
- The defined feature is `blobs.s3`: the stored files are
  in an S3-compatible object store, named by `[store.blobs]` with
  `location = "s3"` and the object store's `store_id` (a UUID). `requires`
  and `[store.blobs]` must agree, and a format-1 manifest carries neither;
  anything else is `config.invalid`. Such a graph opens like any other; its
  blob accesses are refused with `backend.unavailable` while the object store
  is not usable ([Stored files in an object store](#stored-files-in-an-object-store)),
  never read as if its files were local.

```toml
format_version = 2
requires = ["blobs.s3"]

# [graph] and [ontology] as in format 1

[store]
backend = "files"
format_version = 1

[store.blobs]
location = "s3"
store_id = "<uuid>"
```

The catalog (`FileCatalog.file_store`: app and server) and the maintenance
port (`FileStoreMaintenance.store`: `graph-admin`) make the same check before
they open a graph's store (`storage.files.store_format`); another backend or
layout version is `format.unsupported_version`. The maintenance port leaves a
manifest that cannot be read at all to `verify`, which reports it
(`pin_mismatch`). Every manifest rewrite (the pin switch of a release) keeps
`[store]` as read, so the location is never dropped; `graph-admin graph show`
reports it as `store.blob_location` and `store.store_id`.
