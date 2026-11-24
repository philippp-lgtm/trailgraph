# Files

A graph whose ontology imports the standard module `files` (`imports:
[{module: files, version: "^1.0"}]`) stores files: scans, documents,
saved web pages, any media type. graph_cli stores, addresses and
references them; it never reads what is *in* them (text, meaning). An
agent reads a stored file itself and writes what it finds through the
normal write commands. What the bytes declare about themselves (size,
type, page count, pixel size, table shape, database schema) graph_cli
extracts deterministically as [metadata](#metadata). The commands are
`graph file add`, `graph file path` and `graph file check` (see
[`graph`](cli_graph.md)) and `graph-admin files verify`, `graph-admin files gc`,
`graph-admin files move`, `graph-admin files extract`,
`graph-admin files extractors` and `graph-admin files store check` (see
[`graph-admin`](cli_graph_admin.md)).

## Model

- `files:File` holds `title` (the main label), `content` (type `blob`:
  `sha256:<hex>` of the bytes, unique among active files, immutable),
  `media_type`, `file_name`, `origin` (type `local_path`), `source_url` and
  `retrieved_at`. A graph module may subclass it (`is_a: files:File`) to
  give files domain meaning.
- `media_type` is the type the file was **recorded** with when it was added:
  the magic bytes of its first 16 bytes, else its extension (`.txt`, `.md`,
  `.csv`, `.tsv`, `.html`, `.json`, `.xml`, Office and OpenDocument types),
  else `application/octet-stream`, or what `--media-type` says. It is
  immutable and may be wrong (a text file named `.pdf`); the type the bytes
  have is the [`detected_media_type`](#metadata) of the file's format record.
- `files:from_file` points from any entity to a file, with the qualifiers
  `page` and `last_page` (from 1) and `locator` (a section, region or task
  number).
- A file's content never changes. A new version is a new `file add`
  followed by `supersede`. Nothing that a revision ever referenced is
  removed; `graph-admin files gc` removes only files no transaction names.
- Web sources need no stored copy: an `iri` attribute plus a `date` for
  the retrieval. A saved copy is a stored file with `source_url` and
  `retrieved_at`. graph_cli fetches nothing from the network.

## Metadata

What a file's bytes say about themselves is stored as **metadata records**:
ordinary entities of the standard modules, found, queried and referenced like
any other, and **read-only**: only the extraction writes them, so none can be
edited by hand or drift from the bytes ([Security](security_model.md#derived-file-metadata)).
A record is the current result of one *extractor* for one file: a pure
function of the file's bytes and the extractor's version. The file's content
never changes, so a result is valid until a newer version of the extractor
exists.

| Module | Extractor | Records | Applies to |
| --- | --- | --- | --- |
| `files` 1.1 | `format` | `files:FormatMetadata`: `detected_media_type`, `media_type_mismatch`, `size`, `text_encoding` | every file; always on with `files` 1.1 |
| `files_pdf` | `pdf` | `files_pdf:PdfMetadata`: `page_count`, `pdf_version`, `encrypted` | detected PDF |
| `files_image` | `image` | `files_image:ImageMetadata`: `width`, `height` in pixels as stored (no rotation, no EXIF) | detected PNG, JPEG, GIF, WebP |
| `files_table` | `table` | `files_table:TableMetadata`: `delimiter`, `quote_char`, `has_header`, `column_count`, `row_count`, `column_names` (the first 200), `columns_truncated` | text recorded as CSV or TSV whose content confirms a delimiter |
| `files_sqlite` | `sqlite` | `files_sqlite:SqliteMetadata`: counts of tables, views, indexes and triggers, `user_version`; `files_sqlite:SqliteObject` per table and view (the first 500; `objects_truncated` says more exist) with `kind`, `column_count`, `column_names` | detected SQLite database |

A graph opts in per kind: importing the module (`files_pdf`, ...) enables its
extractor, and a graph that imports none pays nothing in its model.
`graph-admin files extractors` lists what the program can extract and the
lowest module version each needs; a graph that imports a module at a lower
version keeps its older records and `files extract` reports `module_outdated`
until the next ontology release takes the current version.

- **The type comes from the bytes, never from the name.** `format` detects
  the type from the first 64 KiB (signatures, SQLite, zip and OpenDocument,
  gzip, then a text test), every other extractor dispatches on that result,
  and `media_type_mismatch` says that the recorded type cannot be true of the
  detected one. A binary file named `.csv` is recorded as `text/csv`, is
  detected as `application/octet-stream` and gets no table record. The one
  exception is delimited text: no signature tells it, so `table` applies to
  detected text that was *recorded* as CSV or TSV (`.tsv` is mapped at add
  time) and the content has to confirm a delimiter.
- **Status** is `ok`, `failed` (the type applies but the bytes cannot be read
  as it: `problem` holds a stable code such as `pdf.unreadable`,
  `pdf.encrypted`, `table.no_dialect`, `sqlite.unreadable`, `limit.time`) or
  `not_applicable` (an earlier version applied and the current one does not
  accept the file). A failed result is a recorded result, not a missing
  value. A *transient* failure (the extractor process was killed, the stored
  copy is unreadable) says nothing about the bytes: it is reported and never
  recorded, and the next run tries again. Every attribute a version does not
  produce is unset, so a record never mixes two versions.
- **Retroactive.** `graph-admin files extract --graph G` brings every file to
  the current version of every enabled extractor: after upgrading the
  program, after an ontology release that imports a kind, after a limit was
  raised (`--retry-failed`). A second run writes nothing. A new version
  updates the same record (its id and the edges that point at it stay;
  `history` and `--as-of` show what each version said); a part the new
  version no longer produces is retracted; a record that said `ok` and now
  says `failed` or `not_applicable` is a *regression*, listed by
  `--dry-run` and written. A text file renamed `.pdf` never reaches the PDF
  extractor; if an older format version detected it as a PDF, its PDF record
  becomes `not_applicable` and the history keeps what the older version
  claimed. A record of a **newer** version than this program's is left alone
  and reported (`newer_version`): an older install never downgrades.
- **Parts.** A record owns its parts (a table of a database) and its edges to
  them. The parts of a new version are diffed against the stored ones by key:
  the same key is updated if changed, a new key is created, a gone key is
  unlinked and retracted. Their names come from the file and are untrusted:
  every character that is not printable becomes a blank, the name is trimmed
  and cut to 200 characters and 400 bytes, and names that collide get ` (2)`,
  ` (3)`. People point *at* parts with their own edges; a relation of the
  graph to a part that says `on_target_inactive: refuse` makes the new version
  of that one record wait (`blocked_by_references`), nothing is written half.
- **When.** `graph file add` extracts the file it stored in the same command,
  on the machine that runs it (in the remote mode, on the server, which holds
  the bytes), as the caller, and answers `metadata`; `--no-extract` skips it,
  and a failure never fails the add: what could not be written is the warning
  `file.extraction_incomplete`. A caller who may add files but not write the
  record classes (see [Access control](access_control.md#metadata-records))
  stores the file with that warning, which names the missing right (found by a
  dry run, so nothing is audited per add); a maintainer's `files extract`
  writes the records later. `graph-admin files extract` is maintenance: it needs the
  maintain permission on the graph (`maint.reindex`), writes as the
  maintainer, and takes `--file`, `--extractor`, `--retry-failed` and
  `--dry-run` (which parses every file and writes nothing). Records of files
  that are no longer active (superseded, retracted) are retracted by it.
  `graph-admin files verify` names the records behind their extractor.
- **Isolation.** Every extractor runs in a child process with an empty
  environment, one read-only path, a wall-clock timeout and CPU and memory
  limits (`[extract]`, [Configuration](configuration.md)); a limit hit is
  `failed` with `limit.time` or `limit.memory`. With an object store the
  bytes are streamed into one private temporary file per file and run, shared by
  its extractors and checked against its hash. On a local data root the child reads the stored file
  directly, so a stored file altered on disk yields a result of the altered
  bytes until `files verify --rehash` finds it.
- **Limits of the parsers.** The PDF extractor uses `pypdf`, which opens an
  encrypted file only with an empty user password; AES-encrypted PDFs,
  including the common "restricted but opens without a password" kind, get
  `failed pdf.encrypted` and no page count, because the package needed for
  AES is deliberately not a dependency. Output depends on the parser library,
  the interpreter's `csv` module and the SQLite library; a change that alters
  the golden corpus bumps the extractor's version.
- **Not extracted** (later): EXIF and location of photos, the text layer of
  PDFs, row counts of database tables, Office and OpenDocument records, HEIC
  size, audio and video.

## Origins and roots

Origins are machine-neutral: `{"root": <name>, "path": <relative path>}`
against named roots that each machine maps in `config.toml`. The longest
root that contains the file wins; without one the file gets no origin
(warning `file.no_root`). An absolute path is never recorded.

```toml
[roots]
notes = "~/path/to/synced/notes"   # absolute or ~/ (HOME)
[files]
max_bytes = 104857600              # per file, 1 B to 1 GiB; 100 MiB by default
```

Roots are added by hand. A root that is not configured on a machine makes
`file path --origin` fail with `file.root_unknown`; `file check` reports
`root_unknown`. Paths are compared as given after resolving symlinks:
there is no case folding on case-insensitive file systems and no Unicode
normalization, so a name stored in NFD on one machine may not resolve on
another. The media type comes from a built-in table of magic bytes and
extensions (`--media-type` overrides it), so it is the same on every
machine.

In the [remote mode](server.md#remote-mode-of-graph) the roots of the
machine running `graph` count: `file add` records the origin there,
`file path --origin` and `file check` resolve origins there, and `file
path` answers a read-only copy downloaded to this machine (the
[file cache](#file-cache)); `[files] max_bytes` of the server limits what
it stores.

## File cache

When the stored copy has no path on this machine, `file path` answers a
copy in the file cache `<state>/files/<graph id>/<hash>` (`<state>` is
`$XDG_STATE_HOME/graph_cli`): in the remote mode, where the copy is
downloaded from the server, and for a graph whose stored files are in an
[object store](#object-storage-optional).

- A copy is read only and is checked against its hash before
  it is answered; a copy that changed is fetched again. Read it, never edit
  it.
- Two commands may fetch the same file at once: each writes its own
  partial file and only bytes with the right hash become the copy.
- Every `file path` that answers a copy marks it as used. A later `file
  path` removes copies nobody used for 24 hours, so the cache does not
  keep files on the machine for long; the next `file path` fetches them
  again.
- The cache is never authoritative and can be deleted at any time.

## Where stored files live

By default the bytes of a graph's stored files are in its directory in the
data root, next to the rest of the graph; nothing needs to be configured.
An installation can keep them in an S3-compatible object store instead,
graph by graph.

### Object storage (optional)

- **What moves:** only the bytes of stored files. The transaction log, the
  index, the audit trail, the manifests, tokens and policies stay in the
  data root, which keeps every rule of [Storage](storage.md).
- **Privacy:** an object is named by the graph's id and the file's hash
  (`<prefix>graphs/<graph id>/blobs/sha256/<xx>/<hash>`) and carries no
  metadata: the store sees bytes, sizes and hashes, never a file name, an
  origin, a media type or a title. One more object,
  `<prefix>graph_cli_store.json`, identifies the store; every graph in it
  names that id, so a graph pointed at another bucket fails ("this graph's
  files are not in that bucket") instead of looking empty. Uploads are
  signed with the file's SHA-256, which the store checks, and downloads are
  checked against the hash while they stream; plain `http://` (allowed only
  where configured) still lets the network read the bytes.
- **Permissions:** the key needs `GetObject`, `PutObject`, `DeleteObject`
  and `ListBucket` on its bucket and nothing else (no bucket creation,
  versioning or policies). Without `ListBucket` a store answers 403 instead
  of 404 for an absent object, and a missing file could not be told from a
  refused one.
- **Durability:** an upload the store answered with success counts as
  durable. A self-hosted store must write data and metadata to disk before
  it answers (Garage, which the integration tests run against:
  `data_fsync = true` and `metadata_fsync = true`).
- **Setting it up:** `[files] store = "s3"`, `[files.s3]` and the
  credentials in the environment ([Configuration](configuration.md#object-storage)),
  then `graph-admin files store check`, which says in words what is wrong.
  From then on `graph-admin graph create` puts the files of new graphs in
  the store (an audited configuration change in the same command). Existing
  graphs stay where they are; changing the setting never moves one.
- **Moving a graph:** `graph-admin files move <graph> --to s3` (or `--to
  local`, `--all` for every graph) copies the files while writes go on,
  then switches the graph under its lock, audited as
  `maint.graph_configure` ([Moving stored files](cli_graph_admin.md#moving-stored-files)).
  It never deletes: `graph-admin files gc --graph <graph>` removes the old
  copies afterwards, each only after it read the new copy in full and found
  the same hash.
- **Nothing else changes** for agents and `graph`: the same commands and
  answers, locally and remotely. `file path` answers a copy in the
  [file cache](#file-cache); the server and the live view stream the bytes
  and keep no copy.
- **Writes to file entities need the store.** Every revision of a
  `files:File` names its bytes and its commit confirms them in the store,
  so while the store cannot be used even a title change of a file fails
  (`backend.unavailable`). Reads, and writes that touch no file, keep
  working.
- **When it fails:** a store that cannot be reached, refuses the
  credentials or the request time, has no such bucket or holds another
  store's marker answers `backend.unavailable` with what to fix; a store
  that refuses an upload (quota, write permission; some stores answer
  wrong credentials this way too) answers `files.store_refused`; an object
  missing from the store is `backend.corrupt`, like a missing local file.
  Messages name the operation, the endpoint host, the HTTP status and the
  S3 error code, never a credential.
- `files gc` removes an unreferenced object only when it is older than 24
  hours by the store's clock: a file being added is unreferenced between
  its upload and its commit.
- **Older versions** refuse a graph whose files are in an object store
  entirely (its manifest is format 2, [Storage](storage.md#manifest)).
  Before going back to an older version, move such graphs to the data root.

## Privacy

Audit events name stored files by hash and size only: never a file name,
an origin path or content. Metadata records are graph data like any other:
what a graph imports is what it records about its files. A kind that would
record personal data (the location in a photo, later) is a module of its own,
imported on purpose, never part of a basic one. Reading a file is not audited (reads never
are; the opt-in [access log](audit.md#access-log) counts only bytes).
File bytes, names and origins are instance data like any attribute value:
names and origins live in the graph's transaction log, the bytes in its
`blobs/` or the [object store](#object-storage-optional). How they are
stored: [Storage](storage.md).

## Backups

Backups of the data root contain the stored files, so they may contain
personal data (scans of documents). After a restore, run
`graph-admin audit verify` (a missing stored file is an error there) and
`graph-admin files verify --graph <graph> --rehash` (damaged bytes). Keep
the data root on a local file system, as for the rest of the store; only
the bytes of stored files may live elsewhere, in an object store.

With an object store, the stored files of the graphs in it are not in the
data root's backup:

- Back up the data root first and the bucket afterwards (the objects below
  the configured prefix, the marker included). A bucket newer than the data
  root is harmless: its extra objects are orphans that `files gc` removes
  after the grace period. A bucket older than the data root lacks the files
  added in between.
- After a restore, run `graph-admin audit verify`, then `graph-admin files
  verify --graph <graph> --rehash` for each graph; a missing object is a
  `blob_missing` error naming its hash. Recover missing objects from an
  older bucket backup before relying on the graph; `files gc` is safe
  meanwhile (it deletes only unreferenced objects older than the grace
  period).
- A copy of the bucket keeps the marker and so the store's id: to move to
  another provider, copy the bucket with its marker and change the
  endpoint, bucket and credentials. Nothing in the data root changes.
