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
uv run pytest tests/unit/services --lf  # one area, only what failed last time (see "Which tests to run")
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run lint-imports
GRAPH_CLI_UPDATE_GOLDENS=1 uv run pytest  # rewrite golden files and the generated skill after an intended change
docker build --target test .               # the whole suite on Linux, in the image
GRAPH_CLI_DOCKER_TESTS=1 uv run pytest tests/docker  # the container: kill mid-write, restart, verify
tests/integration/run_garage.sh            # stored files in a real object store (Garage in Docker)
```

The page of `graph-admin view` is kept as separate source files under
`packages/graph_cli_admin/src/graph_cli_admin/maintenance_cli/static/`:
`view.html` (markup), `view.css` and `js/*.js`, listed in `view.html` in load
order. The scripts are classic scripts that share one global scope, so the order
is the order of their top-level code. `maintenance_cli.view_page` inlines the
style sheet and the scripts into one document at start, so the view serves the
page on the one route `/` and a reverse proxy needs no route for assets.
Add a script by adding its `<script src="js/<name>.js">` line to `view.html`.

Run the CLIs from the checkout:

```sh
uv run graph --help
uv run graph-admin --help
uv run graph-server serve --port 0
```

## CI and releases

`.github/workflows/check.yml` runs on every pull request and every push to
`main`: the lint commands above, `scripts/lint-workflows.sh`, the suite in the
image (`docker build
--target test .`), `scripts/smoke_image.sh` on the runtime image (it
starts, answers `/v1/health`, and `graph-admin audit verify` in it is
clean) and the object store integration test against a real S3-compatible
store (`tests/integration/run_garage.sh`, see [Tests](#tests)). The repository variable `CI_RUNS_ON` (a JSON `runs-on` value) moves
the jobs to a self-hosted runner; pull requests from forks always run on a
hosted one.

A push to `main` that merges a pull request rebased on `main` (its tree equals
the tree of the pull request head, and that head has a green first-attempt
`check` run) is not tested again: every job logs "tree identical to PR #n head
..., validated in run ...; not re-run" and succeeds. `scripts/ci-reuse-pr-run.sh`
decides, any doubt or API error means the full run, and
`tests/ci-reuse/run.sh` tests it offline.

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

The image is built once and promoted. A push to
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
| `docs/overview.md`, `deployment_options.md`, `design_decisions.md` | everyone who wants the big picture, people and agents | what graph_cli is and is for, the core concepts, how the parts fit together, the ways to run it, the foundational design decisions and their trade-offs; plain language and diagrams, every detail a link to its home |
| `docs/cli_graph.md`, `configuration.md`, `files.md`, `skills.md` | users of `graph` | behavior, options, concepts across commands |
| `docs/cli_graph_admin.md`, `access_control.md`, `security_model.md`, `live_view.md`, `server.md` | maintainers | operating graphs, policies, what is protected and what is not, running the server and its API |
| `packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/` | maintainers (agents and people) | the admin skills: `ontology-best-practices` (the `graph_cli.ir/1` format reference, modeling guidance, change levels, release workflow), `maintaining-graphs` (the `graph-admin` workflow), `authoring-graph-skills` (graph skill templates: the block catalog, size budgets, what publishing checks; `docs/skills.md` keeps the concept), `authoring-graph-functions` (named read functions: the `graph_cli.function/1` format reference, what publishing checks, writing guidance), `authoring-graph-actions` (named write actions: the `graph_cli.action/1` format reference, what publishing checks, writing guidance), `authoring-graph-views` (views and set views: the `graph_cli.view/1` format reference, what a call answers and its bounds, what publishing checks, writing guidance) |
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
- **The big-picture documents stay coarse.** `overview.md`,
  `deployment_options.md` and `design_decisions.md` explain and link; they
  hold no limits, defaults, key lists or procedures, so a code change rarely
  touches them.
- **Diagrams are Mermaid blocks** (text, reviewed in the diff, rendered by
  GitHub). A diagram that mirrors code gets a check, like the layer diagram
  of `docs/architecture.md`.
- **Screenshots** are marked in the text with `<!-- screenshot: <name> -->`,
  directly followed by the image link to `images/<name>.png` (a PNG in
  `docs/images/`). A terminal session is shown as a real text transcript
  between `<!-- transcript: <name> -->` and `<!-- /transcript -->`, not as an
  image. Both are produced by a script, see [Generated files](#generated-files).
- **New documents** get a lowercase name with `_` and a line in
  `docs/README.md`.
- **Examples stay generic** (see `CLAUDE.md`): placeholders such as
  `<graph>` or `example_graph`, or the toy fixtures.

Update the documentation in the same change as the code:

| Change | Update |
| --- | --- |
| a `graph` command or option | regenerate the skill; `docs/cli_graph.md`: a row in the command overview for a new command (checked), the cross-command rules when they change |
| a `graph-admin` command or option | update the inventory golden; `docs/cli_graph_admin.md`: a row in the command overview for a new command (checked), the cross-command rules when they change; the admin skills that name it (`maintaining-graphs`; command lines are checked) |
| a `config.toml` key, pointer key or environment variable | `docs/configuration.md`: a key and its default in the reference block of "`config.toml`", a variable in the table of "Environment variables" (both checked) |
| an HTTP route or parameter, a `[server]` setting, a token or enrollment rule, the container | `docs/server.md` (a route in the table of "API" or "Admin API", checked against the OpenAPI goldens; the OpenAPI documents are generated, see [Generated files](#generated-files)); `docs/configuration.md` for a setting |
| a policy rule, action or identity source | `docs/access_control.md` (its policy examples must compile), and `docs/security_model.md` if what is prevented or detected changes |
| a key, base type, facet or check of `graph_cli.ir/1`, a change level of `ontology diff`, or when a check runs | `bundled_skills/ontology-best-practices/references/` of `graph_cli_admin` (`ir_format.md` is checked against the parser; the change levels and check timing are not) |
| a key, type, limit or check of `graph_cli.function/1`, or a rule publishing applies to functions | `bundled_skills/authoring-graph-functions/references/format.md` of `graph_cli_admin` (the format has one home there; `docs/cli_graph.md` and `docs/cli_graph_admin.md` describe behavior and link to it) |
| a key, type, limit or check of `graph_cli.action/1`, or a rule publishing applies to actions | `bundled_skills/authoring-graph-actions/references/format.md` of `graph_cli_admin` (the format has one home there; `docs/cli_graph.md` and `docs/cli_graph_admin.md` describe behavior and link to it) |
| a key, section kind, limit, bound or check of `graph_cli.view/1`, or a rule publishing applies to views | `bundled_skills/authoring-graph-views/references/format.md` of `graph_cli_admin` (the format has one home there; `docs/cli_graph.md` describes the commands and the behavior of a call and links to it) |
| a block, size budget or publish check of graph skills | `bundled_skills/authoring-graph-skills/SKILL.md` of `graph_cli_admin` (the rules have one home there; `docs/skills.md` describes the concept and links to it; `tests/skills/test_graph_skills_skill.py` pins blocks, reserved files and budgets) |
| a layer, package or module | `docs/architecture.md` (or `storage.md`, `audit.md`) and the import contracts |
| an on-disk format or a verification check | `docs/storage.md` or `docs/audit.md`; `tests/cli/test_storage_doc.py` checks that the directory tree of `storage.md` names every path of a real data root and that its manifest example matches the golden |
| an error code, finding code or policy action renamed or removed | every document that names it (checked) |
| an example graph's files or setup | `examples/<name>/README.md`; `tests/cli/test_company_example.py` keeps the example buildable |
| a way to run graph_cli, a core concept or a foundational design decision | `docs/deployment_options.md`, `docs/overview.md` or `docs/design_decisions.md` |
| install steps or the quickstart | `README.md`; `tests/cli/test_readme_quickstart.py` runs the quickstart's blocks on a throwaway data root and knows only the commands it uses (`uv run graph-admin`, `graph`, `mkdir -p`, a pointer file) |

`uv run pytest tests/test_docs.py` checks that every link and anchor
resolves, that `docs/README.md` lists every document and that the command
overviews in `docs/cli_graph.md` and `docs/cli_graph_admin.md` name exactly
the commands of `graph` and `graph-admin`, and only options those commands
take, that the layer diagram in `docs/architecture.md` matches the
layers contract of import-linter, that every screenshot marker is followed by
its image and every image in `docs/images/` is a used, small PNG, that
the transcripts run only real `graph` commands with options they take, that
the reference block of `docs/configuration.md` names exactly the keys of
`config.toml` with their real defaults and its tables exactly the
environment variables the code reads, that the route tables of
`docs/server.md` name exactly the paths of the OpenAPI documents, that every
error code, finding code and policy action a document names in a code span
exists (a dotted span that names a field instead goes into `NOT_CODES`), and
that every policy example in `docs/access_control.md` compiles.

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
placeholders outside the allowed set (`ALLOWED_PLACEHOLDERS` in
`tests/skills/test_app_skill_checks.py`), paths, ids and non-ASCII text. The
skill names no graph type.

The OpenAPI documents of the server are generated from the inventories with
output schemas and kept as goldens (`tests/goldens/server/`), so every API
change shows as a diff. A change to a command the server runs (its
parameters, errors or `output`) or to a shared definition changes them;
regenerate and review:

```sh
GRAPH_CLI_UPDATE_GOLDENS=1 uv run pytest tests/unit/server/test_openapi.py
```

The screenshots in `docs/images/` and the terminal transcript in
`docs/overview.md` are taken from the example graph by
`scripts/docs_screenshots.py`. It builds `examples/company` in a temporary
installation, starts `graph-admin view`, runs the transcript's commands and
takes the pictures with Playwright, which it installs on first use (the
dependency stays out of `uv.lock`):

```sh
uv run --with playwright==1.65.2 python scripts/docs_screenshots.py
```

Rerun it when the live view looks different, when the output of a command in
the transcript changes or when the example graph changes, and review the
images and the diff: ids and times differ on every run, so commit a rerun
only for a real change.

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
| CLI | `tests/cli/` | `graph` and `graph-admin` as subprocesses: commands, envelopes, exit codes, one scenario per app and admin code; the write guarantees through `graph` (failing audit sink, kill -9 then retry, parallel writers, nothing written by reads) |
| skills | `tests/skills/` | the committed `graph-cli` skill: freshness, docs-vs-code, leaks (also of every `skills emit` output, static and generated graph skills), structure; the hand-written admin skills: their YAML examples compile, their format references name every accepted key, their command lines name real commands and options (the files are listed below the table), structure |
| server | `tests/server/` | `graph-server` in-process (routes, auth, uploads, CORS, denials audited) and as a process (threads plus a `graph-admin` process lose no writes); the remote `graph` against stub servers (lost answers, retries, versions); every command answers the same locally and remotely on both toy ontologies; the admin API (token scopes, maintainers, no paths from requests) and every `graph-admin` command it runs answering the same from a remote-mode host |
| docker | `tests/docker/` | opt-in (`GRAPH_CLI_DOCKER_TESTS=1`): the container killed mid-write recovers, `audit verify` in it stays clean |
| integration | `tests/integration/` | opt-in (`GRAPH_CLI_TEST_S3_ENDPOINT` and the other variables the module names): stored files in a real S3-compatible store through both programs, local and remote mode (add, read, download, verify, gc, move both ways, quota, store check, wrong settings); `tests/integration/run_garage.sh` starts the pinned Garage image, creates a bucket, a key and a quota, and runs it |
| architecture | `tests/test_architecture.py`, `tests/test_source_rules.py` | import contracts with positive controls, source rules, wheel contents |
| docs | `tests/test_docs.py` | documentation links and anchors resolve (also inside the example's README and the admin skills), the docs index lists every document, the command overviews of `graph` and `graph-admin` match their inventories |

The admin skill tests are `tests/skills/test_ontology_skill.py` (YAML examples
of `ontology-best-practices` compile; its format reference names every key the
parser accepts), `tests/skills/test_functions_skill.py` and
`tests/skills/test_actions_skill.py` and `tests/skills/test_views_skill.py` (the same for
the function, action and view files of `authoring-graph-functions`,
`authoring-graph-actions` and `authoring-graph-views`),
`tests/skills/test_graph_skills_skill.py` (the blocks, reserved generated
files, size budgets and listing limits that `authoring-graph-skills` states
match the renderer and the publish check) and
`tests/skills/test_admin_skills.py` (every `graph-admin` and `graph` command
line in the admin skills names a real command and only its options). In the
checkout, link the admin skills for an agent working there (`.claude/` is not
tracked):

```sh
mkdir -p .claude/skills
for s in packages/graph_cli_admin/src/graph_cli_admin/bundled_skills/*/; do
  ln -s "../../$s" ".claude/skills/$(basename "$s")"
