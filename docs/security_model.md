# Security model

Locally, every agent runs as the same OS user, and only the program
(`graph` or `graph-admin`) is a verified difference between callers. The project,
agent and session are asserted, so locally [access control](access_control.md)
is a guardrail plus detection, not prevention: it stops well-behaved agents
and mistakes (the wrong project, the wrong graph, a write where only reading
was meant), but an agent with a shell can edit the files. The project
selects a scope rather than only narrowing one: a pointer that names a
project gets that project's scope.

## Detection

Detection covers policy files edited, placed or deleted by hand and
decisions made under another policy (see
[Audit: policy verification](audit.md#policy-verification)), but not a
consistent rewrite of every stream (all tails, all hashes); that needs a
head kept outside the data root. <!-- claim: security_model.policy_edits_detected --> Reads trust the derived files (revision
files, the checkpoint, the index) and check the transaction log only
after them, so an edited derived file changes what reads return until
`graph-admin audit verify` reports it and `graph-admin graph reindex`
rebuilds it (see [Storage](storage.md#filestore)). <!-- claim: security_model.derived_files_rebuilt --> Writers trust the index
only after its SHA-256 digest of the transaction log matches and check
what they copy from it, so an edited log is found by the next writer; an
index edited consistently with its digest is found by `audit verify` and
replaced by the full replay a writer runs regularly
([Storage](storage.md#filestore)). <!-- claim: security_model.writers_check_the_index --> A data migration (a maintainer's release
that rewrites entities
as new revisions) is announced by its own intent, and `audit verify` checks the
transaction against it: the identity every revision keeps, the versions, a digest
of the revisions and the pin that follows
([Audit: migration verification](audit.md#migration-verification)). <!-- claim: security_model.migration_verified --> Reads are not audited, so a read under
another policy leaves no trace in the audit; the opt-in
[access log](audit.md#access-log) records reads, but it is not chained
and can be edited. <!-- claim: security_model.reads_not_audited -->

## Prevention

Prevention needs the data out of the agents' reach. With
[`graph-server`](server.md) owning the data root (in the container, or as
another OS user), callers hold only a bearer token: they cannot edit the
files, the policy or the audit, and the same policies decide. What it
prevents depends on what is verified:

- The token is verified: a grant or deny rule on `principal_id:
  token:<name>` holds for every request with it. An app token reaches no
  maintenance action; an admin token (scope `admin`, only with the
  [admin API](server.md#admin-api) turned on) reaches only maintenance, as
  one of the policy's maintainers. Treat an admin token like a shell in
  the container: whoever holds it can change policies, tokens and
  ontologies, and every change is audited under its name. <!-- claim: security_model.token_scopes --> An admin token
  bound to a project is narrower: it maintains only the graphs the
  project's scope says `maintain` for (release, publish, push, check,
  reindex) and sees no other graph; a hard-coded ceiling keeps it from
  every installation and destructive action and from the maintainer
  rules, so it cannot release with a data migration (`maint.migrate`)
  ([Project admins](access_control.md#project-admins)). Whoever
  holds it can still change those graphs' ontology, skill, named
  functions and named actions, and reads their data through check findings.
  The skill, the functions and the actions steer every agent that uses the
  graph, in every project:
  `maintain` on a graph other projects read lets one project's admin write
  instructions for their agents and choose which queries and which reviewed writes they run.
  Tokens whose grants name entries ([catalog grants](access_control.md#catalog-grants)) run exactly
  what the catalog then says under that name, so whoever may publish a graph's catalog defines what
  a catalog grant lets a token read and write, though not which names it holds. A publish that
  removes or changes an entry a grant names warns (`catalog.granted_entry_removed`,
  `catalog.granted_entry_changed`, [republishing](access_control.md#catalog-grants)) but is not
  refused: the grant names, it does not pin, so the warning and `policy explain` (the entry's
  current hash) are what a maintainer has until finer publish rights exist.
  <!-- claim: security_model.project_admin_ceiling -->
- A token bound to a project (`token create --project`) verifies the
  project, so that project's scope is a boundary for the token, and a
  grant to the project (`principal: {project: P}`) reaches it; without a
  binding the client asserts the project, the scope stays a guardrail and
  no project grant or `maintain` level applies. <!-- claim: security_model.project_bound_tokens -->
- Roles ([Roles](access_control.md#roles)) are verified only through a
  server token: the server reads them from the token record, a client cannot
  assert them, and a local invocation has none, so a rule naming a role holds
  only with the server. Assigning roles is maintenance
  (`maint.token_update`, `graph-admin token role add` and
  `graph-admin token role remove`, `--role` of `graph-admin token create`
  and `graph-admin enroll create`), so whoever holds an admin token can widen what an app
  token may do within what the policy grants to roles; an app token cannot
  assign any, and a project admin is kept from it by its ceiling. A role
  change counts from the next request, as a revoke does, and every change is
  audited ([Audit](audit.md#token-verification)). <!-- claim: security_model.roles_verified -->
- Catalog grants ([Catalog grants](access_control.md#catalog-grants)) limit a server
  token to the published functions, actions and views its grant names: it runs them with the
  entry's own rights, over its own read view, so a function or view reads what the token could
  not `get` or `find` by hand (an action writes, see the next item), while every primitive,
  the card and every entry the grant does not name stay refused or unknown. They hold like any
  rule: only through server tokens (the grant needs `entrypoint: server`, since a local agent with
  a shell reads the files). What an entry needs is decided deny-only: an explicit deny rule, a
  scope, a type limit or a ceiling still refuses it, and the audit of a refusal names the entry,
  and the grant when the entry decision allowed it, next to the rule that denied. An entry
  outside the grant is `not_found`, as for a name never published. Whoever may publish the
  graph's catalog decides what a granted name reads (see the project admin item). <!-- claim: security_model.catalog_grants -->
- An action a catalog grant names writes through the grant ([Actions](access_control.md#actions)):
  `graph do` of it is decided by the token's own rights first and, where they lack an allow, by the
  entry, `write.action` on the action's resource and then every op of its expansion deny-only. A
  token therefore writes what the action's template says although it could not write it by hand, so
  that a reviewed action can be the one way to change a type, while every deny rule, scope, type
  limit, term or agent-type rule, ceiling and obligation still refuses the whole action
  (`access.denied`, nothing is written), and a type or attribute the token does not see is never
  written (its decision is the one on a resource no rule names, which no grant allows). The
  decisions are made again under the lock after a policy refresh and in a dry run. The audit event
  of the write names the action (name and hash) and, in every decision, the grant that let the step
  through; a refusal of an action the caller sees names it too. For a token limited to its catalog,
  an action the grant does not name is `action.not_found`; a reader with `catalog.actions` keeps
  its answer for the others. The action's template, preconditions, outputs and refusals are a
  security boundary for these callers: whoever may publish the graph's actions decides what a
  granted name writes (see the project admin item). <!-- claim: security_model.catalog_actions -->
- A caller limited to a catalog cannot list the graph through the one thing it may ask for,
  entities by name, except by trying aliases (the residual below)
  ([References and names](access_control.md#references-and-names-for-a-caller-limited-to-its-catalog)):
  a `ref` parameter takes a full id or an alias and never the trailing hex digits of an id (so no
  answer lists the ids that share a suffix), an id of another class or of a type it does not see
  is `entity.not_found` like an id that does not exist (no class, no id), and its `lookup` is the
  exact match of a name for the parameter of an entry it may use: first page, active records,
  candidates of that parameter's class among the types it sees, no count of other classes, no
  prefix, no paging, with `read.find` asked deny-only. A plain lookup, `search` and `find` stay
  refused, and so does every lookup that is not of that shape.
  <!-- claim: security_model.catalog_no_enumeration -->
- Write allow lists ([Write allow lists](access_control.md#write-allow-lists))
  narrow what a subject may write to the attributes and relations its grant
  names. They hold like any rule: only through server tokens, where the
  caller cannot edit the policy or the data; locally they are a guardrail that
  stops well-behaved agents and mistakes, and an agent with a shell can edit the
  files. Decisions on attributes and relations are audited like decisions on
  types: the events of a write and of its refusal record them, with their
  resources, and `audit verify` checks their policy hash as for any decision. A
  `writes` grant cannot exceed what scopes and deny rules allow, because they
  narrow it and an explicit deny wins. Reading is not limited by it: whoever
  may write a type may still read what the read rules leave, which is all of the
  graph unless it has read limits (next item). <!-- claim: security_model.write_allow_lists -->
- Read limits ([Read limits](access_control.md#read-limits)) hide whole types
  from a subject: for it the graph reads as if their entities, relations and
  names did not exist ([What a caller can
  see](access_control.md#what-a-caller-can-see)). They hold like any rule: only
  through server tokens, where the caller cannot edit the policy or the data;
  locally they are a guardrail that stops well-behaved agents and mistakes,
  and an agent with a shell can read the files. They narrow inside a graph the
  caller may see; whether a graph is visible at all (`graph.not_found`) is
  unchanged. The maintainers, the `graph-admin` commands, project admin tokens
  and the [live view](live_view.md) are not limited: they read everything
  because they maintain the graph, so their findings, usage report and
  views show hidden data. What a limit still shows is listed under [Type read
  limits](#type-read-limits). <!-- claim: security_model.read_limits -->
- Markings ([Markings](access_control.md#markings)) hide single attributes of a
  visible type from a subject that does not hold them: for it the attribute
  reads as if the type did not declare it. They hold like read limits: only
  through server tokens; locally a guardrail, since an agent with a shell can
  read the files. The maintainers, the `graph-admin` commands, project admin
  tokens and the live view read raw data. What markings still show is listed
  under [Field visibility](#field-visibility). Programs before 0.23.0 refuse the
  compiled ontology that carries markings (schema `/2`), so no older server or
  client, an image rollback included, ever loads a model with markings and shows
  the attributes. <!-- claim: security_model.markings -->
- Enrollment codes ([Enrollment](server.md#enrollment)) are bearer
  secrets too, but narrow ones: one use, a short lifetime, always bound to
  a project, so a code never yields an unbound token (a maintainer's). Who
  holds a code gets that project's token, app or project admin, with the
  roles of an app code, as the maintainer chose; hand it over like a password, out of band, and revoke
  it (`enroll revoke`) when unsure. The holder picks only a label: the
  token is named `<project>.<label>` and never takes a name a policy rule
  grants something to. The exchange needs no token, answers the same for
  every code it refuses, takes no lock for a wrong code and never logs a
  code; it is audited under the enrollment's id. Revoking a maintainer's
  token leaves the codes it created valid (`enroll list`). `graph connect`
  checks the client's folder before it spends the code and keeps the token
  outside the folder, which may be in git, per server, so a token is never
  sent to another server. <!-- claim: security_model.enrollment_codes -->
- Agent, session and device are still asserted by the client: deny rules
  on them stop well-behaved clients only. <!-- claim: security_model.asserted_facets -->
- The transport: the server speaks plain HTTP on `127.0.0.1`; beyond the
  machine it belongs behind TLS (the compose `tls` profile), and the
  client refuses plain HTTP to another host unless told otherwise. Tokens
  are stored as hashes and never logged or audited; a request without a
  valid token is logged but reaches nothing. <!-- claim: security_model.transport -->
- Program updates: `graph self update` installs the wheel the server hands
  out after checking its hash against the server's manifest. Both come
  over the same connection, so the check catches a broken transfer, and
  TLS is what keeps them authentic: `self update` installs nothing over
  plain HTTP to another host. Whoever can replace the wheels in the image
  or the server's answers runs code on every client that updates (the
  wheels are not signed: trusting the server is trusting its code). The
  `graph` wheel is public (program code without data); the `graph-admin`
  wheel needs an admin token ([Self update](server.md#self-update)). <!-- claim: security_model.self_update -->

Whoever can run `graph-admin` where the data lives (`docker compose exec`),
or holds an admin token of a server with the admin API on, remains the
maintainer. Without a server, a coding agent's own
permissions narrow the normal path further, for example in a project's
Claude Code settings:

```json
{"permissions": {"deny": ["Bash(graph-admin:*)", "Edit(.graph_cli.toml)", "Write(.graph_cli.toml)"]}}
```

plus `Edit`/`Write` denies for the data root. Such settings are not
installed by `graph`.

## Type read limits

A limited caller is answered as if the hidden types did not exist
([What a caller can see](access_control.md#what-a-caller-can-see)). The
protection is complete for data: no entity, id, alias, label, value, edge,
count or term of a hidden type reaches it through a read command, a file
download, a function, an action, the generated skill or `whoami`; a function or view a catalog
grant names runs over the same limited snapshot and model, so it too returns visible entities only.
<!-- claim: security_model.type_limits_complete --> The tool manifest
([Tools for agent harnesses](cli_graph.md#tools-for-agent-harnesses)) is rendered the same way: it lists only
the functions, actions and views the caller may use (and the `lookup` tool only when the caller is
served one: it may find entities, or its catalog grant names an entry with a reference input and
nothing denies `read.find`), and its schemas hold the enum values, classes and entity attributes of
the model the caller sees, so a hidden type, attribute or marking never reaches it, and an entry
the caller sees but may not use is absent. <!-- claim: security_model.tool_manifest_per_caller -->
A call of a tool (`graph call`) is no way around that: it finds the tool among the entries the caller sees (one it
does not see is `tool.not_found`, one it may not use is `access.denied`, recorded) and runs the service
of the command behind it, so who may do it, what is refused and what the audit records are the
command's. <!-- claim: security_model.tool_call_is_the_command --> What is left
tells that something exists or that something happened, never what it is. These
are accepted and known:

- **Integrity refusals tell that something hidden exists.** Validation judges the
  whole graph, because judging the filtered state would let a caller write
  data that breaks the graph. A write can therefore be refused because of what
  the caller cannot see: a unique value, an alias or the exact bytes of a file
  held by a hidden entity, a retract blocked by a hidden referrer, a cycle
  through hidden entities, a cardinality that hidden edges have reached, a rule
  that counts a relation reaching a hidden type. The refusal keeps its code and
  severity and says "conflicts with data you cannot see"; it never names the
  entity, type, relation or value, and `file add` of bytes a hidden file holds is
  a concealed `unique_violation` on the content, never `file.already_present`
  with the holder. This is the covert channel of row-level security in
  databases (PostgreSQL documents the same for unique constraints). A caller
  can probe for the existence of a value by trying to write it; it learns
  nothing else. <!-- claim: security_model.integrity_refusals_concealed -->
- **A required relation with a hidden range blocks creates.** When a type must
  have an edge to a type the caller cannot see (a cardinality minimum on a
  relation whose range is hidden), every create of that type is refused for the
  caller, because it cannot give a target. Hide such types together, or give
  the caller a role that sees the range. `policy check` warns about it for a
  grant's own `reads` (`admin.policy_unusable_write`); it cannot see what other
  grants of the same caller add. <!-- claim: security_model.required_relation_hidden_range -->
- **`graph view` is a `read.get` read.** A caller who may read an entity may read its
  card: counts of its relations and the first neighbours with their labels, even where
  a rule denies `read.traverse` (what `graph neighbors` needs). Types and attributes
  the caller cannot see stay out of the card like out of every read.
  Published views follow the same rule with two additions: a set view also needs `read.find`, and a
  view that names anything the caller cannot see is not found at all, so a view definition never
  shows a hidden term ([Views](access_control.md#views)). A view a catalog grant names needs none
  of these as rights of the caller (they are asked deny-only), which adds one accepted
  point: `graph view REF` answers with the default view where the entity's class has a granted
  default view and with the one refusal of the card otherwise (the entity of a granted entity view
  that is of another class is `entity.not_found`, like an unknown id).
  <!-- claim: security_model.view_is_read_get -->
- **A function, action or view the caller may not use is named.** One that names only what the
  caller sees but needs a right it lacks is listed as not allowed, with its name, summary, `use_when`,
  parameters and the right that is missing; the maintainer's query, operations, conditions, sections
  and literals stay out, and anything that names a hidden term stays absent. So a caller learns
  that a reviewed function, action or view exists and what it is for, never its operations,
  conditions or literals; `missing` names the right and the visible type, attribute or relation
  it is needed on
  ([Functions, actions and views you may not use](access_control.md#functions-actions-and-views-you-may-not-use)). <!-- claim: security_model.not_allowed_named -->
- **A granted view names the function its rows run.** A caller limited to its catalog runs the
  `rows` functions of a view it is granted without being granted them (the view is the reviewed
  boundary), so the view's own surfaces name them: `describe view`, the view's tool in the
  manifest, the section of the answer and the `view.function_deprecated` warning. No listing,
  search, count, skill function document or `whoami` of that caller names a function its grants
  do not name, and `run` of it is `function.not_found`. <!-- claim: security_model.view_names_row_functions -->
- **An action a catalog caller runs shows what it touches.** The refusal of a failed precondition
  quotes the actual values of the attributes it checks on the entities the caller named, an
  integrity refusal names the visible data it conflicts with, and the answer names the ids of what
  the action created or changed: the details a caller with write rights gets, now also for a token
  that could not read those entities by hand (of types it sees). They are not filtered, because an
  agent needs them to correct a call; an action's `require` conditions are written with that in
  mind. <!-- claim: security_model.catalog_action_residual -->
- **A catalog caller can walk aliases and sees facts.** An alias it tries is accepted or
  `entity.not_found`, and aliases are minted in sequence (`DEPT-0001`, `DEPT-0002`, ...), so it can
  walk the aliases of the class of a reference parameter of any entry it may use (a `many`
  parameter takes a list per call) and read through the entry what the entry returns for each
  entity; this lists the entities of that class it sees, bounded by the types of its `reads`, and
  allowed reads are not audited, so a walk leaves no trace. Narrowing it is a later decision (an
  audited refusal, a rate limit, a grant that refuses aliases). The candidates of its narrow
  lookup also carry `facts` when a name has several, the labels of the neighbours that tell the
  homonyms apart, so it learns the names of the neighbours of the entities it finds by name, all
  of a type it sees. <!-- claim: security_model.catalog_lookup_residual -->
- **Activity shows, data does not.** The envelope's `seq` counts every
  transaction of the graph, hidden ones included. A revision whose only change
  was an edge to a hidden entity appears in `history` with an empty `changed`.
  The `reason` of a revision is the writer's text and shown as written, and so
  is the `action` name of a history item, even when the action is hidden from
  the caller. The `superseded_by` of an entity that a
  hidden entity superseded is empty. Timing differences are not hidden either. <!-- claim: security_model.activity_shows -->
- **Revision numbers can have gaps.** A revision of a hidden type that an entity had
  before a `retype` moved it into a visible type is absent for the caller, and the
  numbers of the others are as stored, so a gap in `history` or a `get --rev` that
  answers `entity.revision_not_found` below the first revision shown tells that such
  revisions exist, never what they held
  ([Time](access_control.md#what-a-caller-can-see)). <!-- claim: security_model.revision_gaps -->
- **Edges of a past revision are judged by the target's type now.** A visible entity's
  earlier revision keeps an edge to an entity that has a visible type now, also when that
  entity had a hidden type at the time of the revision (a `retype` moved it since). The
  edge shows that a link to something existed then, and the target is one the caller can read
  today. <!-- claim: security_model.past_edges_judged_now -->
- **A class the model dropped judges nothing.** Revisions that an entity had under a class
  the pinned model no longer has (a `retype` moved it, a later release removed the class) are
  judged by the class the mapping shows, as the class the entity has now. A caller who may
  see that class reads those revisions, the values in them included, although it could not
  have read them as the dropped class, whose visibility no policy can decide. <!-- claim: security_model.dropped_class_judges_nothing -->
- **The change feed shows the same activity.** `graph changes` lists only the
  entities the caller sees, and a transaction with none has no entry, so the
  `tx_seq` of its entries have gaps; a gap tells that a transaction happened
  that the caller does not see, as the envelope's `seq` does. A transaction that changed visible and hidden entities appears
  with the visible ones and its `command`, `reason` and `action` as written. A
  revision whose only change was an edge to a hidden entity or a hidden
  attribute is an item with an empty `changed` and no hidden id as a target.
  The entry of a data migration of an ontology release always appears, with its
  versions and its summary (prose, not filtered), even when the caller sees none
  of its entities. A consumer's
  projection built from the feed holds what its caller sees, so it is served
  only to callers with the same `view` digest
  ([Changes since a position](cli_graph.md#changes-since-a-position)). <!-- claim: security_model.change_feed_activity -->
- **Prose is not filtered.** The summary and description of an ontology term
  that stays visible, the text of a curated graph skill and the `reason` of
  a change are text the maintainers and writers wrote, shown as they wrote
  them. If that text names a hidden type, the caller reads the name. Write
  ontology prose and curated skill text so that it holds for every reader
  ([Skills](skills.md#per-reader)); the generated blocks are rendered per reader
  and name nothing hidden. <!-- claim: security_model.generated_blocks_per_reader -->
- **What it does not cover.** Rows of a visible type are visible to whoever
  sees the type; read limits stop at whole types (single attributes are
  [markings](#field-visibility)). A type cannot be write-only: a caller writes
  only types it sees. <!-- claim: security_model.writes_only_visible_types -->

## Field visibility

A caller that lacks a marking is answered as if the attribute did not exist
([What a caller can see](access_control.md#attributes-a-caller-lacks-a-marking-for)).
The protection is complete for values and names: no value, attribute name,
marking name, enum value or datatype only hidden attributes use, rule, function,
action or skill text of a marked attribute reaches it through a read command, a
write answer, `whoami`, `describe` or the generated skill, and a write that
names the attribute, a guess of its current value included, is answered as a
write of an attribute the type lacks. <!-- claim: security_model.field_visibility_complete --> What is left tells that something is
marked or that something happened, with one exception that an author controls
(the first item). These are accepted and known:

- **Rules, guards and preconditions over a marked attribute are a channel.**
  Validation judges the whole graph, so a rule or lifecycle guard that reads a
  marked attribute can refuse a caller's write or transition. The finding is
  concealed (its code, the target entity and a path that names a visible
  attribute stay, the operands and the rule are dropped), and the caller sees
  no rule text, yet dry runs are not audited: a
  caller who can change the inputs of such a rule (link members into a `sum`
  over a hidden number, say) can repeat dry runs and narrow the hidden value
  down. The transition that such a guard blocks is listed as allowed. This is
  not one bit but an amplifiable channel, and it exists only where an author
  put a marked attribute into an expression: the release warns
  (`admin.marking_in_expression`) and the ontology guidance advises against it.
  A precondition needs no extra care for callers that see its action: an action
  whose precondition reads a hidden attribute is hidden. <!-- claim: security_model.marked_rules_are_a_channel -->
- **Activity shows, values do not.** A revision whose only change was a hidden
  attribute appears in `history` with an empty `changed`; the entity's `rev`,
  `tx_seq` and `recorded_at` count it, so they jump without a visible reason,
  and a selection by the transaction that last changed an entity matches an entity
  changed only in a hidden attribute. The same `rev` shows in write results. The
  `reason` of a revision is the writer's text, shown as written, and so is a
  free-text alias.
  The change feed lists such a revision as an item with an empty `changed`, and
  a transaction that only changed hidden attributes of entities the caller
  otherwise sees is an entry like any other. <!-- claim: security_model.marked_activity_shows -->
- **`whoami` tells that something is marked.** `markings: []` says that the graph
  uses markings and the caller holds none, `markings: [...]` which of the
  markings in use it holds. A marking the caller lacks is never named. A caller
  with an attribute view is also answered through the early refusal for a name
  the type truly lacks (no other findings of validation ride along), which tells
  the same single fact. <!-- claim: security_model.whoami_tells_marked -->
- **Integrity refusals tell that a hidden value exists.** The findings of
  [Type read limits](#type-read-limits) apply to attributes too, with the
  code and severity kept and the path cut to `attrs`: a marked required
  attribute without default blocks every create of its type for a caller that
  lacks the marking (`missing_required`; release warns with
  `admin.marking_blocks_create`, `policy check` with
  `admin.policy_unusable_write`); a default that fills a marked unique attribute
  can collide (`unique_violation`); values kept from the raw state that a newer
  release rejects refuse unrelated updates of that entity (`enum_value_unknown`,
  which tells it is an enum, `datatype_mismatch`, `facet_violation`,
  `missing_required`); a deprecated marked attribute that a default sets warns
  `deprecated_term` on creates. <!-- claim: security_model.marked_integrity_refusals --> Give a marked required attribute a default, or
  leave it unmarked.
- **Prose is not filtered.** The summary and description of ontology terms, the
  text of a curated graph skill and the texts of functions and actions
  (`summary`, `use_when`, `returns`) are written by maintainers and shown as
  written: if they name a marked attribute or marking, a reader that cannot see
  it reads the name. Write them so that they hold for every reader
  ([Skills](skills.md#per-reader)). The marking declarations themselves are not
  shown to a caller that lacks them. <!-- claim: security_model.marking_declarations_hidden -->
- **Bundled file attributes cannot be marked.** The attributes of
  `files:File` ([Files](files.md#model)) stay visible to whoever
  sees the type, as do the download route and `file path`; a marked attribute of
  a file subclass is an ordinary attribute and hidden like any other. <!-- claim: security_model.file_attributes_visible -->
- **Smaller points.** `possible_duplicate` warnings never compare a hidden label
  (fewer warnings, no leak); a marked label leaves the entity without a label for
  the caller. Timing differences, between a hidden and a missing name or in the
  cost of reading a marked type, are not hidden. A release that downgrades or
  reuses a marked name is refused or warned
  ([Markings](access_control.md#markings)); a marking removed on purpose makes
  the values, history included, readable to every caller that sees the type. <!-- claim: security_model.marked_smaller_points -->
- **What it does not cover.** Classes, relations, edge qualifiers and enum values
  cannot be marked (personal data in an edge qualifier stays visible to whoever
  sees the edge), a marked attribute cannot be write-only (a caller writes only
  attributes it sees), and there are no deny rules on single attributes' reads. <!-- claim: security_model.only_attributes_are_marked -->

## Documents from a source

A change set document can hand over the desired state of an external source
([Reconciling a source](cli_graph.md#reconciling-a-source)). What that does
and does not prevent:

- The `source` of a document (`name`, `version`, `run`) is a **claim**. It is
  recorded in `request.source` next to the authenticated actor, like a
  reason, and it grants nothing: what a run may do is the rights of its
  actor ([Reconcile documents](access_control.md#reconcile-documents)). <!-- claim: security_model.source_is_a_claim --> A
  caller can name any source it likes; the key namespace `<source>/...` keeps
  honest pipelines apart, it does not stop a caller that may write the type
  from writing keys under another name. Whoever may retract a type can
  retract its entities with plain ops. Binding a source to a verified
  caller is not built.
- The guards (an empty complete scope, a mass retire, item counts) limit
  **accidents**: a failed collector, a broken query, a renamed key
  attribute. They are not a barrier against a writer with retract rights,
  who can raise `allow_retire` or `allow_empty` in the same document. <!-- claim: security_model.source_guards_limit_accidents -->
- What the declared needs refuse comes before planning, so it does not
  depend on the state: a scope type the caller does not see is an unknown
  type, and a key attribute or field it lacks the marking for is refused by
  name as an unknown attribute, with the same answer as for a name the type
  does not declare. A marked attribute outside `fields` is never read or
  written by the run. <!-- claim: security_model.source_declared_needs -->
- The plan reads the graph as the caller sees it. An entity of a hidden type
  that holds a wanted key is not found, so the create fails on the unique
  check, and the finding is concealed as for any write: its code and the
  visible target stay, nothing about the hidden entity. <!-- claim: security_model.source_plan_as_seen -->
- Refusals of the guards are audited as `invalid` events of the graph's
  stream with the source, so a refused run is visible; `audit verify` checks
  that the intent and the transaction name the same source. The source is
  not part of the transaction's `changeset_hash` (the transaction holds the
  ops the run expanded to) but of the request digest, which covers the whole
  document. <!-- claim: security_model.source_refusals_audited -->

## Derived file metadata

Metadata records of stored files ([Files: Metadata](files.md#metadata)) are
derived from the bytes of untrusted files. What protects them and what does
not:

- **Parsing runs in a child process, never in the writer.** Each extractor
  starts as `python -I -X utf8 -m graph_cli.extractors.child` (an isolated interpreter) with an empty
  environment, one read-only path, a wall-clock timeout, a CPU limit (a
  second above the timeout) and an address-space limit
  ([`[extract]`](configuration.md)); a limit hit or a crash is a recorded
  `failed` result, a kill from outside a retried transient one. A parser bug
  can hurt that process only, and it holds neither the graph nor a token. <!-- claim: security_model.extraction_in_a_child -->
  macOS ignores the address-space limit (`RLIMIT_AS`): there the memory limit
  is best effort and the timeout and CPU limit still hold. The limits stop
  runaway parsers, they are not a sandbox: the child runs as the same OS user
  and can read what that user can read.
- **SQLite files are opened read only and immutable**, with untrusted schemas
  off and no extension; only the schema is read, never a row. The PDF parser
  is the one third-party parser, pinned exactly and imported only by the
  extractors (an import contract). <!-- claim: security_model.sqlite_read_only -->
- **Records are written by the extraction only.** Every op on an entity of a
  record or part class is refused (`data.semantic.managed_class`) unless the
  request's source has the reserved kind `extractor` **and** the entity's key
  starts with the source's name. The kind is set by the program in process
  and no document can name it (`changeset.malformed`, locally and over the
  server); the check reads the source the audit records, on the stored state
  and the pinned model, so a caller who does not see every type gets the same
  answer. A link from an ordinary entity *to* a record is allowed, because
  people point at records. Not covered: ontology releases with a migration
  (the maintainer's tool rewrites records when a module is renamed), and the
  key prefix is the whole ownership test (no registry lookup). An action
  whose template writes a record class can be published but never runs:
  `graph do` is refused with the same finding. <!-- claim: security_model.records_by_extraction_only -->
- **Rights are the caller's.** The records are written as the caller, so the
  write path decides rights and visibility as for any write: a caller without
  `write.create` on the record classes stores a file and gets a warning
  instead of metadata (a dry run of the record's document finds out first, so nothing is
  audited per add). A run over the whole graph or over named files needs
  the maintain permission (`maint.reindex`) and is not available to the
  `graph` program; `graph file add` extracts and reports only the file it
  stored. <!-- claim: security_model.extraction_rights_are_the_callers --> A stored file altered on disk in a local data root is parsed as it
  is (`files verify --rehash` finds it); with an object store the bytes are
  checked against their hash before a parser sees them. <!-- claim: security_model.extraction_object_store_checked -->

## Data in backups and audit events

Audit events carry ids, types, hashes and reason text, never attribute
values. <!-- claim: security_model.audit_events_carry_no_values --> Stored files and their origins are instance data; backups of the
data root may therefore contain personal data (see
[Files: backups](files.md#backups)).
