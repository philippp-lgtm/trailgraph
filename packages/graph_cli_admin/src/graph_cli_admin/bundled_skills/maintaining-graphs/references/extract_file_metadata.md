# Metadata records of stored files

A graph that imports the standard module `files` (1.1 or later) can hold derived **metadata records**
of its stored files: size and detected media type (always, with `files`), and per kind the page count
of a PDF (`files_pdf`), the pixel size of an image (`files_image`), the shape of a CSV or TSV
(`files_table`) and the tables of a SQLite database (`files_sqlite`). Importing a kind module enables
its extractor for that graph. Records are written by the program, never by hand, and agents cannot
change them. Only a maintainer runs the backfill.

## When to run `files extract`

- after an ontology release that imports a kind module (`files_pdf`, ...) or moves `files` to 1.1:
  the files stored so far have no records of the new kind;
- after upgrading `graph` and `graph-admin`: an extractor with a new version updates its records, and
  `--dry-run` lists records that would go from `ok` to `failed` or `not_applicable` (regressions);
- after raising `[extract] timeout_seconds` or `max_memory_mib` in `config.toml`: `--retry-failed` runs
  the results that failed on a limit again.

`graph file add` extracts the file it stores on its own; the command is for what was stored before.

## Steps

1. `graph-admin files extractors`: what this program can extract, with the lowest module version each
   needs. A graph that imports a module below that version gets `module_outdated` in the report; release
   the ontology with the module's current version.
2. Preview: `graph-admin files extract --graph <graph> --dry-run`. It parses every file and writes
   nothing. Read `extractors` (counts per extractor), `regressions`, `problems`, `transient` and
   `newer_versions`.
3. Run it: `graph-admin files extract --graph <graph>`. One tx per file and extractor; a second run
   writes nothing. `--file <id>` and `--extractor <name>` (both repeatable) narrow it.
4. `graph-admin files verify --graph <graph>` shows under `metadata` which records are still behind their
   extractor, and `graph-admin audit verify` checks the trail.

Needs the maintain permission on the graph (`maint.reindex`); a project admin token does not reach it
over the admin API. The run reads every file once: for a large graph start with `--extractor <name>`.

## What the report means

| Key | Meaning and what to do |
| --- | --- |
| `failed` with a `problem` | the bytes cannot be read as their type (`pdf.unreadable`, `pdf.encrypted`, `table.no_dialect`, `sqlite.unreadable`, `limit.time`, `limit.memory`): a recorded result, nothing to repair; AES-encrypted PDFs are `pdf.encrypted` by design |
| `transient` | not recorded (the process was killed, the stored copy is unreadable): run again; `files verify --rehash` if it repeats |
| `newer_versions` | records of a newer program than this one are left alone: update this program |
| `problems` with `blocked_by_references` | a relation of the graph with `on_target_inactive: refuse` still points at a part the new version no longer produces: the record waits at its old version until that edge is gone |
| `module_outdated` | the graph imports the kind module below the version the extractor needs |
| an error that ends the run | `details.progress` says how far it got; what was written stays and a rerun continues |
