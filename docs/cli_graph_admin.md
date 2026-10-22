# `graph-admin`: the maintenance CLI

`graph-admin` is run from the repository checkout (`uv run graph-admin ...`)
and is not installed on `PATH`. Output and exit codes are those of `graph`
(see [Output and exit codes](cli_graph.md#output-and-exit-codes)).
`graph-admin --help` and `graph-admin <command> --help` show every option.

| Command | What it does |
| --- | --- |
| `init` | create the data root and, if missing, `config.toml` with a device id |
| `graph create NAME --ontology DIR` | create a graph pinned to the first release of an authored ontology; `--language TAG` sets the BCP 47 default language of its values (canonical case, fixed after create) |
| `graph list`, `graph show REF` | list graphs with title and language; show manifest, head, installed releases and the published skill |
| `graph check REF` | check every entity against the pinned ontology, including what no write checks (rules with `when: batch`, `in` cardinality); exit 1 (`admin.data_errors`) on error findings, warnings are listed; `--ontology DIR` checks against an authored version before it is released |
| `graph skill publish GRAPH DIR` | publish a skill directory or a template as the graph's skill (`--dry-run`, `--reason`) |
| `graph skill render GRAPH DIR --out OUT` | preview: write what `graph skills install` would install into `OUT/graph-<graph>/` and report what publish would refuse (`--force`); changes nothing in the data root |
| `ontology validate DIR` | compile and check an authored ontology |
| `ontology diff DIR --against DIR` / `--graph REF` | classify the changes and check the version bump |
| `ontology release GRAPH DIR` | publish a release into the graph and pin the graph to it; classes the release adds get their `display_prefix` as alias prefix (existing prefixes never change: a class renamed with `renamed_from` keeps the prefix of its old name, and a prefix another class already holds is not given out, with the warning `admin.alias_prefix_taken`) |
| `audit verify` | check every chain, tx log, revision file, manifest pin and installed policy; exit 1 on error findings |
| `audit anchor` | record every graph stream's head in the installation stream |
| `files verify --graph REF` | check the stored files of a graph: missing (and, with `--rehash`, damaged) files are errors, orphans and staging leftovers info |
| `files gc --graph REF` | remove stored files no transaction names and staging files older than one hour (`--dry-run`, `--reason`), audited as `maint.repair` |
| `policy check FILE [--graph G]` | compile an authored policy and report every problem; installs nothing |
| `policy show [--graph G]` | the installed compiled policy and its hash (the built-in one when none is installed) |
| `policy diff FILE [--graph G]` | rules and keys added, removed or changed against the installed policy |
| `policy apply FILE [--graph G] --reason R` | install a policy, audited as `maint.policy_change` (the installation policy also mirrored into every graph stream); refuses one that would lock you out; the installed policy again records missing mirrors |
| `policy explain --graph G [--project P] [--action A] [--type T] [--file F]` | what an app caller from a project (or from none) would get, and by which rule; records nothing |
| `view --graph REF` | serve a live view of the graph in the browser until Ctrl+C (`--port`, `--no-open`); no JSON mode (see [Live view](live_view.md)) |

Every change is audited: an intent in the graph's stream, the effect, then
an installation event anchoring the intent. A release must bump the version
at least as much as the changes require; a new major version on a graph
with data is released only if the data has no error findings under the new
version (`check_graph`, the check `graph check --ontology` runs without
releasing), otherwise the refusal lists them. Verification reports an
interrupted operation as info and anchors only graphs without errors.

Related guides: [Access control](access_control.md) (policies),
[Skills](skills.md#graph-skills) (publishing graph skills),
[Files](files.md#backups) (verifying stored files after a restore).
