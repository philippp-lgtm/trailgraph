# Storage

`graph_cli.storage` implements the kernel's storage ports (read, write
session, maintenance, audit sink). It knows no ontology semantics: services
hand it fully computed transactions, and it checks only structural
invariants (sequence, revision chain, hashes, aliases, edge targets).

| Package | Content |
| --- | --- |
| `storage.common` | chained JSONL codec, fault points, commit checks, tx-log replay view (the reference query evaluator is `kernel.query_eval`), the cipher primitives of sealed graphs (`storage.common.crypto`, below) |
| `storage.memory` | `InMemoryStore`, `InMemoryAuditLog`: the reference backend |
| `storage.files` | `FileStore` (one graph directory), `FileAuditLog` (installation audit stream), lock, durable writes, recovery |
| `storage.files.manifest`, `storage.files.releases` | the `manifest.toml` codec; reading manifests and installed ontology releases |
| `storage.files.index` | the disposable index: building and updating it, binding it to the tx log, its log digest, the view over it for readers and writers |
| `storage.files.skill_slot` | reading a graph's published skill (`skill/source.json` and `skill/files/<hash>/`) |
| `storage.files.kinetic_slot` | reading a graph's published function or action catalog, one implementation for both kinds (`<kind>/slot.json`, `<kind>/compiled/<hash>.json`, and with `sources=True` `<kind>/files/<hash>/<name>.yaml`); the app reads only the compiled document |
| `storage.files.policy_files` | installed policies: `policy/installation.json` and `graphs/<id>/policy.json`, read lock-free, replaced by one rename |
| `storage.files.token_files` | the token file `tokens/tokens.json` of the server API and the enrollment codes `tokens/enrollments.json`, read lock-free, replaced by one rename under the registry lock |
| `storage.files.keystore` | the key store of a sealed graph (below): key-encryption key files, data keys wrapped in SQLite, shredding, backup and the comparison behind key verification |
| `storage.files.keyrings` | store formats of a graph directory, the `[keys]` settings and the open key stores of a process (`KeyRings`); imports the key store only for a sealed graph |
| `storage.memory.keyring` | `MemoryKeyRing`: the data keys of a sealed in-memory graph |
| `storage.files.blobs` | stored files by hash: staging, publishing, reading, listing, blob verification and removal |
| `storage.files.maintenance` | `FileStoreMaintenance`: graph creation, manifest switch, release install, skill publish, function and action catalog publish, policy install, `verify`, `verify_blobs`, `remove_blobs`, `recover`, `reindex` (admin only) |

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
  the copies in their tx lines. Without a usable index of format 2, when
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

## Sealed graphs

