# Configuration

Both CLIs locate everything with the same code (`graph_cli.catalog`).

## Locations

XDG; macOS uses the same paths.

| What | Where |
| --- | --- |
| Config | `$XDG_CONFIG_HOME/graph_cli/config.toml`, else `~/.config/graph_cli/config.toml` |
| Data root | `graph-admin --data-root` (`graph` has no such flag), then `GRAPH_CLI_ROOT` (warns), then config `[paths] data_root`, then `$XDG_DATA_HOME/graph_cli`, else `~/.local/share/graph_cli` |
| State (operational log, access log, viewer sockets) | `$XDG_STATE_HOME/graph_cli`, else `~/.local/state/graph_cli` |

Keep the data root on a local file system (see [Storage](storage.md)).

## `config.toml`

`graph-admin init` creates it with a device id. It holds the device id and
name, runtime mode, data root, identity providers, lock timeout, durability,
audit redaction and the access log, output default, file roots, file size
limit and the read signal. The sections described elsewhere:

- `[audit]`: `record_data_root` (default `true`) puts the data root path
  into audit events; `access_log` (default `false`) turns on the
  [access log](audit.md#access-log)
- `[roots]` and `[files]`: [Files](files.md#origins-and-roots)
- `[activity]`: [Live view](live_view.md)

Unknown config or manifest keys are warnings; an unknown `format_version`
or an invalid value stops the command.

## Project pointer

`.graph_cli.toml` in a project directory (the nearest one up from the
working directory applies) lists the graphs a project wants (`graphs`),
optionally a `default` and a `project` name for the actor; it cannot move
the data root.

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
| `GRAPH_CLI_AGENT_ID`, `GRAPH_CLI_AGENT_TYPE`, `GRAPH_CLI_SESSION_ID`, `GRAPH_CLI_PARENT_SESSION_ID` | the asserted agent and session ([Identity](access_control.md#identity)) |

Only `catalog` and the admin wiring may read the process environment.
