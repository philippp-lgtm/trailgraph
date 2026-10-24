# Server

`graph-server` serves the command surface of `graph` over HTTP, and on
request the maintenance commands of `graph-admin` too (the
[admin API](#admin-api)). With it, the server process owns the data root:
agents in containers, other machines, web apps and the local `graph` go
through one API, and every request is authorized for the bearer token it
carries.

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
boundary for that token. An app token never runs a maintenance action.

Every token has a scope: `app` (the default) reaches the `graph` commands,
`admin` (`token create NAME --scope admin`) only the admin API. Either may
upload. A token used on the other surface is refused with 403 and
`remote.unauthorized`, before any command runs.

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
| `GET /v1/dist/manifest`, `GET /v1/dist/{file}` | the programs the server hands out ([Self update](#self-update)); no token needed, except for the `graph-admin` wheel |

A command request names the parameters by their inventory names:

```json
{"args": {"type": "<type>", "--set": ["<attr>=<value>"], "--dry-run": true},
 "actor": {"agent": {"id": "<value>", "type": "<value>"}, "project": "example_project"},
 "pointer": {"graphs": ["example_graph"], "default": "example_graph"}}
```

A file parameter (`apply`, `query --file`, `file add`) takes an upload:
`{"upload": "sha256:<hex>", "name": "<file>"}`, for `file add` also the
`origin` the client computed. An upload is kept for its token only and
removed after the command that used it, or after an hour; a token's
pending uploads hold at most four times `[files] max_bytes`. A body larger
than any route takes is refused before it is read. `-` (stdin) does not
exist on the server: upload the input instead. Over the API `file path`
answers `path: null`; the bytes come from the file endpoint. `?render=text`
answers `{envelope, stdout, stderr}`, the text `graph` would print.

Browsers may call the API from the origins listed in `[server]
cors_origins` ([Configuration](configuration.md#configtoml)); a browser
never sends the token on its own, so a page can only use a token it was
given.

## Builds

The server is the origin: the program it runs is the one clients should
run. Every installed build of `graph` and `graph-admin` carries a build id,
a hash over the sources of the distribution and of the distributions it
depends on, so two installs of the same source tree have the same id
whatever their version says. The client names its program and build in
`Graph-Cli-Build: <program> sha256:<hex>`; every answer carries the
server's build of that program in the same header.

After the token, before any route, the server compares the two: another
build of the same program is refused with `remote.client_outdated` (422,
exit code 1); the message names both builds. With `[server] client_builds
= "warn"` the request runs, and the client warns `remote.version_differs`
instead (for a rolling upgrade). Nothing is compared when either side has
no build id: a web app or `curl` sends none, and neither does a program
run from a checkout (`uv run graph`), which then only gets the warning
when the version string differs. `/v1/health` and `/v1/dist/` are never
refused: they are how a client catches up (`graph self update`).

Installing a client from a checkout with a build id: run
`python3 scripts/build_id.py` before `uv tool install` and `python3
scripts/build_id.py --clean` after it. The image's build stage does the
same, so a client and a server built from the same tree match.

## Self update

The image keeps the wheels of `graph` and `graph-admin` it was built from,
with `constraints.txt`, the pinned versions of their third-party
requirements, in the directory `GRAPH_CLI_DIST_DIR` names
([Configuration](configuration.md#environment-variables)).
`GET /v1/dist/manifest` lists them:

```json
{"schema": "graph_cli.dist/1",
 "programs": {"graph": {"distribution": "graph_cli", "version": "<value>",
   "build": "sha256:<hex>", "requires": [], "access": "public",
   "file": "<file>", "hash": "sha256:<hex>", "size": 0}},
 "constraints": {"file": "constraints.txt", "hash": "sha256:<hex>", "size": 0}}
```

`GET /v1/dist/{file}` answers the bytes of a listed file. The `graph`
wheel and the constraints need no token (program code, no data), so a
machine without `graph` can install it:

```sh
uv tool install --constraints <endpoint>/v1/dist/constraints.txt <endpoint>/v1/dist/<file>
```

The `graph-admin` wheel needs a token with the admin scope (`access:
"admin"`); it requires the `graph` wheel. A wheel of another build than the
server runs is not handed out (the server names it on stderr at start), so
a client never installs a build the server then refuses. `[server] dist =
false` turns `/v1/dist/` off (404).

`graph self update` brings a machine in step with the server:

1. In the remote mode, when the server runs another build of `graph`, it
   fetches the wheel and constraints into `<state>/dist/`, checks their
   hashes and runs `uv tool install --reinstall` into the uv tool
   environment `graph` runs from, so the launcher stays where it is. A
   `graph` not installed with `uv tool`, or without `uv` on `PATH`, gets
   `update.unsupported_install` with the command to run instead, and so
   does an endpoint that is plain HTTP to another host (a program fetched
   that way could have been changed on the way); a checkout (no build id)
   is left as it is. Locally this step is skipped.
2. The installed program, run as a new process, refreshes the agent skills
   in every scope that holds a skill of this program: the project's (at
   the pointer file found from `--dir`, default the working directory),
   adding the skills of the pointer's graphs that are not installed yet,
   and the user's ([Installing skills](skills.md#installing-skills)).
   Skills edited by hand stay and are reported unless `--force`.
3. It checks the skills it refreshed; the answer lists the program step
   (`current`, `updated` or `skipped`), each scope's skills and what
   changed.

`--check` changes nothing: it compares the builds and dry-runs the skill
refresh, and exits 0 only when nothing is outdated (else
`update.available`). `remote.client_outdated` and `skills.outdated` name
`graph self update` as their hint. `graph-admin self update` does step 1
for `graph-admin` with the admin token; it installs no skills.

## Skill freshness

A client sends the graph skills installed for its agent in
`Graph-Cli-Skills`: one item per installed copy, separated by `, `, each
`graph=<graph id>;hash=<slot hash>[;bundle=<bundle>;version=<version>];scope=<project|user>`
(the bundle and version of a generated skill's model). The server judges
the item of the graph a command selects against the graph's published
skill and pin: a write is refused with `skills.outdated` (422), a read
warns `skills.stale` ([Stale skills](skills.md#stale-skills)). A malformed
item is ignored, and a client without installed skills sends no header:
like the build check, this keeps agents consistent and is no access
control.

## Admin API

With `admin_api = true` under `[server]` in the server's `config.toml`
([Configuration](configuration.md#configtoml)), the server also runs the
`graph-admin` commands: creating graphs, validating, comparing and
releasing ontologies, checking graphs, publishing skills, policies, tokens,
audit and file maintenance. Off (the default), every `/v1/admin/` path is
404.

| Endpoint | Purpose |
| --- | --- |
| `GET /v1/admin/inventory` | the `graph-admin` inventory, as the API takes it |
| `GET /v1/admin/openapi.json` | an OpenAPI 3.1 document of the admin API |
| `POST /v1/admin/commands/{path}` | run a `graph-admin` command (`{"args": {...}, "actor": {...}}`) |

An admin request acts through the entrypoint `maint` with the token's
principal `token:<name>`, like a maintainer on the server: an installed
policy with `maintainers` must name the token (`{id: "token:<name>"}`;
`token create --scope admin` warns `admin.token_not_maintainer` when it
does not), one without `maintainers` lets every maintenance caller
maintain. Every admin request, reads such as `token list` or `policy show`
included, must first be allowed `maint.audit_read` on `installation`, so
an admin token that is no maintainer reaches nothing. Denials and changes
are audited exactly as locally, with the token and the asserted agent as
the actor.

An admin token bound to a project (`token create NAME --scope admin
--project P`) is a [project admin](access_control.md#project-admins): it
needs no `maintainers` entry, and passes instead when its project's scope
says `maintain` for at least one graph (`token create` warns otherwise).
It runs only the commands on one graph (`graph show|check|pull|push|
reindex`, `graph skill publish|render`, `ontology release|diff`, `files
verify`) and `graph list`, and sees only the graphs it maintains; every
other command is refused with `access.ceiling`.

The API never takes a path: `--data-root` is not part of it, a file
parameter (a policy) takes an upload, and a directory parameter (an
ontology, a skill) takes `{"files": {"<relative path>": "sha256:<hex>"},
"name": "<dir>"}`, every file an upload of the same token. Relative paths
have no empty, `.` or `..` parts; at most 1000 files. The server builds the
directory under the name's last part and removes it after the command;
answers name the client's arguments, never the server's copy. `graph skill
render` takes `--out` as an empty directory and answers `path: null` with
`rendered`, its files by name in base64. `init` and `view` run only where
the data lives.

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
  proxy page); `remote.client_outdated`: the server runs another build of
  `graph` ([Builds](#builds)); `remote.version_differs` (warning): the
  server runs another version or, in `warn` mode, another build.
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

## Remote mode of `graph-admin`

On the same machine `graph-admin` sends its commands to the admin API,
with the admin token from `GRAPH_CLI_ADMIN_TOKEN` or `[remote]
admin_token_file` ([Configuration](configuration.md#remote-mode)). It
parses the command line itself and prints the server's answer as its own,
as `graph` does.

- Ontology and skill directories and policy files are read here and
  uploaded: every regular file below a directory; a symlink or another
  file kind is refused.
- `graph skill render` checks `--out` here and writes the preview into it.
- `graph pull` writes the files the server answers into `DIR`, with the
  checks it makes locally; `graph push` uploads only the base file,
  `ontology/` and `skill/` of `DIR` and writes the new base into it.
- `describe commands` and `ontology validate` run here; `init`, `view` and
  `--data-root` are refused (`admin.remote_mode`).
- The server compares its build of `graph-admin` with this one, as for
  `graph` ([Builds](#builds)).
- Writes are never retried. A lost answer is `remote.outcome_unknown`:
  check the state (`graph show`, `token list`, `policy show`) before
  running the command again.

## Container

The repository's `Dockerfile` and `compose.yaml` run the server in a
container that owns the data. The image holds the three distributions,
runs as a user without a login, and keeps data, state and config in the
named volumes `graph_data` (`/data`), `graph_state` (`/state`) and
`graph_config` (`/config`). On macOS a named volume lives in the Docker
VM's Linux file system, so locks and `fsync` behave as on Linux; a bind
mount of an existing data root works when the container is its only
writer, but is not the default.

```sh
docker compose up -d                       # 127.0.0.1:8470 on this machine only
docker compose exec graph graph-admin token create <name> --project <value>
GRAPH_DOMAIN=graphs.example.org docker compose --profile tls up -d   # HTTPS via Caddy
docker compose exec graph graph-admin view --graph <graph> --host 0.0.0.0 --port 8471 --no-open
```

The first start creates the data root and installs a default
installation policy: the server may write every graph from a project
scope, and a request without one may read. Add scopes for your projects
(or bind tokens to projects) and apply your own policy. Maintenance runs
in the container (`docker compose exec graph graph-admin ...`); the
directory `GRAPH_WORKSPACE` (default `docker/workspace`) is mounted read
only at `/workspace` for authored ontologies and policies. To maintain
from another machine instead, turn on the [admin API](#admin-api) in
`/config/graph_cli/config.toml` in the container, restart it, create an
admin token there, name it among the policy's `maintainers` if the policy
has them, and use `graph-admin` in remote mode. A
[live view](live_view.md) started in the container on port 8471 is
published to `127.0.0.1:8471` (`GRAPH_VIEW_PORT`); open the printed
address on the host. The `tls`
profile adds Caddy with an automatic certificate for `GRAPH_DOMAIN`; then
80 and 443 are the only ports published beyond `127.0.0.1`. A healthcheck
asks `/v1/health`.

Moving an existing data root into the container:

1. Stop every local writer. If the installed policy names `maintainers`,
   add `{id: "os:10001"}` (the container's user) and grant the server, then
   apply it here: in the container no other user can apply a policy, not
   even run `audit verify`. Run `graph-admin audit verify`.
2. Copy the data root into the volume, for example with `docker compose
   run --rm --no-deps -v <data root>:/source:ro --entrypoint sh graph -c
   'cp -a /source/. /data/graph_cli/'` before the first start. Settings
   the server reads (`[audit]`, `[files]`, `[server]`) go into
   `/config/graph_cli/config.toml` in the container.
3. Start the container and run `graph-admin audit verify` in it.
4. Create the tokens and switch the clients to the
   [remote mode](#remote-mode-of-graph).

`docker build --target test .` runs the whole test suite on Linux in the
image.
