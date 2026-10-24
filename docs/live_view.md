# Live view

`graph-admin view --graph <graph>` prints an address on stderr and opens
it: the whole graph as dots and lines, with labels, and a list of recent
activity. It serves until Ctrl+C (`--port`, `--host`, `--no-open`).

## Activity

Entities light up briefly when something touches them: green
when created (also by `graph file add`), blue when changed, yellow when
read. Writes show up by themselves, from any process, once committed.
Reads show up only when `config.toml` has

```toml
[activity]
signal_reads = true
```

Then `graph get`, `find`, `lookup`, `neighbors`, `file path` and
`file check` send the ids they return to every running view, as Unix
datagrams to sockets in the state directory. Nothing is stored, a read
never fails or waits because of it, and without a running view nothing
is sent. The signal carries ids only, no labels, values or query text.
A Unix socket path is limited to about 100 bytes; with a
very long state directory the view starts without reads and says so.

## Access

The server listens on `127.0.0.1` and answers only requests that
carry the random token in the printed address, so keep the address to
yourself. `--host` binds another address, for a view inside a container
whose port is published to `127.0.0.1` on the host; requests must then still
name `127.0.0.1` or `localhost` (any port) and carry the token. The graph itself carries labels, types, aliases and record
status only.

## Using the view

Clicking a node loads that entity on demand: its attributes
(hidden labels left out), edges in both directions with their
qualifiers, and every revision with the agent, principal and session
that wrote it. Clicking an edge shows its relation, ends and qualifiers
and the history of its source entity. For a stored file (`files:File`)
the panel offers a preview of PDFs and images in the page and the
original as a download; reading the bytes needs `read.get` on the file's
type, as for `file path`. Click an activity entry to jump to its entity;
Escape closes the panel. The search box dims everything whose label,
alias or type does not contain the text (Enter opens the first match);
"hide entities that do not match" removes those instead, and one
checkbox per type hides whole types. Attribute values are not searched.
"scale nodes by number of connections" enlarges nodes in proportion to
their edge count, relative to the most connected visible node.
