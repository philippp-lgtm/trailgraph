# Configuration

Both CLIs locate everything with the same code (`graph_cli.catalog`).

## Locations

XDG; macOS uses the same paths.

| What | Where |
| --- | --- |
| Config | `$XDG_CONFIG_HOME/graph_cli/config.toml`, else `~/.config/graph_cli/config.toml` |
| Data root | `graph-admin --data-root` (`graph` has no such flag), then `GRAPH_CLI_ROOT` (warns), then config `[paths] data_root`, then `$XDG_DATA_HOME/graph_cli`, else `~/.local/share/graph_cli` |
| State (operational log, access log, viewer sockets) | `$XDG_STATE_HOME/graph_cli`, else `~/.local/state/graph_cli` |
| Tokens of a client, per server and project | `tokens/<server>/<project>/token` and `admin_token` next to `config.toml`; `<server>` is host, port and path of the endpoint (directories 0700, files 0600) |

Keep the data root on a local file system (see [Storage](storage.md)).

## `config.toml`

`graph-admin init` creates it with a device id. It holds the device id and
name, runtime mode, data root, identity providers, lock timeout, durability,
audit redaction and the access log, output default, file roots, file size
limit and the read signal. The sections described elsewhere:

- `[audit]`: `record_data_root` (default `true`) puts the data root path
  into audit events; `access_log` (default `false`) turns on the
  [access log](audit.md#access-log)
- `[output]`: `default` (`text` or `json`); `spill_bytes` (default
  16384, 0 turns it off) and `spill_dir` (absolute or `~/`, default
  `/tmp/graph_cli_<uid>`) send larger `graph` JSON answers to a file
  ([Large answers go to a file](cli_graph.md#large-answers-go-to-a-file))
- `[roots]` and `[files]`: [Files](files.md#origins-and-roots)
- `[files] store` and `[files.s3]`: [Object storage](#object-storage)
- `[activity]`: [Live view](live_view.md)
- `[server]`: `cors_origins`, the browser origins (`https://host[:port]`)
  allowed to call `graph-server` (default none); `admin_api` (default
  `false`) makes it serve the [admin API](server.md#admin-api) too;
  `client_builds`: `"compatible"` (default) refuses a client of another
  build only when its commands, answers or skills differ, `"exact"` every
  client of another build, `"warn"` none: it gets a warning
  ([Builds](server.md#builds));
  `dist` (default `true`): `false` hands out no programs
  ([Self update](server.md#self-update)); `public_url`, the `http(s)` URL
  clients reach the server at (such as the domain of a proxy in front of
  it, without credentials or query): the base of the problem types and the `servers` of
  the OpenAPI documents ([API](server.md#api)), unset by default; read by
  the server only
- `[runtime]` `mode = "remote"` with `endpoint`, and `[remote]`: the
  [remote mode](#remote-mode)

Unknown config or manifest keys are warnings (except in `[files.s3]`); an
unknown `format_version` or an invalid value stops the command.

### Object storage

`[files] store` decides where `graph-admin graph create` puts the stored
files of a **new** graph: `"local"` (the default) in the data root,
`"s3"` in the S3-compatible object store of `[files.s3]`. Existing graphs
stay where they are until `graph-admin files move`; changing the setting
never moves one. What moves, permissions, backups:
[Object storage](files.md#object-storage-optional).

```toml
[files]
store = "local"          # default; "s3": new graphs keep their stored files in [files.s3]

[files.s3]
bucket = "example-bucket"
prefix = ""              # optional key prefix; "graph_cli" becomes "graph_cli/"
endpoint_url = ""        # empty: AWS by region
region = ""
addressing = "path"      # or "virtual"
# allow_plain_http = true  # http:// to a host that is not loopback
```

- `[files.s3]` takes exactly these keys: any other key stops the command
  (`config.invalid`), so a credential pasted into the file is never silently
  ignored. Credentials come only from the environment
  ([Environment variables](#environment-variables)), and the environment
  wins over the file for the bucket, the endpoint and the region, so one
  file can serve several deployments.
- `bucket` and `region` are required (no default region); the prefix is
  segments of letters, digits and `!_.*'()-` separated by `/`.
- The endpoint is an `http(s)` URL without user info, path, query or
  fragment; `http://` only to a loopback host unless `allow_plain_http =
  true` (a store inside a private network, such as a cluster's; file bytes
  then travel in clear there, though checked against their hash). Empty
  means AWS itself, reached by region.
- `addressing = "path"` puts the bucket into the path
  (`<endpoint>/<bucket>/<key>`, what most self-hosted stores expect);
  `"virtual"` into the host name (`<bucket>.<endpoint host>`).
- With `store = "local"` only the types of `[files.s3]` are checked; the
  settings are used by `graph-admin files move --to s3` and by graphs
  already in an object store. With `store = "s3"` an incomplete or invalid
  setting stops every command. A graph in an object store opened without
  usable settings fails only where it needs its file bytes
  (`backend.unavailable`).
- Every process that reads or writes stored files needs the settings and
  the credentials: `graph` in the local mode, `graph-server`, `graph-admin`
  and its live view. A `graph` in the [remote mode](#remote-mode) needs
  none.

## Remote mode

With `mode = "remote"`, `graph` keeps no data on this machine: every
command goes to a [`graph-server`](server.md) and its answer is printed as
if it ran here.

```toml
[runtime]
mode = "remote"
endpoint = "https://graphs.example.org"   # http:// only to 127.0.0.1 or localhost

[remote]
token_file = "~/.config/graph_cli/token"  # or GRAPH_CLI_TOKEN; chmod 600
admin_token_file = "~/.config/graph_cli/admin_token"  # graph-admin; or GRAPH_CLI_ADMIN_TOKEN
timeout_s = 60
# allow_plain_http = true                 # http:// to another host: the token travels in clear
```

The token comes from `GRAPH_CLI_TOKEN`, else from the token stored for the
configured endpoint and the project of the pointer that applies
(`<config dir>/tokens/<server>/<project>/token`, written by `graph
connect`), else from `token_file`. A token file must not
be readable by group or others. `graph-admin` sends its commands to the
server's [admin API](server.md#admin-api) with an admin token from
`GRAPH_CLI_ADMIN_TOKEN`, else `<config dir>/tokens/<server>/<project>/admin_token`,
else `admin_token_file` (the same rules); what only runs where the data
lives (`init`, `view`, `--data-root`) is refused (`admin.remote_mode`).
Tokens per project let one machine work for several projects, each folder
with its own token, while the folder itself (whose pointer may be in git)
holds none; keyed by the server too, a token is never sent to another
server after the endpoint changes. What changes for each command:
[Server](server.md#remote-mode-of-graph).

`graph connect <endpoint> --code <code>` sets this up from an enrollment
code ([Server](server.md#enrollment)): it stores the token per project,
writes `mode` and `endpoint` into `config.toml` (a new file gets a device
id; in an existing one only those two keys change, and another endpoint or
the local mode is replaced only with `--force`) and writes a pointer into
the folder when it has none.

## Project pointer

`.graph_cli.toml` in a project directory (the nearest one up from the
working directory applies) lists the graphs a project wants (`graphs`),
optionally a `default` and a `project` name for the actor; it cannot move
the data root, name a server (`endpoint`) or carry a token (`token*`).

```toml
format_version = 1
project = "example_project"
graphs = ["example_graph", "other_graph"]
default = "example_graph"
```

The pointer only narrows what the installation policy allows (see
[Access control](access_control.md)). How a command picks its graph:
[Graph selection](cli_graph.md#graph-selection).

## Environment variables

| Variable | Effect |
| --- | --- |
| `GRAPH_CLI_GRAPH` | the graph when `--graph` is not given ([Graph selection](cli_graph.md#graph-selection)) |
| `GRAPH_CLI_ROOT` | the data root, with a warning ([Locations](#locations)) |
| `GRAPH_CLI_OUTPUT` | `json` makes JSON output the default |
| `GRAPH_CLI_SPILL_BYTES` | overrides `[output] spill_bytes`; `0` keeps every answer on stdout |
| `GRAPH_CLI_TOKEN` | the bearer token in [remote mode](#remote-mode), before `[remote] token_file` |
| `GRAPH_CLI_DIST_DIR` | the directory of wheels `graph-server` hands out ([Self update](server.md#self-update)); set in the image |
| `GRAPH_CLI_ADMIN_TOKEN` | the admin token of `graph-admin` in [remote mode](#remote-mode), before `[remote] admin_token_file` |
| `GRAPH_CLI_AGENT_ID`, `GRAPH_CLI_AGENT_TYPE`, `GRAPH_CLI_SESSION_ID`, `GRAPH_CLI_PARENT_SESSION_ID` | the asserted agent and session ([Identity](access_control.md#identity)) |
| `GRAPH_CLI_S3_BUCKET` | the object store's bucket, before `[files.s3] bucket` ([Object storage](#object-storage)) |
| `AWS_ENDPOINT_URL_S3`, then `AWS_ENDPOINT_URL` | the object store's endpoint, before `[files.s3] endpoint_url` |
| `AWS_REGION`, then `AWS_DEFAULT_REGION` | the object store's region, before `[files.s3] region` |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` | the object store's credentials (both keys or neither; the session token is optional); only from the environment |
| `AWS_CA_BUNDLE` | an absolute path to the CA certificates for the object store's TLS |

Only `catalog` and the admin wiring may read the process environment.
