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

## Enrollment

Instead of passing a token to a client, a maintainer can hand out a
one-time code, and the client fetches its own token with it. The code is
bound to a project, so whatever it yields is a token of that project: the
maintainer's own token never leaves where it is.

```sh
graph-admin enroll create --project <value>                 # an app token's code, 15 minutes
graph-admin enroll create --project <value> --scope admin --expires-in 2h   # a project admin's
graph-admin enroll create --project <value> --role <value>  # an app token that holds a role
graph-admin enroll list                                     # codes not used yet
graph-admin enroll revoke <id>
```

`enroll create` needs `maint.token_create` (whoever may create tokens),
locally or over the [admin API](#admin-api). The code (`gce_` and 26
characters) is shown once and works once, for at most 24 hours (`--expires-in
<n>m|<n>h`, default 15 minutes); the data root keeps its hash. `create`
warns when the installed policy would give a token of the project nothing.
An app code may carry roles (`--role`, repeatable): the token the exchange
yields holds them, as if `token create --role` had made it. They must be
declared by the installed installation policy
([Roles](access_control.md#roles)), and an admin code takes none. The `graphs`
a check or an exchange names are those the token reaches with its roles.

On the client, one command sets up the machine and a folder:

```sh
graph connect <endpoint> --code <code> [--dir <folder>] [--label <label>]
uvx --from <endpoint>/v1/dist/<file> graph connect <endpoint> --code <code>   # without graph yet
```

`<file>` is the `graph` wheel the [manifest](#self-update) names. `connect`
first asks the server what the code is for (that uses nothing) and checks
everything here that could stand in the way: `config.toml` with another
endpoint or the local mode (`--force` replaces it: it changes where every
`graph` command of this user goes), a pointer in the folder that names
another project or none, a token stored for the project before (`--force`
replaces it; revoke the old one), a project whose token would reach no
graph. Any of these is `connect.refused` and the code stays unused. Then it:

1. exchanges the code for a token named `<project>.<label>`, the label
   `--label`, default the host name (`<host>-admin` for an admin code;
   the client picks only the label, so
   it can never take a name a policy rule grants something to; a name that
   is taken or named by the policy is refused and the code stays unused),
   and stores it in `<config dir>/tokens/<server>/<project>/`
   ([Configuration](configuration.md#remote-mode));
2. writes the remote mode and the endpoint into `config.toml`;
3. writes `.graph_cli.toml` into the folder, unless one applies there
   already: the project, the graphs the token can use, the default when it
   is one;
4. installs `graph` from the server as a uv tool when it runs as none
   (under `uvx`, for example) and the server's build otherwise
   ([Self update](#self-update)); a checkout is left as it is;
5. installs the graph skills of the pointer's graphs into the folder's
   `.claude/skills/` (creating `.claude`) and runs the installed `graph
   self update`.

An admin code (`--scope admin`) stores `admin_token` instead and sets up
`graph-admin` (with the admin token, the `graph-admin` wheel), with the
graphs the project maintains in the pointer: `graph-admin graph pull` and
`graph-admin graph push` then work in that folder for the project's graphs. It installs no skills
and no app token; a project admin who also uses `graph` there gets a
second, app code.

The exchange is `POST /v1/enroll` with `{code, label?, check?}` (at most 4
KiB, no token, never refused for a build): with `check: true` it answers
`{enrollment: {id, project, scope, expires_at, graphs}}`, else `{token,
name, project, scope, graphs}`. An unknown, used, revoked or expired code
is `remote.unauthorized` (401), one answer for all of them, and so is a
code the audit says was used or revoked already (an exchange or revoke cut
short before the file changed). The exchange is audited as
`maint.token_create` by the enrollment itself (principal `enrollment:<id>`,
decision `enrollment:<id>`); see [Audit](audit.md).

A code does not depend on who created it: revoking the creator's token
leaves its codes valid, and the tokens they yield have no expiry. When a
maintainer token is revoked, also check `enroll list` and revoke what it
created. The code travels on the command line (`--code`), so it can show in
`ps` and the shell history; it is worthless once used or expired.

## API

Every path starts with `/v1/`; every answer is the `graph` envelope, and
the HTTP status follows its error category (200 ok, 400 usage, 401 no
valid token, 403 denied, 404 not found, 409 conflict, 422 refused or
invalid, 503 unavailable, 500 internal). `Graph-Cli-Exit-Code` carries the
exit code `graph` would have. A route called with another method answers
405 with `Allow`; a retryable 503 (such as `backend.lock_timeout`) carries
`Retry-After` in seconds.

| Endpoint | Purpose |
| --- | --- |
| `GET /v1/health` | `{ok, version, api}`; no token needed |
| `GET /v1/errors`, `GET /v1/errors/{code}` | every error code of `graph` and `graph-admin`, whether or not the admin API is on: `{errors: [...]}`, or one `{code, category, retryable, status, exit_code, summary}`; no token needed |
| `GET /v1/inventory` | the command inventory (`graph describe commands`) |
| `GET /v1/openapi.json` | an OpenAPI 3.1 document generated from the inventory |
| `POST /v1/commands/{path}` | run a command; `{path}` is the command path with `/` (`describe/types`) |
| `PUT /v1/uploads` | stage bytes for a file parameter; answers `{upload, size}` |
| `GET /v1/graphs/{graph}/files/{ref}` | the stored file of an entity, checked like `file path` and streamed ([Downloads](#downloads)) |
| `POST /v1/graphs/{graph}/skill` | the graph's published skill, rendered for installing |
| `GET /v1/dist/manifest`, `GET /v1/dist/{file}` | the programs the server hands out ([Self update](#self-update)); no token needed, except for the `graph-admin` wheel |
| `POST /v1/enroll` | an enrollment code exchanged for a token ([Enrollment](#enrollment)); no token needed |

Consumers that follow the graph poll `changes` through `/v1/commands/changes`
([Changes since a position](cli_graph.md#changes-since-a-position)); the server
offers neither long polling nor a stream for it.

A command request names each parameter by its API name, the option without
leading dashes and with `_` for `-` (`--dry-run` is `dry_run`, `--set-json`
is `set_json`, an argument such as `type` keeps its name):

```json
{"args": {"type": "<type>", "set": {"<attr>": "<value>"}, "dry_run": true},
 "actor": {"agent": {"id": "<value>", "type": "<value>"}, "project": "example_project"},
 "pointer": {"graphs": ["example_graph"], "default": "example_graph"}}
```

A value is what the option takes on the command line: a string, an integer,
`true` for a flag (`false` and `null` mean not given), a list for a
repeatable option. The options that take `<name>=<value>` pairs and
`--where` also take a structured form, turned into exactly the command line
it stands for, so the answer is the same:

| Parameters | Structured form | Command line |
| --- | --- | --- |
| `set`, `qualifier` | `{"<attr>": "<value>", "<attr>": 2}` | `--set <attr>=<value> --set <attr>=2` |
| `set_json`, `qualifier_json`, `param` | `{"<attr>": <json>}` | `--set-json <attr>=<json>` |
| `add`, `remove`, `edge` | `{"<attr>": ["<value>", "<value>"]}` | `--add <attr>=<value> --add <attr>=<value>` |
| `add_json`, `remove_json` | `{"<attr>": [<json>, <json>]}` | `--add-json <attr>=<json>` per value |
| `where` | `[{"field": "<attr>", "op": "ge", "value": 2}]` | `--where <attr>:ge=2` |

A text pair takes a string, a number (written as given, `1.10` stays
`1.10`) or a boolean (`true`, `false`); an object, array or `null` belongs
in the `_json` option, which writes any JSON value as JSON (a string stays
a string: `"12"`). A name holds no `=`. In a filter, `op` defaults to `eq`,
`exists` takes `true` or `false`, and `in` a non-empty list whose items hold
no `,`; the field holds neither `=` nor `:`. `--rel` and `--in-rel` take
their command-line text only. The inventory names (`"--set"`,
`"--dry-run"`) are still accepted; one parameter under both names is
refused. The body is strict JSON: a key given twice is refused
(`usage.invalid_input`).

A file parameter (`apply`, `query --file`, `file add`) takes an upload:
`{"upload": "sha256:<hex>", "name": "<file>"}`, for `file add` also the
`origin` the client computed. An upload is kept for its token only and
removed after the command that used it, or after an hour; a token's
pending uploads hold at most four times `[files] max_bytes`. A body larger
than any route takes is refused before it is read. `-` (stdin) does not
exist on the server: upload the input instead. Over the API `file path`
answers `path: null`; the bytes come from the file endpoint. `?render=text`
answers `{envelope, stdout, stderr}`, the text `graph` would print.

A write that takes `--idempotency-key` also takes the key as the header
`Idempotency-Key: "<key>"` (a quoted string, as the IETF draft of that
name says). Repeating the write with the same key and content answers the
first result again (`idempotent_replay: true`); the same key with other
content is `conflict.idempotency_key_reuse` (409; the draft says 422). The
header on a command without that option, or with another key in `args`,
is `usage.invalid_argument`. A key is optional.

An error answer is RFC 9457 problem details (`application/problem+json`)
when the request's `Accept` names that type, on every route, including a
missing token, an unknown route and a refused build; otherwise it stays
the envelope. The problem
has `type`, `title`, `status`, `detail` (the error's message) and the
envelope's `code`, `category`, `retryable`, `hint`, `details`,
`warnings`, the `exit_code`, and `data` for a partial result. `type` is
`<public_url>/v1/errors/<code>` and `title` the code's summary when
`[server] public_url` is set ([Configuration](configuration.md#configtoml)),
else `about:blank` and the status phrase. Match on `code`, which every
problem has. With `?render=text` an error asked for as a problem is the
problem only.

`GET /v1/openapi.json` describes every route above (`/v1/dist/` only while
the server hands out programs), with `servers` naming `[server] public_url`
when it is set. A command's answer `200` is the envelope with that command's
`data`, the schema `graph describe commands --output-schemas` declares; for
every status the command's declared error codes reach there is one response
that lists them in `x-graph-cli-error-codes`, and `default` covers every
other error answer (such as a malformed body). Schemas shared by several
commands are in `components.schemas` under their names in the inventory's
`$defs`; a command's `data` is `<operationId>_data`, the operation id being
the command path joined with `_` (`describe_types_data`). A request body
lists the API names with their structured forms; the inventory names are
marked `deprecated`. Every error response also offers
`application/problem+json` (the `problem` schema), and a write that takes
an idempotency key declares the `Idempotency-Key` header. The document is
open like the inventory: a client ignores keys it does not know.

Browsers may call the API from the origins listed in `[server]
cors_origins` ([Configuration](configuration.md#configtoml)); a browser
never sends the token on its own, so a page can only use a token it was
given. A page may send `Idempotency-Key` and `If-None-Match` and read
`ETag`, `Retry-After`, `Allow` and the `Graph-Cli-*` headers. With origins
listed, every answer carries `Vary: Origin`.

### Downloads

`GET /v1/graphs/{graph}/files/{ref}` runs the checks of `file path` for
the request's actor and then streams the stored bytes with their
`Content-Length`, `Graph-Cli-Content-Hash` and `Graph-Cli-Media-Type`:
from the graph's own file in the data root, or, for a graph whose stored
files are in an object store, from the store, checked against their hash
while they stream. The server never writes a copy of a file; `file path`
over the API answers `path: null` and reads no bytes. What fails before
the first byte (an object missing from the store, the store unreachable,
a file of one chunk whose bytes differ from their hash) is an error answer
like any other. What fails later (the bytes of a large file turn out
different from their hash at the end, the store connection breaks) can
only end the answer early: the body is shorter than its `Content-Length`,
the server closes the connection and logs the code. The remote `graph`
checks the hash of every download and refuses such a body.

The file download carries `ETag: "sha256:<hex>"` and
`Cache-Control: private, no-cache`: a client may keep the bytes (a
browser too, on disk, also after its token is revoked), and a request with
`If-None-Match` naming that tag answers 304 without them, after the same
check and audit as a download.

## Builds

The server is the origin: the program it runs is the one clients should
run. Every installed build of `graph` and `graph-admin` carries a build id,
a hash over the sources of the distribution and of the distributions it
depends on, so two installs of the same source tree have the same id
whatever their version says. The client names its program and build in
`Graph-Cli-Build: <program> sha256:<hex>`; every answer carries the
server's build of that program in the same header.

Most changes to the sources never reach a client: storage, services and
the live view run on the server. So an installed build also carries a
compatibility id, a hash over what a client brings to the server: its
command inventory with the schemas of the answers, the agent skills it
installs (the `graph-cli` skill for `graph`, the admin skills for
`graph-admin`) and the wire level, a number raised when the request or
answer format changes or a fix on the client side must reach every
client. Two builds with the same compatibility id parse the same
commands, read the same answers and tell the agent the same. The client
sends it in `Graph-Cli-Compat: <program> sha256:<hex>`, and every answer
carries the server's in the same header.

After the token, before any route, the server compares them. With
`[server] client_builds = "compatible"` (the default) another build of the
same program is refused with `remote.client_outdated` (422, exit code 1)
only when its compatibility id differs too, or when it sends none (a
client installed before compatibility ids); the message names both
builds. A client of another build with the same compatibility id runs and
warns `remote.version_differs` with `details.compatible: true`: `graph
self update` installs the server's build when it suits. `"exact"`
refuses every other build, `"warn"` none (the client warns instead, for a
rolling upgrade). Nothing is compared when either side has no build id: a
web app or `curl` sends none, and neither does a program run from a
checkout (`uv run graph`), which then only gets the warning when the
version string differs. `/v1/health` and `/v1/dist/` are never refused:
they are how a client catches up (`graph self update`).

A client computes its compatibility id on its first remote command after
an install and keeps it under the state directory
(`compat/<program>/<build>`); the server computes its own at start.

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

`graph start` (the first call of a session) does the skill half of this without replacing the
program: the server answers the graphs and what the token may use (`POST /v1/commands/start`, the
usual command route), the client compares its build with the manifest below, refreshes the stale skill
copies from the server's skills and names a different build with the hint `graph self update` (the
skills are then only reported: the update renders them again).

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
   (its `action`, such as `current`, `updated` or `skipped`), each scope's skills and what
   changed.

`--check` changes nothing: it compares the builds and dry-runs the skill
refresh, and exits 0 only when nothing is outdated (else
`update.available`). `remote.client_outdated` names `graph self update` as its hint;
`skills.outdated` names `graph start`, which refreshes the skills without replacing the program. `graph-admin self update` does step 1
for `graph-admin` with the admin token; it installs no skills (its admin
skills are refreshed by `graph-admin workspace sync` or `skills install`).

## Skill freshness

A client sends the graph skills installed for its agent in
`Graph-Cli-Skills`: one item per installed copy, separated by `, `, each
`graph=<graph id>;hash=<slot hash>[;bundle=<bundle>;version=<version>][;visible=<digest>][;featured=<digest>];scope=<project|user>`
(the bundle and version of a generated skill's model, the digest of what a limited reader sees,
the digest of what the render shows of the catalogs: the featured lines and whether the graph has
functions, actions and views). The server judges
the item of the graph a command selects against the graph's published
skill, pin and catalogs: a write is refused with `skills.outdated` (422), a read
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
| `GET /v1/admin/openapi.json` | an OpenAPI 3.1 document of the admin API, `/v1/health`, `/v1/errors`, `/v1/uploads` and `/v1/dist/`, built like the [app document](#api) |
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
It runs only the commands on one graph (`graph-admin graph show`,
`graph-admin graph check`, `graph-admin graph pull`, `graph-admin graph push`,
`graph-admin graph reindex`, `graph-admin graph skill publish`,
`graph-admin graph skill render`, `graph-admin graph functions publish`,
`graph-admin graph functions show`, `graph-admin graph actions publish`,
`graph-admin graph actions show`, `graph-admin graph views publish`,
`graph-admin graph views show`, `graph-admin ontology release`,
`graph-admin ontology diff`, `graph-admin files verify`) and
`graph-admin graph list`, and sees only the graphs it maintains; every other command is refused with `access.ceiling`.

Answers, errors and problem details follow the [app API](#api). No
`graph-admin` command takes an idempotency key, so `Idempotency-Key` is
refused there.

The API never takes a path: `--data-root` is not part of it, a file
parameter (a policy) takes an upload, and a directory parameter (an
ontology, a skill) takes `{"files": {"<relative path>": "sha256:<hex>"},
"name": "<dir>"}`, every file an upload of the same token. Relative paths
have no empty, `.` or `..` parts; at most 1,000 files. The server builds the
directory under the name's last part and removes it after the command;
answers name the client's arguments, never the server's copy. `graph-admin graph skill
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
Every request, also of `graph-admin`, `graph connect` and `self update`,
names the client in `User-Agent: graph-cli/<version>`, so a reverse
proxy that refuses requests without one lets it through.

- A write always carries an idempotency key, in the `Idempotency-Key`
  header: the given `--idempotency-key`, else a generated one. If the
  connection breaks
  after a write was sent, `remote.outcome_unknown` names the key and the
  command to run again with it, which is safe. Reads are retried once;
  writes never. A read or dry run that uploads its input (`query --file`
  or `-`, `apply --dry-run`, `file add --dry-run`) uploads it again for
  the retry, because the server uses each upload up once.
- `remote.unreachable`: no connection; `remote.unauthorized`: no token, or
  the server refused it; `remote.protocol`: the answer is not the API's (a
  proxy page); `remote.client_outdated`: the server runs another build of
  `graph` ([Builds](#builds)); `remote.version_differs` (warning): the
  server runs another version or, in `warn` mode, another build.
- `describe commands` runs locally.
- `apply FILE|-` and `query -|--file` read their input here and upload it.
- `file add PATH` uploads the file with its origin under this machine's
  `[roots]`; the server stores it.
- `file path` downloads the stored file into this machine's
  [file cache](files.md#file-cache) and answers that path; `--origin` and
  `file check` look at the origins on this machine, as locally.
- `graph skills emit`, `graph skills install` and `graph skills check` run here; a graph skill comes from the
  server, rendered for the graph's current ontology, and must be allowed by
  this project's pointer.

## Remote mode of `graph-admin`

On the same machine `graph-admin` sends its commands to the admin API,
with the admin token from `GRAPH_CLI_ADMIN_TOKEN`, the project's stored
`admin_token` or `[remote] admin_token_file`
([Configuration](configuration.md#remote-mode)). It
parses the command line itself and prints the server's answer as its own,
as `graph` does.

- Ontology, skill, function, action and view directories and policy files are
  read here and uploaded: every regular file below a directory; a symlink or another
  file kind is refused.
- `graph-admin graph skill render` checks `--out` here and writes the preview into it.
- `graph-admin graph pull` writes the files the server answers into `DIR`, with the
  checks it makes locally; `graph-admin graph push` uploads only what push reads of `DIR` (the base
  file, `ontology/`, `skill/`, `functions/`, `actions/` and `views/`) and writes
  the new base into it.
- `graph-admin describe commands`, `graph-admin ontology validate`,
  `graph-admin skills install` and `graph-admin skills check` run here; `workspace sync` runs here and reaches the graphs through
  `graph-admin graph list` and `graph-admin graph pull`; `init`, `view` and `--data-root` are refused
  (`admin.remote_mode`).
- The server compares its build of `graph-admin` with this one, as for
  `graph` ([Builds](#builds)).
- Writes are never retried. A lost answer is `remote.outcome_unknown`:
  check the state (`graph-admin graph show`, `token list`, `policy show`) before
  running the command again. A release is the exception: `ontology release`
  and `graph-admin graph push` name the command to run again, which is safe, because a
  release that was applied is found installed and changes nothing, and one
  that was interrupted is finished (also a data migration, which commits
  once and is never applied twice). A command that writes nothing (every
  read, a dry run) is sent a second time, with its uploads, when the
  connection fails.
- A data migration runs inside one request, under the graph's lock, and
  answers when it is finished: see [Timeouts](#timeouts).

## Timeouts

The server puts no limit on how long a command runs; it answers when the
command is done. Three clocks can end the wait earlier:

- **The client's** `[remote] timeout_s`
  ([Configuration](configuration.md#remote-mode)) is the socket timeout of
  every request: it ends a request that sends or receives nothing for that
  long, which for a command is the time until the answer starts. A release
  with a data migration sends nothing back until the migration is done.
- **The server's** `channel_timeout` (120 seconds) closes a connection that
  is idle, before a request arrives or while the server cannot write; it
  does not interrupt a command that is running.
- **The ingress** (a reverse proxy or load balancer in front of the
  server) has its own read timeout for the time to the first byte of the
  answer, often 30 to 60 seconds. It ends the wait for the client, not
  the command: the server finishes it.

A command that outlasts one of them is applied anyway and the client sees
`remote.outcome_unknown` (a write) or `remote.unreachable`. Only a data
migration takes long enough to matter. It rewrites every entity it
touches in one transaction and materializes each revision with fsyncs.
Measure the preview on the real data
(`graph-admin graph check <graph> --ontology <dir>` counts what would
change), expect time roughly proportional to the entities migrated, and
before a large
migration set `[remote] timeout_s` and the ingress read timeout above the
expected duration with a margin (the answer reports `duration_s`). App
writers wait for the graph's lock up to `lock_timeout_s` and then get the
retryable `backend.lock_timeout`, so run a large migration in a quiet
moment. If an answer is lost anyway, wait and run the same release again:
it waits for the lock of the running migration, then finds it done or
finishes it, and applies nothing twice; `graph-admin graph show` and `audit verify`
confirm the result.

## Container

The repository's `Dockerfile` and `compose.yaml` run the server in a
container that owns the data. The image holds the three distributions
(without pip: nothing in it installs at run time), runs as a user without
a login, and keeps data, state and config in the named volumes
`graph_data` (`/data`), `graph_state` (`/state`) and `graph_config`
(`/config`). On macOS a named volume lives in the Docker
VM's Linux file system, so locks and `fsync` behave as on Linux; a bind
mount of an existing data root works when the container is its only
writer, but is not the default.

```sh
docker compose up -d                       # 127.0.0.1:8470 on this machine only
docker compose exec graph graph-admin token create <name> --project <value>
GRAPH_DOMAIN=graphs.example.org docker compose --profile tls up -d   # HTTPS via Caddy
docker compose exec graph graph-admin view --host 0.0.0.0 --port 8471 --no-open
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
address on the host. To serve it to other machines, see
[Behind a reverse proxy](live_view.md#behind-a-reverse-proxy). The `tls`
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
4. Create the tokens, or enrollment codes, and switch the clients to the
   [remote mode](#remote-mode-of-graph) (with a code: `graph connect`,
   [Enrollment](#enrollment)).

### Stored files in an object store

To keep stored files in an S3-compatible object store
([Object storage](files.md#object-storage-optional)), set `[files] store =
"s3"` and the non-secret `[files.s3]` keys in
`/config/graph_cli/config.toml` ([Configuration](configuration.md#object-storage))
and give the container the credentials as environment variables
(`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`; the bucket, endpoint and
region may come the same way), from the orchestrator's secret store (for
example a Kubernetes Secret through `envFrom`, or an `env_file` kept out of
git), never in the config file, which refuses unknown keys for that
reason. Every process in the container that touches stored files needs
them: the server, `graph-admin` in it and its live view. An endpoint inside
a private cluster network is often plain `http://`; that needs
`allow_plain_http = true`, and the bytes then travel in clear inside that
network (each download is still checked against its hash). The
[download route](#downloads) streams from the store; the server needs no
disk space for file copies. Then run `graph-admin files store check` in the
container, and move existing graphs with `graph-admin files move`
(back up the data root first).

`docker build --target test .` runs the whole test suite on Linux in the
image. Each release publishes the runtime image to the GitHub container
registry of the repository ([CI and releases](../CONTRIBUTING.md#ci-and-releases)).
