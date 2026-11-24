# Design decisions

This page lists the deliberate, foundational choices behind graph_cli: what
was decided, why, and what each choice gives and costs. It explains the
reasoning; it is not a feature list or a changelog. The details of every
topic live in the linked documents.

## Using a graph and maintaining it are separate

**Decision.** `graph` reads and writes the data of existing graphs.
`graph-admin` creates graphs, releases ontologies, installs policies and
skills, publishes functions and actions, and verifies the audit trail. They
are separate distributions: the code of `graph` contains no maintenance
code, `graph-admin` is not installed on `PATH`, and the policy never grants
maintenance actions to `graph` or to an app token on the server.

**Why.** An agent that works in a graph should not be able to change the
rules it works under: the schema, the policies and the instructions it
follows. Splitting by role keeps the everyday tool small and lets
maintenance be done with more care.

**Trade-off.** You get a boundary that is checked in code, not only agreed
on. You give up some convenience: there are two tools, and a new graph or a
schema change always goes through a maintainer. On one machine both tools
run as the same OS user, so locally the split is a guardrail. It becomes a
real boundary when a server owns the data ([Security
model](security_model.md#prevention)).

## The core knows no domain

**Decision.** The core handles entities, relations, attributes, lifecycles,
revisions and files, and nothing about any subject. What a graph is about is
written in its ontology. A few generic standard modules (provenance, files)
ship with the project, and a graph imports what it needs. Each purpose gets
its own graph, with its own ontology, versions, policy and lock, instead of
one large graph for everything.

**Why.** Many different uses (notes, inventories, organization structures,
research records) should be built on the same tool without any one of them
shaping it. A core shaped by one subject becomes awkward for all the others.

**Trade-off.** You get a tool that fits unrelated subjects. You give up
built-in domain types: every graph needs an ontology before it is useful,
and domain shortcuts come from per-graph [functions and
actions](#named-functions-and-actions-beside-free-form-access), not from the
core.

## Every write is a validated change set

**Decision.** Every change to data, from a single `create` to a large batch,
is a change set that takes one path: authorization, validation against the
graph's ontology, audit, commit ([the write
path](overview.md#how-the-parts-fit-together)). A change set commits
completely or not at all. A write must fit the ontology (types, attributes,
relations, cardinalities, lifecycle transitions, uniqueness, rules); what it
does not describe is refused, with findings that say what to fix. Dry runs,
idempotency keys, expected revisions and reasons belong to the same path.
Nothing is edited in place: each change creates a new revision, and entities
are retracted or superseded, not deleted ([Write
pipeline](architecture.md#write-pipeline)).

**Why.** Agents write as often as people, and they make mistakes. If invalid
data cannot get in, readers can trust what is there, and a refusal is a
precise message an agent can act on. One path means every guarantee
(validation, access control, audit) holds for every entry point: the CLI,
the server, named actions and imports.

**Trade-off.** You get consistent data and safe retries. You give up
free-form storage: structure comes first, every write pays for validation,
and data that does not fit waits until the ontology does.

## Ontologies are released with semantic versions

**Decision.** An ontology (`graph_cli.ir/1`) changes only through a release
by a maintainer, and a graph is pinned to one release. Releases follow
semantic versioning: the tool classifies each change as patch, minor or
major and refuses a release whose version bump is smaller than the changes
require. A major release can carry a migration that rewrites existing data
to the new names and shapes in one transaction, keeping ids, history and the
audit trail. A release also checks that the graph's published functions,
actions and views still fit ([Releasing with a data
migration](cli_graph_admin.md#releasing-with-a-data-migration)).

**Why.** Data, catalogs and skills all depend on the names in the ontology.
Without explicit versions, a rename silently breaks them. With a pin, every
revision belongs to a known model, and older revisions can still be read
under current names ([Earlier states](cli_graph.md#earlier-states)).

**Trade-off.** You get predictable change and readable history. You give up
quick edits: changing a live model takes a maintainer, a version and
sometimes a migration, and a major release is refused while the existing
data would have errors under the new version.

## One transaction log, plain local files

**Decision.** Each graph keeps a hash-chained log of its transactions as the
single source of truth. Current-state files, the checkpoint and the index
are derived from it and can be rebuilt. One writer at a time holds the
graph's lock; readers take no lock and see a consistent snapshot. The store
is ordinary files on a local POSIX file system, so no database server is
needed. Stored files are addressed by content hash and never change. By
default their bytes live in the data root; per graph, they can live in an
S3-compatible object store instead. Only the bytes move, and the object
store sees no names or metadata ([Storage](storage.md#filestore), [Object
storage](files.md#object-storage-optional)).

**Why.** One append-only record gives history, reads of earlier states,
audit cross-checks and recovery from a single mechanism, and derived parts
can be replaced without migrating data. The store relies on file locks,
synced appends and atomic renames on a local file system; network and shared
file systems are not supported. Sharing goes through the server instead (see
[One command surface](#one-command-surface-for-the-cli-and-the-server)).

**Trade-off.** You get a setup that is simple to run, back up and verify.
You give up several things:

- Writers to one graph wait for each other.
- The data root must be local.
- Recorded data cannot be erased from the history.
- With an object store, backups cover two places, and writing a file entity
  needs the store to be reachable.

## The audit trail is append-only and verifiable

**Decision.** Every write and every refusal is recorded as an event in a
hash-chained stream, with the intent written before the commit. Each graph
has its own stream; installation-wide events (graph creation, denials,
anchors) go to a separate one. `graph-admin audit verify` checks the chains
and cross-checks them against the transaction logs, installed policies,
tokens, ontology pins and stored files, and reports damage as findings
([Audit](audit.md#verification)). Events hold ids, types, hashes and
reasons, never attribute values. Reads are not in the chain; an optional
[access log](audit.md#access-log) records them.

**Why.** "Who changed what, why, and under which policy" should be
answerable without trusting the writer. Chaining makes edits and deletions
detectable, and keeping values out means reading the audit does not reveal
graph data.

**Trade-off.** The audit detects tampering but cannot prevent it. A
consistent rewrite of every stream goes unnoticed unless a head is kept
outside the data root ([Security model](security_model.md#detection)). Reads
are not audited by default, and every write pays for an extra synced append.

## Access is decided by policy, and the limits are stated

**Decision.** Two policy layers decide each request on an action and a
resource. An installation policy grants a level (none, read or write) per
principal and narrows it per project. A graph policy narrows further inside
one graph: read-only or hidden types, obligations such as a required reason,
and deny rules. An explicit deny wins and the default is deny. Finer tools
(write allow lists, read limits, markings, roles) refine this. A graph the
caller may not see answers as if it did not exist, and refusals are audited
([Access control](access_control.md#policy-layers)).

**Why.** A person, a coding agent and a read-only reporting app should have
different rights on the same graphs. The rules should be data that can be
reviewed, compared and verified.

**Trade-off.** What a policy can enforce depends on who verifies the caller.
Locally every agent runs as the same OS user and only the program is
verified, so policies are a guardrail plus detection. When `graph-server`
owns the data, callers hold only a token and the same policies prevent.
Agent and session names stay asserted by the client either way. A server
grants nothing until a policy does.

## One command surface for the CLI and the server

**Decision.** Commands are declared once, as data. The CLI, the server
routes, the OpenAPI document, `graph describe commands` and the generated
agent skill all derive from these declarations. `graph-server` serves the
commands of `graph` over HTTP with bearer tokens. In remote mode, `graph`
sends each command the server offers to it and prints the answer as it would
locally, with the same envelope, text and exit codes
([Server](server.md#remote-mode-of-graph)). To use one graph from several
machines, run the server where the data lives and use remote mode elsewhere
([Deployment options](deployment_options.md)).

**Why.** One definition keeps the CLI, the API and the documentation from
drifting apart. Sharing needs no second storage mechanism: the data stays on
one machine under one lock, and every other machine reaches it through an
API that checks each request.

**Trade-off.** You get little duplicated code, and remote mode costs agents
nothing to learn. You give up a resource-style API, because the API follows
the commands and their parameters. Data is not replicated across machines.

## The output is built for agents and people

**Decision.** Every command prints one envelope: readable text by default,
one JSON document with `--json`. It carries the result, paging, hints for
the next step, warnings and a structured error with a code, a category and a
matching exit code. An answer too large for an agent's context is written to
a file, and the output says where ([Output and exit
codes](cli_graph.md#output-and-exit-codes)).

**Why.** Coding agents read command output as context. It has to be compact,
predictable and recoverable from errors without a human. People want the
same text, so one shape serves both and no separate machine interface is
needed.

**Trade-off.** You get output agents can chain with little guidance, and
stable error codes. You give up completeness per call: answers are lean,
long results are paged or spilled to a file, and a caller may need a second
call. New keys may appear within a version of the envelope, so consumers
must ignore keys they do not know.

## Agent skills are generated, and the tool teaches the rest

**Decision.** The skill for `graph` is generated from the command
declarations and never edited by hand. A graph skill is a template the
maintainer writes; marked blocks in it are filled from the graph's current
ontology and catalogs when it is installed. An installed copy records what
it was made from. When the program, ontology or catalog changes, the copy is
stale, writes are refused until it is refreshed, and `graph start` refreshes
it ([Graph skills](skills.md#graph-skills), [Stale
skills](skills.md#stale-skills)). The skill says what a graph is for and
when to use it. The detail comes from the tool on demand: the `describe` and
search commands show modules, types, functions and actions step by step, so
no agent has to load a whole ontology.

**Why.** Hand-written descriptions of a tool or a schema go stale and then
mislead the agent that follows them. Generation ties the text to what it
describes. A small skill with detail on demand keeps working as ontologies
grow.

**Trade-off.** You get skills that match the program and the model. You give
up some autonomy: curated text still needs a maintainer and is checked when
published, and agents must ask for detail instead of seeing everything up
front. A stale skill blocks writes until `graph start` has run.

## Named functions and actions beside free-form access

**Decision.** A maintainer can publish named, typed functions (reviewed
read-only queries) and actions (reviewed writes: a template of change set
operations with typed parameters and preconditions on the current state) per
graph. Agents find them by describing what they want, run them with `graph
run` and `graph do`, and are guided to prefer them
([Functions](cli_graph.md#functions), [Actions](cli_graph.md#actions)). They
sit beside the general tools: reads can still use `find` and a read-only
Cypher query, and writes can still use the primitive commands and change
sets. Actions compile to the same change sets, so validation, policy and
audit apply unchanged.

**Why.** Questions and changes that recur should not be rediscovered each
time. A named query or action is shorter and safer, and it can state a
precondition the raw writes cannot. The free-form path keeps unforeseen
questions answerable.

**Trade-off.** You get fewer steps and mistakes, and one audited intent per
action. You give up some effort: someone has to write and maintain the
catalog, and a release is refused when an entry no longer fits the ontology.
Nothing forces writes through actions: what an agent may write is limited by
policy, not by the catalog.

## Layers with one-way dependencies

**Decision.** The code is organized in layers, with a small foundation at
the bottom and the command layers on top. Dependencies point downward only.
Import contracts checked by the test suite enforce this and further rules:
the application distribution never imports the maintenance or server code,
storage, audit and access control depend only on the foundation, and only
the command layers use the CLI framework
([Architecture](architecture.md#layers)).

**Why.** The separation of usage and maintenance, replaceable storage, and
the independence of audit and policy are only as real as the dependencies
that carry them. Rules checked by tools do not erode.

**Trade-off.** You get parts that can change independently and boundaries
that hold as the project grows. You give up shortcuts: new code must fit a
layer, cross-layer imports are refused, and a small feature can touch
several layers.
