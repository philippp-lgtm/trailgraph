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

Then `graph get`, `history`, `find`, `lookup`, `search`, `neighbors`,
`query`, `file path` and `file check` tell every running view what they
returned, as Unix datagrams to sockets in the state directory. Nothing
is stored, a read never fails or waits because of it, and without a
running view nothing is sent. A signal names the command, the reader
(the agent if the harness names one, else the principal, as for writes)
and its session, the ids of the entities returned and the ids of the
edges walked: those `neighbors` followed and the relationships in
`query` rows. A `query` that returns only values (`RETURN n.name`)
lights nothing up. The signal carries no labels, values, arguments or
query text. A read of several hundred entities is cut: entities come
before edges, the rest is dropped.

Behind a [server](server.md) the reads happen in the server, so the
server's `config.toml` needs `signal_reads` and the view must run next to
it, with the same state directory (in the container: `docker compose
exec`).

A Unix socket path is limited to about 100 bytes; with a
very long state directory the view starts without reads and says so.

### Trail

The view numbers what it sees as steps: one per read command and one per
written entity, each with time, command, reader and session, the entity
ids and, for reads, the edge ids. It keeps the last 500 steps in memory,
so the steps of one session show where an agent went; they are gone
when the view stops. The page gets each step live and can fetch the
recent ones at `/trail` with the same token, for example after a
reconnect.

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
