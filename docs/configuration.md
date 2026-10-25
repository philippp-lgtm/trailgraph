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
- `[activity]`: [Live view](live_view.md)
- `[server]`: `cors_origins`, the browser origins (`https://host[:port]`)
  allowed to call `graph-server` (default none); `admin_api` (default
  `false`) makes it serve the [admin API](server.md#admin-api) too;
  `client_builds`: `"exact"` (default) refuses a client of another build,
  `"warn"` lets it through with a warning ([Builds](server.md#builds));
  `dist` (default `true`): `false` hands out no programs
  ([Self update](server.md#self-update)); `public_url`, the `http(s)` URL
  clients reach the server at (such as the domain of a proxy in front of
  it, without query): the base of the problem types and the `servers` of
  the OpenAPI documents ([API](server.md#api)), unset by default; read by
  the server only
- `[runtime]` `mode = "remote"` with `endpoint`, and `[remote]`: the
  [remote mode](#remote-mode)

Unknown config or manifest keys are warnings; an unknown `format_version`
or an invalid value stops the command.

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

Only `catalog` and the admin wiring may read the process environment.
