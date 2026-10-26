# Live view

`graph-admin view` prints an address on stderr and opens it: a side bar
with the graphs of the data root and, for the graph picked there, the
whole graph as dots and lines, with labels, and a list of recent
activity. `--graph <graph>` opens the page on that graph; without it the
page opens on none. It serves until Ctrl+C (`--port`, `--host`,
`--no-open`, `--token-file`).

## Switching graphs

The side bar lists every graph you may read (`read.get`), archived ones
marked, with name and title; a graph you may not read is left out, and
listing it is not audited as a denial. Click a graph to show it. The
address names the shown graph as `graph=<name>`, so a reload, a bookmark
or Back and Forward keep it, and an address with `graph=` opens on that
graph directly (a name or an id). The list refreshes when the window gets
the focus and every minute, so a graph created while the view runs shows
up without a restart. The toggle at the top folds the side bar to a
narrow strip and back; the browser remembers the choice.

The view opens a graph when the page first asks for it, not at start,
and keeps up to eight open; opening a ninth closes the one used least
recently, and a page showing that one reconnects and opens it again.

## Activity

Entities light up briefly when something touches them: green
when created (also by `graph file add`), blue when changed, and in the
color of the reading session when read. Writes show up by themselves,
from any process, once committed. Reads show up only when `config.toml`
has

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
edges walked: those `neighbors` followed, and for `query` the entities
and relationships in the rows and then every node and relationship its
`MATCH` rows went through, so a query that returns only values or counts
(`RETURN n.name`, `count(n)`) lights up what it read too. That covers
all matched rows, not only the page shown; what `WHERE` filtered out and
the patterns inside a `WHERE` predicate do not count. The signal carries
no labels, values, arguments or query text. A read of several hundred
entities and edges is cut: they take turns, so a cut read still draws a
walk, and the rest is dropped.

On the page a read is a walk: a pulse runs along each walked edge, in
the order the read names them, from the end it reached first; the
entities and edges it touched glow and fade within a second or two, and
reads in quick succession add up, so a busy agent flickers through the
graph while the rest of it steps back. Each session gets its own
color. With reduced motion the walk lights up at once, without pulses.

Behind a [server](server.md) the reads happen in the server, so the
server's `config.toml` needs `signal_reads` and the view must run next to
it, with the same state directory (in the container: `docker compose
exec`).

The view binds its socket in `<state>/graph_cli/activity/`. A Unix socket
path is limited to about 100 bytes, and some shared mounts refuse socket
files; then the view starts without reads and says why. In a container
whose state is such a mount, put that one directory on a `tmpfs`.

### Trail

The view numbers what it sees in a graph as steps: one per read command
and one per written entity, each with time, command, reader and session,
the entity ids and, for reads, the edge ids. It keeps the last 500 steps
per open graph in memory, so the steps of one session show where an
agent went; they start when the graph is opened and are gone when it is
closed or the view stops. The page gets the steps of the shown graph
live, and only those, and can fetch the recent ones at
`/trail?graph=<graph>` with the same token, for example after a
reconnect.

## Access

The server listens on `127.0.0.1` and answers only requests that
carry the random token in the printed address, so keep the address to
yourself. `--host` binds another address, for a view inside a container
whose port is published to `127.0.0.1` on the host; requests must then still
name `127.0.0.1` or `localhost` (any port or none) and carry the token. The printed
address names the port inside the container: open the published port with
the same token. The graph itself carries labels, types, aliases and record
status only.

`--token-file <file>` reads the token from a file instead (surrounding
whitespace stripped, at least 32 characters); the view refuses to start
with a file it cannot read, an empty one or a shorter token, and the
printed address then leaves the token out. `GET /health` answers `ok`
without token, for probes; it reads no graph. Bound with `--host` to
another address it answers whatever `Host` a request names (a probe
names the pod's address); on `127.0.0.1` the `Host` must still name
`127.0.0.1` or `localhost` with the port, as for every other request.

## Behind a reverse proxy

To reach the view from elsewhere, run it next to the
[server](server.md) and put an authenticating reverse proxy in front.
On a container platform the view is a second container in the server's
pod (a sidecar) with the same data root and state directory, so writes
and, with `signal_reads` in the server's `config.toml`, reads show up
(see [Activity](#activity)):

```sh
graph-admin view --host 0.0.0.0 --port <port> --no-open --token-file <file>
```

The token file comes from a secret that only the view and the proxy
see. The proxy

- authenticates the user and forwards only `GET` requests, to the routes
  `/` (the page), `/graphs`, `/graph`, `/entity`, `/blob`, `/events` and
  `/trail`, with their query parameters (`graph`, `id`);
- sends `Host: localhost` or `127.0.0.1` (with any port or none), not
  the public name, else the view answers 403;
- adds the token as the query parameter `t`, replacing any `t` the
  browser sent (the page then sends an empty one, which the view
  ignores), so the token never reaches the browser;
- may serve the view below a path prefix (`/<prefix>/` to the view's
  `/`): the page asks for every route by a relative URL. Redirect
  `/<prefix>` to `/<prefix>/`, or those URLs miss the prefix;
- keeps `/events` streaming: no response buffering, and an idle timeout
  above the 15 seconds between heartbeats.

Point liveness and readiness probes at `/health` on the view's port.
The view's page and answers are not encrypted; keep its port inside the
pod or cluster network and let the proxy terminate TLS.

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
