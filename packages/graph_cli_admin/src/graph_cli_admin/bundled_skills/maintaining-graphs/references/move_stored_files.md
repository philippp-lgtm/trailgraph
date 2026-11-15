# Moving a graph's stored files to an object store (and back)

The bytes of stored files live in the data root by default. An installation can keep them in an
S3-compatible object store instead, graph by graph (`docs/files.md`, "Object storage
(optional)"). Only the bytes move: the transaction log, the audit trail and everything else
stay in the data root. Agents see no difference. Only a maintainer moves graphs; ask the user
before starting, and run the steps for one graph at a time.

## Before the move

1. The store must be configured where the data lives (`[files.s3]` and the credentials in the
   environment, `docs/configuration.md`). Check it:

   ```sh
   graph-admin files store check
   ```

   Every check must be `ok`; a `warning` for the marker only means no graph uses the bucket
   yet. On `admin.store_check_failed` read the messages, tell the user what is wrong and stop.
2. Make sure a backup of the data root exists and is checked. graph-admin has no backup
   command: ask the user.
3. Preview the move; it copies nothing and counts the files and bytes:

   ```sh
   graph-admin files move <graph> --to s3 --dry-run
   ```

## The move

```sh
graph-admin files move <graph> --to s3 --reason "<why>"
graph-admin audit verify
graph-admin files verify --graph <graph> --rehash
```

The move copies every file while writes go on, then switches the graph under its lock (one
audited `maint.graph_configure` event). `audit verify` must report no errors. `files verify`
lists the old copies in the data root as `blob_misplaced` (info); any error stops here. In the
remote mode the same command sends a dry run, then short steps, so each request fits the
timeout.

## After the move

1. Ask the user to back up the bucket (with the store's own tools): the data root first, the
   bucket after it, so the bucket backup holds every file the data root backup names.
2. Remove the old copies. gc deletes a copy only after it read the new one in full and found
   the same hash; ask the user before running it for real:

   ```sh
   graph-admin files gc --graph <graph> --dry-run
   graph-admin files gc --graph <graph> --reason "<why>"
   ```

## Back to the data root

The same steps with `--to local`:

```sh
graph-admin files move <graph> --to local --dry-run
graph-admin files move <graph> --to local --reason "<why>"
graph-admin audit verify
graph-admin files verify --graph <graph> --rehash
```

Then `files gc` as above removes the copies in the bucket. The bucket's marker stays.

## Refusals

| Code | What to do |
| --- | --- |
| `config.invalid` | no object store is configured, or a setting is wrong; tell the user |
| `backend.unavailable` | the store cannot be reached, refuses the credentials, or holds another store's marker; `graph-admin files store check` says which |
| `files.store_refused` | the store refused an upload: quota or write permission; the graph stays where it was, tell the user; a later run reuses the copies |
| `admin.migration_incomplete` | finish the interrupted release first (`release_with_a_migration.md`) |
| `backend.corrupt` | a file the graph names is in neither location; `graph-admin files verify --graph <graph>` names it; stop and tell the user |

An interrupted move leaves the graph where it was with every file; run the same move again.
`audit verify` reports an interrupted switch as info until then.
