# Server

`graph-server` serves the command surface of `graph` over HTTP. With it,
the server process owns the data root: agents in containers, other
machines, web apps and the local `graph` go through one API, and every
request is authorized for the bearer token it carries.

## Running it

```sh
graph-server serve                      # http://127.0.0.1:8470
graph-server serve --port 0             # any free port; the address is printed
graph-server serve --host 0.0.0.0 --allow-plain-http   # behind a TLS proxy only
```

It reads the same configuration and data root as `graph` and refuses to
start when its own `config.toml` says `[runtime] mode = "remote"`. It binds
`127.0.0.1` unless `--host` names another address, which needs
`--allow-plain-http`: the API speaks plain HTTP, so TLS belongs to a
reverse proxy in front of it. `--threads` (default 8) is how many requests
run at once; writers to one graph wait for each other on the graph lock,
as processes do. The address goes to stderr, then one line per request:
time, method, path, status, duration and the token's name, never a token,
a body, a query string or query text. `SIGTERM` or Ctrl+C stops it.

## Tokens and access

A maintainer creates one token per client (`graph-admin token create NAME`,
see [`graph-admin`](cli_graph_admin.md)); the token is shown once, the data
root keeps only its hash. A request carries it as `Authorization: Bearer
<token>`. Without a valid token a request reaches nothing and is not
audited; with one, it acts as the verified principal `token:<name>` through
the entrypoint `server`.

The installed policy must grant the server: a grant with `entrypoint:
server` (or `[app, server]`), or one naming `principal: {id:
token:<name>}` (see [Access control](access_control.md#policy-layers)).
The built-in policy grants the server nothing, and `token create` warns
when the policy grants the new token nothing. Agent, session, device and
project are asserted by the client in each request; a token created with
`--project` verifies its project instead, so project scopes become a
boundary for that token. The server never runs a maintenance action.

## API

Every path starts with `/v1/`; every answer is the `graph` envelope, and
the HTTP status follows its error category (200 ok, 400 usage, 401 no
valid token, 403 denied, 404 not found, 409 conflict, 422 refused or
invalid, 503 unavailable, 500 internal). `Graph-Cli-Exit-Code` carries the
exit code `graph` would have.

| Endpoint | Purpose |
| --- | --- |
| `GET /v1/health` | `{ok, version, api}`; no token needed |
| `GET /v1/inventory` | the command inventory (`graph describe commands`) |
| `GET /v1/openapi.json` | an OpenAPI 3.1 document generated from the inventory |
| `POST /v1/commands/{path}` | run a command; `{path}` is the command path with `/` (`describe/types`) |
| `PUT /v1/uploads` | stage bytes for a file parameter; answers `{upload, size}` |
| `GET /v1/graphs/{graph}/files/{ref}` | the stored file of an entity, checked like `file path` |
| `POST /v1/graphs/{graph}/skill` | the graph's published skill, rendered for installing |

A command request names the parameters by their inventory names:

```json
{"args": {"type": "<type>", "--set": ["<attr>=<value>"], "--dry-run": true},
 "actor": {"agent": {"id": "<value>", "type": "<value>"}, "project": "example_project"},
 "pointer": {"graphs": ["example_graph"], "default": "example_graph"}}
```

A file parameter (`apply`, `query --file`, `file add`) takes an upload:
`{"upload": "sha256:<hex>", "name": "<file>"}`, for `file add` also the
`origin` the client computed. An upload is kept for its token only and
removed after the command that used it, or after an hour. `?render=text`
answers `{envelope, stdout, stderr}`, the text `graph` would print.

Browsers may call the API from the origins listed in `[server]
cors_origins` ([Configuration](configuration.md#configtoml)); a browser
never sends the token on its own, so a page can only use a token it was
given.

## Remote mode of `graph`

A machine whose `config.toml` says `[runtime] mode = "remote"` (see
[Configuration](configuration.md#remote-mode)) sends every command to the
server. `graph` still parses the command line itself, so usage errors and
`--help` need no server, and prints the server's answer as its own: the
same envelope, text and exit code as locally. Its own warnings come
first; large JSON answers go to a file on this machine. Agent, session,
device and the pointer's project and graphs are sent with each request.

- A write always carries an idempotency key: the given
  `--idempotency-key`, else a generated one. If the connection breaks
  after a write was sent, `remote.outcome_unknown` names the key and the
  command to run again with it, which is safe. Reads are retried once;
  writes never.
- `remote.unreachable`: no connection; `remote.unauthorized`: no token, or
  the server refused it; `remote.protocol`: the answer is not the API's (a
  proxy page); `remote.version_differs` (warning): the server runs another
  version.
- `describe commands` runs locally.
- `apply FILE|-` and `query -|--file` read their input here and upload it.
- `file add PATH` uploads the file with its origin under this machine's
  `[roots]`; the server stores it.
- `file path` downloads the stored file into
  `<state>/files/<graph id>/<hash>` on this machine (read only, kept as a
  cache) and answers that path; `--origin` and `file check` look at the
  origins on this machine, as locally.
- `skills emit|install|check` run here; a graph skill comes from the
  server, rendered for the graph's current ontology, and must be allowed by
  this project's pointer.
