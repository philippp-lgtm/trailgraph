# Live view

`graph-admin view` prints an address on stderr and opens it: a side bar
with the graphs of the data root and, for the graph picked there, the
whole graph as dots and lines, with labels, and a list of recent
activity. Besides the graph itself, the page shows the graph's ontology,
its types as a graph and its named functions (see
[Model, type graph and functions](#model-type-graph-and-functions)). `--graph <graph>` opens the page on that graph; without it the
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
and keeps up to eight open. Beyond that it closes the graph used least
recently that no page is showing and that `--graph` did not name; a
graph a page shows stays open, so with more graphs shown at once more
stay open. Warnings about a graph opened later (for example an ontology
pin the program cannot read) print on stderr once.

## Model, type graph and functions

Below the picked graph the side bar lists its views; while the side bar
is folded, a bar at the top of the page lists them. The address names
the view as `tab=` (`model`, `types` or `functions`; none for the graph)
and what it shows as `item=`, so a reload, a bookmark or Back and Forward
keep both. The three views read what `graph describe` prints, for the
graph's current ontology; nothing in them streams, a reload shows a
change.

- **Graph**: the graph itself, as described in the rest of this page.
- **Model**: the modules with version, summary and how many types,
  relations, functions and active entities each holds, built-in modules
  apart. A module lists its types, the relations that stay inside it,
  those that link it to other modules, its rules and functions. A type
  shows its fields with their flags, its lifecycles with states and
  transitions, its relations in both directions, its rules, ancestors
  and subtypes, and how many active entities it and its subtypes have.
  "Show in the graph" switches to the graph with every other type
  hidden.
- **Type graph**: every type as a dot in the color of its module,
  hollow when abstract and, by default, sized by its active entities;
  every relation as an arrow from the type it starts at to the type it
  points to (one arrow per pair of types, named by its relations), and a
  dashed arrow from a type to the type it extends. Relations open to any
  type have no end to draw; the side bar names them. Pointing at a type
  lights up its relations and neighbours; clicking it shows its
  description next to the graph, clicking an arrow its relations. The
  side bar hides modules, inheritance and sizes, shows the relation
  names and searches types; drag to pan or move a type, wheel to zoom,
  double-click to fit.
- **Functions**: the named functions by module, each with its summary,
  when to use it, the `graph run` command, parameters, returned columns,
  example questions, the types and relations it reads, and its query
  text.

The model needs `read.describe` on the graph, as `graph describe` does;
a refusal is audited like one of the graph (see [Access](#access)). The
functions need `read.function`: without it the view lists none and
records nothing, as `graph describe functions` does.

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
`query`, `run`, `file path` and `file check` tell every running view what they
returned, as Unix datagrams to sockets in the state directory. Nothing
is stored, a read never fails or waits because of it, and without a
running view nothing is sent. A signal names the command, the reader
(the agent if the harness names one, else the principal, as for writes)
and its session, for `run` the name of the function (never a parameter value), the ids of the entities returned and the ids of the
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
status only; the model carries no attribute values, only how many active
entities each type has.

Each request for a graph checks `read.get` on it again, so a read
revoked in the policy takes effect with the next request; an open
activity stream was checked when it connected and runs on until it
reconnects. A refused request is audited like any denial, but at most
once per graph, action and minute: repeats in that time are refused the same way
without an event, so a page or client that keeps retrying cannot flood
the audit log. After a refusal or an unknown graph the page stops
reconnecting and says why.

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
  `/` (the page), `/graphs`, `/graph`, `/entity`, `/blob`, `/model`,
  `/functions`, `/events` and `/trail`, with their query parameters
  (`graph`, `id`);
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

A proxy that wraps the page in its own frame (a bar above it, a shared
light or dark mode) can rely on two things. The page follows
`data-theme="dark"` or `data-theme="light"` on its `<html>` element and
switches when the attribute changes; without it, the page follows the
device setting. And it fills the height its `<body>` leaves, so a
`padding-top` on `body` for a bar keeps everything in view without
scrolling. A page may also call the global `readColors()` after changing
the theme some other way; it applies the theme and re-reads the colors
the graph is drawn with.

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

On a narrow screen (720 pixels or less, such as a phone) the list of
graphs starts folded and opens over the graph; picking a graph or Escape
folds it again. The panel becomes a sheet at the bottom that shows the
graph's name and counts; "Details" opens it, and clicking a node opens
it with that entity.