done
```

Crash and concurrency tests spawn processes and are marked `slow`; they run
by default. Storage tests build transactions with a
test-only builder (`tests/support/txbuilder.py`) driven by the compiled toy
ontologies. Storage tests use minimal audit bodies
(`tests/support/drafts.py`); tests that verify audit streams use real events
(`tests/support/audit_events.py`). Write service tests run on both backends
through `tests/support/services.py` (an in-memory catalog, a builder with
test clock and seeded ids, and a factory for valid creates of any class).
`uv run python scripts/bench_store.py` (not part of the suite) times reads
and writes of `FileStore` on a generated graph, with and without the
writer's full replay; with `--migration` it migrates a generated graph (sizes are
options; every class and relation moves to a new module) with real fsyncs
and prints the time of each phase and the peak memory.

### Which tests to run

The whole suite runs on every pull request in CI, and a green `check` is the
gate for merging. Locally, run it once before pushing, not after every edit:

| When | Run |
| --- | --- |
| while changing code | the tests of the area: its unit tests and the end-to-end tests of the commands it touches, e.g. `uv run pytest tests/unit/services tests/cli/test_app_read.py`; `--lf` reruns only the tests that failed last time, `--sw` stops at the first failure and starts there next time |
| a quick check across all layers but the end-to-end tests | `uv run pytest -n 4 --ignore=tests/cli --ignore=tests/server` |
| before pushing | the whole suite, `uv run pytest -n 4` (or more workers where the machine has the cores) |

There is no selection by changed files: the end-to-end tests in `tests/cli`
and `tests/server`, which take most of the suite's time, run `graph`,
`graph-admin` and `graph-server`, and those import nearly all of the code, so
a selector would keep almost the whole suite for any code change.

### Test harness

CLIs are tested as processes (`tests/conftest.py`: `run_cli`). Every run
gets a throwaway `HOME` and XDG directories, no `GRAPH_CLI_*` variables and no
`CLAUDE_CONFIG_DIR`, so tests never touch real user config, data, state or
installed skills.

Starting `graph` or `graph-admin` costs time, nearly all of it importing the
program, and an end-to-end test runs a program a dozen times or more. So on
Linux a run is forked from a launcher (`tests/support/launcher.py`):
one process per program and test worker that has imported the program's entry
point the way its console script does; each run is a child of it with the
run's arguments, environment, working directory and standard streams (pipes),
and ends with the run's exit status once its pipes are closed. No state of one
run reaches the next, because every run forks from the same untouched
launcher. That only holds while importing a program reads nothing of the
process (environment, home, working directory, clock, randomness, terminal)
and writes nothing: `tests/test_source_rules.py` checks the source,
`tests/test_harness.py` traces the import and compares forked runs with fresh
processes, and the launcher refuses to start when the import writes to stderr.

What a fork cannot give is a run's own interpreter start: a run whose
environment sets `PYTHON*`, `LANG` or `LC_*` differently starts as a real
process, and all runs of one launcher share a string hash seed, so a test that
checks that output stays the same between runs makes one of them fresh.
Tests that build their own command line call `launcher.run` instead of
`subprocess.run`. `run_cli(..., fresh=True)` starts a real process, as
`tests/test_smoke.py` does; `GRAPH_CLI_TEST_FORK=0` does so for every run
(and is what happens on macOS, where `fork` without `exec` is not safe).

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
the role-based and contract tests run on the single-module toys only), a fourth with a
version series for data migrations (`workshop`: 1.0.0 is one module, 2.0.0 an umbrella with two
areas, classes and relations moved by `renamed_from` CURIEs; used by
`tests/cli/test_migration_end_to_end.py` and not part of the toy lists), one broken ontology per `model.*` finding
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
Two more single-module toy ontologies carry markings for the field visibility
tests (`personnel` with a version series, and `staff`, whose twin in
`tests/fixtures/markings/` is the module without everything a caller holding no
marking may not see); they are listed in `MARKED_ONTOLOGIES`
(`tests/support/fixtures.py`), not in the toy lists.
`tests/fixtures/files/` is the golden corpus of the file extractors: tiny files of neutral
content (a PDF, an encrypted and a damaged one, images, CSV and TSV variants, SQLite databases
including one with hostile object names, a text file renamed `.pdf`, a binary file named `.csv`,
...), described by `manifest.json` and rebuilt by `generate.py`. `tests/goldens/extractors/
<name>@<version>.json` pins every extractor's output per version, entry by entry: an existing entry never
changes (output that changes without a version bump of the extractor fails the suite; a library,
interpreter or SQLite upgrade that changes it bumps the version), and `GRAPH_CLI_UPDATE_GOLDENS=1`
only adds the entries of new corpus files or the golden of a new version.
Core tests never name a fixture type; a leak test keeps fixture vocabulary out
of the packages. Deterministic test doubles (`FixedClock`, `StepClock`,
`SeededIdProvider`) live in `tests/support/`. Golden files live in
`tests/goldens/` and next to the fixtures they describe.
