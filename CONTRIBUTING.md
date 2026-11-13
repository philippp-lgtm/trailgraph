# Contributing

Setup is in the [README](README.md#install). The design of the code base
(layers, import contracts, packages) is in
[`docs/architecture.md`](docs/architecture.md).

## Development

```sh
uv sync                     # create .venv with the three distributions + dev tools
uv run pytest               # all tests (includes import contracts and wheel checks)
uv run pytest -m "not slow" # skip distribution builds, crash and process tests
uv run pytest -n 8          # in parallel (pytest-xdist); the image's test stage does the same
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run lint-imports
GRAPH_CLI_UPDATE_GOLDENS=1 uv run pytest  # rewrite golden files and the generated skill after an intended change
docker build --target test .               # the whole suite on Linux, in the image
GRAPH_CLI_DOCKER_TESTS=1 uv run pytest tests/docker  # the container: kill mid-write, restart, verify
```

Run the CLIs from the checkout:

```sh
uv run graph --help
uv run graph-admin --help
uv run graph-server serve --port 0
```

## CI and releases

`.github/workflows/check.yml` runs on every pull request and every push to
`main`: the lint commands above, the suite in the image (`docker build
--target test .`) and `scripts/smoke_image.sh` on the runtime image (it
starts, answers `/v1/health`, and `graph-admin audit verify` in it is
clean). The repository variable `CI_RUNS_ON` (a JSON `runs-on` value) moves
the jobs to a self-hosted runner; pull requests from forks always run on a
hosted one.

A push to `main` that merges a pull request rebased on `main` (its tree equals
the tree of the pull request head, and that head has a green first-attempt
`check` run) is not tested again: both jobs log "tree identical to PR #n head
..., validated in run ...; not re-run" and succeed. `scripts/ci-reuse-pr-run.sh`
decides, any doubt or API error means the full run, and
`tests/ci-reuse/run.sh` tests it offline. The rule and why it is safe: the
infra repository's `DECISIONS.md` I31.

A release is a tag `vX.Y.Z` on a commit of `main`. Before tagging, check
that the `check` run of that commit is green and wait for the `build` run
of the merge commit (it pushes the candidate image, see below); the release
workflow only looks them up to decide between promoting and building. The three distributions carry the version (`version` in each
`packages/*/pyproject.toml` and `__version__` in each package, then `uv
lock`, the goldens and the generated skill, which name it), bumped in a
pull request before the tag. The version is part of skill 1, so a client
of the previous release has to update. `.github/workflows/build.yml`
refuses a tag that differs from that version, is not on `main` or names an
image that exists already.

The image is built once and promoted (infra `DECISIONS.md` I32). A push to
`main` that changes the image inputs (`Dockerfile`, `.dockerignore`,
`pyproject.toml`, `uv.lock`, `packages/`, `scripts/build_id.py`, `docker/`) builds
the `linux/amd64` runtime image, runs the smoke test on it and pushes it as the
candidate `ghcr.io/<owner>/<repository>:sha-<commit>` (revision label, digest in the
job summary); the suite runs in `check` on the same commit. A tag then adds
`vX.Y.Z` to the candidate of the newest commit at or before the tagged one that
changed the inputs: `scripts/ci-promote-image.sh` tags the same manifest, so the
digest is the candidate's and nothing is rebuilt (minutes become seconds). It
promotes only if the digest that the `build` run on `main` recorded in its
artifact `candidate-digest` is the one the tag `sha-<commit>` still names (the
recorded digest is what gets promoted, never the mutable tag itself), its revision
label and platform match, and the `build` and `check` runs on `main` of that commit and
the `check` run of the tagged commit are first-attempt successes (a run that is
green only after a re-run, a cancelled or a still running one does not count)
and the version tag is free. Anything else, including any error, builds
as before: the suite on the tagged tree (target `test`), the
smoke test on the `linux/amd64` image, then the push as
`ghcr.io/<owner>/<repository>:vX.Y.Z`. A promoted image has no
`org.opencontainers.image.version` label (the candidate is built before the version
is known); its revision label names the commit. `tests/ci-promote/run.sh` tests the
decision offline. The job summary names the digest either way,
which deployments pin (`<tag>@sha256:<digest>`). A pushed tag is never
moved; fix forward with the next version.

## Documentation

Documentation is split by reader and purpose. Put new text where its
reader looks for it:

| Document | Reader | Holds |
| --- | --- | --- |
| `README.md` | newcomers | what graph_cli is, status, install, quickstart, links; nothing else |
| `CONTRIBUTING.md` | contributors | development, tests, fixtures, generated files, these rules |
| `docs/README.md` | everyone | the index of `docs/`, one line per document |
| `docs/cli_graph.md`, `configuration.md`, `files.md`, `skills.md` | users of `graph` | behavior, options, concepts across commands |
| `docs/cli_graph_admin.md`, `access_control.md`, `security_model.md`, `live_view.md` | maintainers | operating graphs, policies, what is protected and what is not |
| `packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/` | maintainers (agents and people) | the admin skills: `ontology-best-practices` (the `graph_cli.ir/1` format reference, modeling guidance, change levels, release workflow), `maintaining-graphs` (the `graph-admin` workflow), `authoring-graph-skills` (graph skill templates), `authoring-graph-functions` (named read functions: the `graph_cli.function/1` format reference, what publishing checks, writing guidance), `authoring-graph-actions` (named write actions: the `graph_cli.action/1` format reference, what publishing checks, writing guidance) |
| `examples/<name>/README.md` | newcomers and maintainers | what one example graph shows, how to build it in a throwaway installation, how to load it into an existing one, sample queries; the example's files are its source |
| `docs/architecture.md`, `storage.md`, `audit.md` | contributors | layers, modules, on-disk formats, verification |

Rules:

- **One home per topic.** Every fact is written in one document; other
  documents link to it instead of repeating it.
- **User documents describe behavior, not code.** Module tables and
  package names belong in the internals documents.
- **Generated files are the reference.** The per-command reference of
  `graph` is the generated `skills/graph-cli/references/` (see
  [Generated files](#generated-files)); `docs/cli_graph.md` keeps the
  command overview and what applies across commands.
- **New documents** get a lowercase name with `_` and a line in
  `docs/README.md`.
- **Examples stay generic** (see `CLAUDE.md`): placeholders such as
  `<graph>` or `example_graph`, or the toy fixtures.

Update the documentation in the same change as the code:

| Change | Update |
| --- | --- |
| a `graph` command or option | regenerate the skill; `docs/cli_graph.md`: a row in the command overview for a new command (checked), the cross-command rules when they change |
| a `graph-admin` command or option | update the inventory golden; `docs/cli_graph_admin.md`: a row in the command overview for a new command (checked), the cross-command rules when they change; the admin skills that name it (`maintaining-graphs`; command lines are checked) |
| a `config.toml` key, pointer key or environment variable | `docs/configuration.md` |
| a policy rule, action or identity source | `docs/access_control.md`, and `docs/security_model.md` if what is prevented or detected changes |
| a key, base type, facet or check of `graph_cli.ir/1`, a change level of `ontology diff`, or when a check runs | `bundled_skills/ontology-best-practices/references/` of `graph_cli_admin` (`ir_format.md` is checked against the parser; the change levels and check timing are not) |
| a key, type, limit or check of `graph_cli.function/1`, or a rule publishing applies to functions | `bundled_skills/authoring-graph-functions/references/format.md` of `graph_cli_admin` (the format has one home there; `docs/cli_graph.md` and `docs/cli_graph_admin.md` describe behavior and link to it) |
| a key, type, limit or check of `graph_cli.action/1`, or a rule publishing applies to actions | `bundled_skills/authoring-graph-actions/references/format.md` of `graph_cli_admin` (the format has one home there; `docs/cli_graph.md` and `docs/cli_graph_admin.md` describe behavior and link to it) |
| a layer, package or module | `docs/architecture.md` (or `storage.md`, `audit.md`) and the import contracts |
| an on-disk format or a verification check | `docs/storage.md` or `docs/audit.md` |
| install steps or the quickstart | `README.md`; run the quickstart on a throwaway data root |

`uv run pytest tests/test_docs.py` checks that every link and anchor
resolves, that `docs/README.md` lists every document and that the command
overviews in `docs/cli_graph.md` and `docs/cli_graph_admin.md` name exactly
the commands of `graph` and `graph-admin`, and only options those commands
take.

## Generated files

`skills/graph-cli/` is generated from the app inventory and never edited by
hand (see [`docs/skills.md`](docs/skills.md#the-graph-cli-skill)).
Regenerate after a change to a command declaration or to the change set
keys in `kernel.changeset`, then review the diff:

```sh
GRAPH_CLI_UPDATE_GOLDENS=1 uv run pytest tests/skills
```

The tests fail on a stale skill (the committed files must equal a fresh
render of the live inventory), check every generated table against the
inventory, the change set tables and example against the parser, the
output fields against real answers and the envelope keys against the
renderer (docs-vs-code), and
reject leaks: fixture vocabulary, maintenance commands, actions or codes,
placeholders outside `<graph> <type> <id> <attr> <value> <relation>
<module> <function> <action> <state> <file> <json> <seq> <dir>`, paths, ids and non-ASCII
text. The skill names no graph type.

The OpenAPI documents of the server are generated from the inventories with
output schemas and kept as goldens (`tests/goldens/server/`), so every API
change shows as a diff. A change to a command the server runs (its
parameters, errors or `output`) or to a shared definition changes them;
regenerate and review:

```sh
GRAPH_CLI_UPDATE_GOLDENS=1 uv run pytest tests/unit/server/test_openapi.py
```

A client of another build keeps working against the server as long as its
compatibility id is the same ([Builds](docs/server.md#builds)). The id
follows the inventory and the agent skills by itself; `WIRE_LEVEL` in
`graph_cli.catalog.compat` is set by hand. Raise it when the request or
answer format changes so that an older client misreads a newer server or
the other way round, or when a fix in code that runs on the client (the
remote client, hooked commands, text output, skill install, self update)
must reach every client now. `tests/server/test_wire_level.py` pins the
headers and envelope keys per level and fails until the change is judged.

## Tests

| Kind | Where | What |
| --- | --- | --- |
| unit | `tests/unit/` | modules in isolation |
| contract | `tests/contract/` | the storage ports and the write service on every backend and both toy ontologies; the seeds |
| golden | `tests/goldens/` | frozen on-disk formats and compiled artifacts; the OpenAPI documents of the server (`tests/unit/server/test_openapi.py` also runs `openapi-spec-validator` on them) |
| crash | `tests/crash/` | `kill -9` of a child process at every named fault point, for storage commits and for the write service with a retry; interrupted maintenance operations |
| concurrency | `tests/concurrency/` | real processes: lock timeout, no lost writes, alias uniqueness, reader isolation, conflicting `expect_rev` |
| tamper | `tests/tamper/` | hand-edited files: torn tails, interior damage, one scenario per storage and audit finding code |
| CLI | `tests/cli/` | `graph` and `graph-admin` as subprocesses: commands, envelopes, exit codes, one scenario per app and admin code; the P1 exit criteria through `graph` (failing audit sink, kill -9 then retry, parallel writers, nothing written by reads) |
| skills | `tests/skills/` | the committed `graph-cli` skill: freshness, docs-vs-code, leaks (also of every `skills emit` output, static and generated graph skills), structure; the hand-written `ontology-best-practices` skill: its YAML examples compile, the format reference names every accepted key, structure |
| server | `tests/server/` | `graph-server` in-process (routes, auth, uploads, CORS, denials audited) and as a process (threads plus a `graph-admin` process lose no writes); the remote `graph` against stub servers (lost answers, retries, versions); every command answers the same locally and remotely on both toy ontologies; the admin API (token scopes, maintainers, no paths from requests) and every `graph-admin` command it runs answering the same from a remote-mode host |
| docker | `tests/docker/` | opt-in (`GRAPH_CLI_DOCKER_TESTS=1`): the container killed mid-write recovers, `audit verify` in it stays clean |
| architecture | `tests/test_architecture.py`, `tests/test_source_rules.py` | import contracts with positive controls, source rules, wheel contents |
| docs | `tests/test_docs.py` | documentation links and anchors resolve (also inside the `ontology-best-practices` skill), the docs index lists every document, the command overviews of `graph` and `graph-admin` match their inventories |

Crash and concurrency tests spawn processes and are marked `slow`; they run
by default and take a few seconds. Storage tests build transactions with a
test-only builder (`tests/support/txbuilder.py`) driven by the compiled toy
ontologies. Storage tests use minimal audit bodies
(`tests/support/drafts.py`); tests that verify audit streams use real events
(`tests/support/audit_events.py`). Write service tests run on both backends
through `tests/support/services.py` (an in-memory catalog, a builder with
test clock and seeded ids, and a factory for valid creates of any class).
`uv run python scripts/bench_store.py` (not part of the suite) times reads
and writes of `FileStore` on a generated graph, with and without the
writer's full replay; with `--migration` it migrates a generated graph of about 6,300
entities and 16,000 edges (every class and relation moves to a new module) with real fsyncs
and prints the time of each phase and the peak memory.

### Test harness

CLIs are tested as subprocesses (`tests/conftest.py`: `run_cli`). Every run
gets a throwaway `HOME` and XDG directories, no `GRAPH_CLI_*` variables and no
`CLAUDE_CONFIG_DIR`, so tests never touch real user config, data, state or
installed skills.

Every envelope the suite sees is checked against the declared schemas
(`tests/support/output_schemas.py`, a pytest plugin): in-process renders, the
stdout of every `run_cli` subprocess and every answer `support.server.call`
gets, as the server sent it. The envelope must match the
`envelope` definition and `data` the `output` of its command, with every
object closed (`unevaluatedProperties: false` unless the schema says
`additionalProperties` itself), so a key the code adds without declaring it
fails the test that saw it. A new command declares `output` on its
`CommandSpec` (a unit test refuses one without); a new key of an answer goes
into the schema in the same change. Build objects with
`cli_kit.output_schemas.obj` (required and optional keys, `extra=` for maps)
and never extend them with `allOf`. A test whose envelopes come from a stub
server marks itself `@pytest.mark.unchecked_output("<reason>")`.

### Fixtures

`examples/company/` is not a fixture but a user-facing example graph (a modular ontology
in two versions, seeds, a graph skill, `setup.sh` and `load.sh`); it stays separate from
the `company` fixture below, and `tests/cli/test_company_example.py` keeps it buildable
(the `setup.sh` runs are marked `slow`).

`tests/fixtures/` holds two unrelated single-module toy ontologies
(`notes_and_sources`, `inventory` with a version series) and a third that is
authored as several modules (`company`: `people`, `organization` and, from
1.1.0, `operations`, brought together by the main module `company`, whose
version is the graph's release; its tests are `tests/cli/test_company_modules.py`
and the `MODULAR_ONTOLOGIES` entries of the compiler, diff and vocabulary tests;
the role-based and contract tests run on the single-module toys only), one broken ontology per `model.*` finding
code, one broken change set per `data.*` finding code, seed change sets for
all three (`tests/fixtures/seeds/`, applied through the write
service; `company` has a second file for the classes of 1.1.0), role mappings for role-based contract tests
(`tests/support/roles.py`) and graph skills for the `inventory` test graph
(`tests/fixtures/skills/`: a static one, a template with curated text, and
`blocks/` with every block once, rendered for both toy ontologies into
`tests/goldens/skills/`), function files for `company`
(`tests/fixtures/functions/company/`: a valid set, compiled into
`tests/goldens/functions/`, and one broken file per check) and a vendored subset of the openCypher TCK for
the Cypher queries (`tests/fixtures/opencypher_tck/`, Apache-2.0; its README
names the source commit and how to update it; runner in
`tests/support/tck.py`).
Core tests never name a fixture type; a leak test keeps fixture vocabulary out
of the packages. Deterministic test doubles (`FixedClock`, `StepClock`,
`SeededIdProvider`) live in `tests/support/`. Golden files live in
`tests/goldens/` and next to the fixtures they describe.
