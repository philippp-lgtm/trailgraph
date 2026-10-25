# Documentation

Start with the [project README](../README.md) (install, quickstart).

## Using `graph`

- [`graph`: the application CLI](cli_graph.md): commands, output and exit
  codes, graph selection, references, filters, lookup
- Generated per-command reference:
  [commands](../skills/graph-cli/references/commands.md),
  [error codes](../skills/graph-cli/references/errors.md),
  [change sets](../skills/graph-cli/references/changesets.md),
  [output fields](../skills/graph-cli/references/output.md)
- [Configuration](configuration.md): locations, `config.toml`, the project
  pointer, environment variables
- [Files](files.md): storing files, origins and roots, backups
- [Skills](skills.md): the `graph-cli` skill, graph skills, installing
  skills

## Operating graphs

- [`graph-admin`: the maintenance CLI](cli_graph_admin.md)
- [Access control](access_control.md): identity, policy layers, decisions,
  authoring policies
- [Security model](security_model.md): what access control prevents, what
  it detects, what it does not
- [Live view](live_view.md): `graph-admin view`
- [Server](server.md): `graph-server`, the HTTP API that owns the data,
  tokens, the remote mode of `graph`, the container
- [Ontology authoring](../packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/ontology-best-practices/SKILL.md):
  the maintainer skill with the `graph_cli.ir/1` format reference, modeling
  guidance, versioning and the release workflow; the other admin skills in
  [Skills](skills.md#admin-skills)

## Internals

- [Architecture](architecture.md): layout, layers, catalog, write pipeline
- [Storage](storage.md): storage ports, FileStore, durability, recovery
- [Audit](audit.md): event streams, verification, policy verification

To work on graph_cli itself, see [`CONTRIBUTING.md`](../CONTRIBUTING.md).
