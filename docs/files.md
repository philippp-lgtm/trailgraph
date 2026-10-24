# Files

A graph whose ontology imports the standard module `files` (`imports:
[{module: files, version: "^1.0"}]`) stores files: scans, documents,
saved web pages, any media type. graph_cli stores, addresses and
references them; it never reads what is in them. An agent reads a stored
file itself and writes what it finds through the normal write commands.
The commands are `graph file add|path|check` (see [`graph`](cli_graph.md))
and `graph-admin files verify|gc` (see [`graph-admin`](cli_graph_admin.md)).

## Model

- `files:File` holds `title` (the main label), `content` (type `blob`:
  `sha256:<hex>` of the bytes, unique among active files, immutable),
  `media_type`, `file_name`, `origin` (type `local_path`), `source_url` and
  `retrieved_at`. A graph module may subclass it (`is_a: files:File`) to
  give files domain meaning.
- `files:from_file` points from any entity to a file, with the qualifiers
  `page` and `last_page` (from 1) and `locator` (a section, region or task
  number).
- A file's content never changes. A new version is a new `file add`
  followed by `supersede`. Nothing that a revision ever referenced is
  removed; `graph-admin files gc` removes only files no transaction names.
- Web sources need no stored copy: an `iri` attribute plus a `date` for
  the retrieval. A saved copy is a stored file with `source_url` and
  `retrieved_at`. graph_cli fetches nothing from the network.

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
path` answers a read-only copy downloaded to this machine; `[files]
max_bytes` of the server limits what it stores.

## Privacy

Audit events name stored files by hash and size only: never a file name,
an origin path or content. Reading a file is not audited (reads never
are; the opt-in [access log](audit.md#access-log) counts only bytes).
File bytes, names and origins are instance data like any attribute value:
they live in the graph's `blobs/` and transaction log. How they are stored
on disk: [Storage](storage.md).

## Backups

Backups of the data root contain the stored files, so they may contain
personal data (scans of documents). After a restore, run
`graph-admin audit verify` (a missing stored file is an error there) and
`graph-admin files verify --graph <graph> --rehash` (damaged bytes). Keep
the data root on a local file system, as for the rest of the store.