A sealed graph holds every instance value encrypted under a data key of its
entity, its tx or its stored file, so that destroying the keys erases the
data. `manifest.toml [store] format_version` says which kind a graph is:
`1` unsealed, `2` sealed. `graph-admin graph create` makes sealed graphs;
graphs of format 1 stay readable and writable. The port is `kernel.sealing`,
the codec `kernel.record_codec`, the cipher primitives `storage.common.crypto`
(the only module that imports `cryptography`), the key store
`storage.files.keystore` ([Key store](#key-store)).

- **Records of format 2.** A tx line is `graph_cli.tx/2`: ids, seq,
  timestamps, actor, ontology pin, `request`, `changeset_hash`,
  `idempotency_key`, `blobs`, `alias_allocations` and the chain keys in the
  clear; `changeset`, `resolution` and `reason` in one envelope `sealed`
  under the tx's key. A revision is `graph_cli.revision/2`: id, rev,
  prev_rev, type, record_status, alias (a generated code), timestamps, tx
  id and seq, ontology version, `content_hash` and the edges as `{edge_id,
  rel, target, target_rev}` in the clear; attributes, edge qualifiers (by
  edge id) and valid times in one envelope `sealed` under the entity's key.
  The associated data is the JCS form of `{graph, record, id, seq}` for a
  tx and `{graph, record, id, rev}` for a revision, so an envelope opens
  only where it was written. The idempotency key stays in the clear: it is
  chosen by the caller and carries no value.
- **No plaintext in the data root.** The tx log, the revision files (byte
  equal to the tx copy: a record keeps the envelope it was read or sealed
  with) and the index hold sealed records only; the index's `edge_in` rows
  carry no qualifiers, which readers take from the source revision. Values
  are decrypted in memory where records are parsed; every value filter
  (`find --where`, `lookup`, `search`, Cypher, unique checks) runs over the
  decrypted revisions, as before.
- **Keyed hashes.** `content_hash` is HMAC-SHA256 under the entity key's
  `mac` subkey over the same projection (C.9), `changeset_hash` and the
  request's `args_digest` under the tx key's. The shape stays
  `sha256:<hex>`; the store format says which kind a hash is. A writer
  creates a new entity's key when it computes the hash, and the tx key only
  once the tx will be written; a dry run creates no key. An idempotent
  retry is compared under the previous tx's key; once that key is shredded
  the retry is refused (`conflict.idempotency_key_reuse`).
- **Reading.** Opening never creates a key. A shredded key reads as
  `shredded: true` with empty attributes and qualifiers (a tx: an empty
  change set and no reason); a key missing from the key store, or an
  envelope that does not authenticate, is `backend.corrupt`. A snapshot and
  a write session refresh the key store first, so a shred by another process
  is seen by the next read.
- **Key stores.** `KeyRings` opens one key store per graph and process from
  the `[keys]` settings ([Keys of sealed graphs](configuration.md#keys-of-sealed-graphs)).
  `graph create` creates the key store before it publishes the graph
  directory; one left by an interrupted create names a graph id that never
  appears. The maintenance port reads a graph's format from its manifest; when
  the manifest is unreadable, an existing key store says the graph is sealed.

- A data key has an id (`Kid`, a UUIDv7). A sealed envelope is
  `{kid, nonce, ct}`, `nonce` and `ct` in unpadded base64url; the record
  format that holds it defines the cipher and the associated data.
- `KeyRing` is the port to the keys of one graph: `entity_key` and `tx_key`
  return the key of their subject and create it on first use, `file_key`
  creates one per stored file, `mac_key` gives the key of the keyed hashes,
  `seal` and `open` encrypt and decrypt envelopes, `shred` destroys keys and
  records the purge event. A shredded or missing key raises `KeyUnavailable`.
- Keyed hashes keep the `sha256:<hex>` shape: HMAC-SHA256 over the same JCS
  bytes. `content_hash`, `changeset_hash` and `RequestMeta.of` take an
  optional MAC key; without one they are the plain hashes (C.2). The ring of
  an unsealed graph, `PlainKeyRing`, hands out no keys, so its hashes are
  byte-identical to those of earlier versions.
- `Revision.shredded` and `TxRecord.shredded` mark a record whose key is
  shredded; neither is persisted. `open` raises `EnvelopeInvalid` for an
  envelope that does not authenticate; `refresh` picks up shreds by other
  processes.
- Envelopes are AES-256-GCM under a random 96-bit nonce. A data key yields
  two subkeys by HKDF-SHA256: `enc` for the cipher, `mac` for the keyed
  hashes. Data keys are wrapped with AES-KW (RFC 3394) under a 256-bit
  key-encryption key.
- A stored file of a sealed graph is a header `GCBLOB\x00\x01 || kid (16 bytes) || salt (16 bytes) || nonce prefix (7 bytes)`,
  then chunks of 64 KiB of plaintext (the last one may be shorter, and is
  empty only for an empty file), each with a 16-byte tag. The chunk key is
  derived by HKDF-SHA256 from the file key and the salt. Chunk `i` is sealed
  with AES-256-GCM under the nonce `prefix || i (4 bytes, big-endian) || last (1 byte, 1 on the final chunk)`
  and the header as associated data, so reordered, dropped, repeated or
  appended chunks and a changed header fail to decrypt.

### Key store

The data keys of a sealed graph live in one SQLite file per graph,
`<keys dir>/<graph id>.sqlite` (mode 0600, new directories 0700), outside the
data root: a backup of the data root holds ciphertext only. `KeyStore` is
the `KeyRing` of a sealed graph. Losing the key store or the key-encryption
key loses the data.

- **Key-encryption key (KEK) file:** 64 lowercase hex digits (32 random
  bytes) and an optional newline. It may be a symlink to a regular file, as
  in container secret mounts; nobody but its owner may write it and only
  owner and group may read it. A new one is created with mode 0600 and is
  never replaced. A KEK is named by its id, the first 16 bytes of
  HKDF-SHA256(KEK, info `graph_cli.kek-id/1`) in hex.
- **Schema `graph_cli.keystore/1`:** `meta(key, value)` with `format`,
  `graph_id` and `shreds` (a counter); `kek(kek_id, created_at,
  retired_at)`; `dek(kid, scope, subject, kek_id, wrapped, created_at,
  shredded_at, purge_event)`. `scope` is `entity`, `tx` or `file`; `subject`
  is the entity or tx id, unique per scope, and empty for file keys (the
  blob header names its kid). `wrapped` is the data key under AES-KW. A
  shredded row has no `wrapped`, but keeps kid, scope, subject and the purge
  event as evidence.
- **Durability:** rollback journal (no WAL), `synchronous = FULL`. A new key
  is committed before it is handed out, so it is durable before the record
  that uses it; a key without a record is an orphan, harmless. Processes
  that race for the key of one subject get the same key (`BEGIN IMMEDIATE`,
  then a lookup before the insert).
- **Shredding:** drops `wrapped` of the named keys in one transaction (a key
  absent from the store fails it, a key shredded before keeps its first purge
  event), bumps `shreds`, commits and runs `VACUUM`. `secure_delete = ON`
  zeroes the freed bytes at the commit, `temp_store = MEMORY` keeps the copy
  `VACUUM` makes off the disk. A deleted rollback journal may leave a wrapped
  key in free blocks of the file system, worthless once the KEK that wrapped
  it is rotated away.
- **Caching:** the wrapped keys are read in one query on first use and each
  is unwrapped on its first use. A process checks `PRAGMA data_version` when
  a read begins (`refresh`) and at most once a second otherwise; when another
  process has shredded or removed keys since (`shreds` changed), it drops its
  cache.
- **KEK rotation:** keys unwrap under the KEK their row names; the current
  KEK wraps new keys, earlier ones are passed along while a rotation runs.
- **Checks:** `check` compares the store with the kids the records reference
  and the kids each recorded purge names: referenced but absent (missing),
  shredded without a purge naming it (unrecorded), live but unreferenced
  (orphan). `collect` deletes orphans created before a cutoff, so the key of
  a file staged before the lock survives until its tx is written. `backup`
  writes a consistent copy (SQLite backup API, mode 0600).
- **Fault points:** `keys.before_commit`, `keys.after_commit`,
  `shred.before_commit`, `shred.before_vacuum`. A key or a shred exists
  exactly when its commit returned.
