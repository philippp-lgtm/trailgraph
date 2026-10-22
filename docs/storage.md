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
| `storage.files.skill_slot` | reading a graph's published skill (`skill/source.json` and `skill/files/<hash>/`) |
| `storage.files.policy_files` | installed policies: `policy/installation.json` and `graphs/<id>/policy.json`, read lock-free, replaced by one rename |
| `storage.files.blobs` | stored files by hash: staging, publishing, reading, listing, blob verification and removal |
| `storage.files.maintenance` | `FileStoreMaintenance`: graph creation, manifest switch, release install, skill publish, policy install, `verify`, `verify_blobs`, `remove_blobs`, `recover`, `reindex` (admin only) |

## FileStore

POSIX only, local filesystems only.

- The per-graph transaction log is the source of truth. Each line is the
  canonical JSON of one transaction, chained by hash. Revision files and the
  `head.json` checkpoint are derived and can be rebuilt from the log.
- One exclusive `flock` lock per graph serializes writers; readers take no
  lock and see a consistent snapshot (the last complete log line).
- Scans (`find`, `lookup`, `neighbors`, aliases, incoming edges) verify
  every log line (canonical form, hash, chain) and replay the log into a
  view that lives as long as the process. Each `graph` command is a new
  process, so its `query` time grows with the size of the log. The
  canonical-form check runs on the C JSON encoder where it agrees with
  JCS and on the reference encoder otherwise; both accept the same lines.
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
  under their hash before), an installed policy. A failure before the rename leaves only the
  staging state, which readers ignore.
