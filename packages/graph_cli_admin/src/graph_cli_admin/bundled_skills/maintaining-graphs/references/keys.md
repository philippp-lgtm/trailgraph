# Keys of sealed graphs

Every graph `graph create` makes is sealed: its values are encrypted under keys
in its key store, which the key-encryption key (KEK) wraps. Without both the
data is lost; with an old copy of the key store, purged data is readable again.

## Back up the key stores

`graph-admin keys backup --to <dir>` writes a copy of every key store to
`<dir>/<graph id>.sqlite` (`--graph <graph>`: one graph). Use a new directory
named by date; an existing file is never replaced (`usage.invalid_argument`).
It runs only where the data lives (`admin.remote_mode` in the remote mode:
tell the user to run it there). Tell the user to keep the copies apart from
the data backups and only for a short time, and the KEK in a secret store.

## Check and clean up

1. `graph-admin keys verify --graph <graph>`. Errors (`integrity.keys.missing`:
   restore the key store from a backup; `integrity.keys.shredded_unrecorded`)
   need the user. `integrity.keys.kek_not_current` means a rotation is
   unfinished (below); `integrity.keys.orphan` is harmless.
2. `graph-admin keys gc --graph <graph> --dry-run`, then without `--dry-run`
   and with `--reason`, deletes orphan keys older than one hour. Run
   `graph-admin files gc --graph <graph>` first when the graph has orphan files:
   their keys stay until the files are gone.

## Rotate the key-encryption key

Only when the user asks (yearly, a suspected leak, or after a purge so that old
key store copies become worthless). The user provides the new KEK file and the
configuration; never create, read or print a KEK yourself.

1. The user configures the new KEK as `[keys] kek_file` (or
   `GRAPH_CLI_KEK_FILE`) and the old one as `[keys] previous_kek_file` for every
   program that opens the graphs, and restarts the server.
2. `graph-admin keys rotate-kek --dry-run`: `rewrapped` per graph. A
   `config.invalid` naming `previous_kek_file` means the old KEK is not
   configured; tell the user.
3. `graph-admin keys rotate-kek --reason "<ticket>"`. If it stops, run it again.
4. `graph-admin keys verify --graph <graph>` for each graph: no
   `integrity.keys.kek_not_current`. Then tell the user to remove
   `previous_kek_file`, destroy the old KEK and its copies, and take a new key
   store backup.
