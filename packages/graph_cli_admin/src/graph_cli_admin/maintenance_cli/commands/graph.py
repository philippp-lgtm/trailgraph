"""``graph-admin graph create|list|show|check|reindex|purge|pull|push``, ``graph skill
publish|render``, ``graph functions publish|show`` and ``graph actions publish|show``."""

from __future__ import annotations

from pathlib import Path
from typing import Final, get_args

from graph_cli.app_cli.commands import all_specs
from graph_cli.app_cli.commands.meta import inventory
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.output_schemas import (
    BOOLEAN,
    INTEGER,
    STRING,
    array,
    enum,
    nullable,
    obj,
    ref,
)
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.ports.catalog import GraphState
from graph_cli_admin.maintenance_cli.commands.common import (
    CHANGE_ERRORS,
    DATA_ROOT,
    GRAPH_ERRORS,
    ROOT_ERRORS,
    admin_context,
    path,
)
from graph_cli_admin.maintenance_cli.output_schemas import (
    NULLABLE_STRING,
    RETURNED_FILES,
    SKILL_KIND,
    STRINGS,
)
from graph_cli_admin.services_admin import actions, functions, graphs, skill, sources
from graph_cli_admin.services_admin.check import check
from graph_cli_admin.services_admin.purge import purge
from graph_cli_admin.services_admin.reindex import reindex

REF: Final = ParamSpec("ref", "graph_ref", "Graph name or id.", required=True, positional=True)
SKILL_DIR: Final = ParamSpec(
    "dir",
    "directory",
    "A skill directory named graph-<graph>, or a template.",
    required=True,
    positional=True,
    keyword="source",
    must_exist=True,
)
FUNCTIONS_DIR: Final = ParamSpec(
    "dir",
    "directory",
    "A functions folder: one <name>.yaml per function.",
    required=True,
    positional=True,
    keyword="source",
    must_exist=True,
)
ACTIONS_DIR: Final = ParamSpec(
    "dir",
    "directory",
    "An actions folder: one <name>.yaml per action.",
    required=True,
    positional=True,
    keyword="source",
    must_exist=True,
)


def create(
    state: CommandState,
    name: str,
    source: str,
    module: str | None,
    title: str | None,
    language: str | None,
    data_root: str | None,
) -> Result:
    ctx = admin_context(state, data_root)
    manifest = graphs.create(
        ctx, name=name, source=Path(source), module=module, title=title, language=language
    )
    return Result(
        data=graphs.describe(manifest), graph=graphs.envelope_graph(manifest.entry, seq=0)
    )


def list_command(state: CommandState, include_archived: bool, data_root: str | None) -> Result:
    ctx = admin_context(state, data_root)
    items = graphs.list_graphs(ctx, include_archived=include_archived)
    state.warnings += ctx.catalog_warnings()
    return Result(data={"items": items})


def show(state: CommandState, ref: str, data_root: str | None) -> Result:
    ctx = admin_context(state, data_root)
    entry, data = graphs.show(ctx, ref)
    state.warnings += ctx.catalog_warnings()
    seq = data["seq"]
    return Result(
        data=data, graph=graphs.envelope_graph(entry, seq if isinstance(seq, int) else None)
    )


def check_command(
    state: CommandState,
    ref: str,
    source: str | None,
    module: str | None,
    data_root: str | None,
) -> Result:
    outcome = check(
        admin_context(state, data_root), graph_ref=ref, source=path(source), module=module
    )
    return Result(
        data=outcome.data,
        graph=graphs.envelope_graph(outcome.entry, outcome.seq),
        warnings=outcome.warnings,
    )


def reindex_command(
    state: CommandState, ref: str, reason: str | None, dry_run: bool, data_root: str | None
) -> Result:
    outcome = reindex(
        admin_context(state, data_root), graph_ref=ref, reason=reason, dry_run=dry_run
    )
    return Result(data=outcome.data, graph=graphs.envelope_graph(outcome.entry, outcome.seq))


def purge_command(
    state: CommandState,
    ref: str,
    refs: tuple[str, ...],
    with_files: bool,
    reason: str,
    dry_run: bool,
    data_root: str | None,
) -> Result:
    outcome = purge(
        admin_context(state, data_root),
        graph_ref=ref,
        refs=refs,
        with_files=with_files,
        reason=reason,
        dry_run=dry_run,
    )
    return Result(data=outcome.data, graph=graphs.envelope_graph(outcome.entry, outcome.seq))


def app_inventory() -> dict[str, JsonValue]:
    """The inventory of the app in this checkout: the commands curated text may name."""
    return inventory(all_specs())


def publish(
    state: CommandState,
    ref: str,
    source: str,
    reason: str | None,
    dry_run: bool,
    data_root: str | None,
) -> Result:
    ctx = admin_context(state, data_root)
    published = skill.publish(
        ctx,
        graph_ref=ref,
        source=Path(source).resolve(),
        reason=reason,
        dry_run=dry_run,
        inventory=app_inventory(),
    )
    return Result(data=published.data, graph=graphs.envelope_graph(published.entry, published.seq))


def publish_functions(
    state: CommandState,
    ref: str,
    source: str,
    reason: str | None,
    dry_run: bool,
    data_root: str | None,
) -> Result:
    ctx = admin_context(state, data_root)
    published = functions.publish(
        ctx, graph_ref=ref, source=Path(source).resolve(), reason=reason, dry_run=dry_run
    )
    return Result(data=published.data, graph=graphs.envelope_graph(published.entry, published.seq))


def show_functions(state: CommandState, ref: str, data_root: str | None) -> Result:
    ctx = admin_context(state, data_root)
    entry, data = functions.show(ctx, graph_ref=ref)
    return Result(data=data, graph=graphs.envelope_graph(entry, None))


def publish_actions(
    state: CommandState,
    ref: str,
    source: str,
    reason: str | None,
    dry_run: bool,
    data_root: str | None,
) -> Result:
    ctx = admin_context(state, data_root)
    published = actions.publish(
        ctx, graph_ref=ref, source=Path(source).resolve(), reason=reason, dry_run=dry_run
    )
    return Result(data=published.data, graph=graphs.envelope_graph(published.entry, published.seq))


def show_actions(state: CommandState, ref: str, data_root: str | None) -> Result:
    ctx = admin_context(state, data_root)
    entry, data = actions.show(ctx, graph_ref=ref)
    return Result(data=data, graph=graphs.envelope_graph(entry, None))


def render(
    state: CommandState,
    ref: str,
    source: str,
    out: str,
    force: bool,
    data_root: str | None,
) -> Result:
    ctx = admin_context(state, data_root)
    rendered = skill.render(
        ctx,
        graph_ref=ref,
        source=Path(source).resolve(),
        out=Path(out).resolve(),
        force=force,
        inventory=app_inventory(),
    )
    return Result(
        data=rendered.data,
        error=rendered.error,
        graph=graphs.envelope_graph(rendered.entry, None),
    )


def pull(
    state: CommandState,
    ref: str,
    target: str,
    version: str | None,
    force: bool,
    data_root: str | None,
) -> Result:
    ctx = admin_context(state, data_root)
    pulled = sources.pull(
        ctx, graph_ref=ref, target=Path(target).resolve(), version=version, force=force
    )
    return Result(data=pulled.data, graph=graphs.envelope_graph(pulled.entry, None))


def push(
    state: CommandState,
    ref: str,
    source: str,
    reason: str | None,
    dry_run: bool,
    data_root: str | None,
) -> Result:
    ctx = admin_context(state, data_root)
    pushed = sources.push(
        ctx,
        graph_ref=ref,
        source=Path(source),  # as given: messages name it as the caller typed it
        reason=reason,
        dry_run=dry_run,
        inventory=app_inventory(),
    )
    return Result(
        data=pushed.data,
        graph=graphs.envelope_graph(pushed.entry, pushed.seq),
        warnings=pushed.warnings,
    )


# services_admin.graphs.describe: a graph's manifest
_MANIFEST: Final = {
    "id": STRING,
    "name": STRING,
    "title": NULLABLE_STRING,
    "language": NULLABLE_STRING,
    "state": enum(*get_args(GraphState)),
    "created_at": STRING,
    "ontology": ref("ontology_pin"),
    "alias_prefixes": obj(extra=STRING),
    "extensions": STRINGS,
    "store": obj({"backend": STRING, "format_version": INTEGER}),
}
CREATE_OUTPUT: Final = obj(_MANIFEST)
LIST_OUTPUT: Final = obj(  # services_admin.graphs.list_graphs
    {
        "items": array(
            obj(
                {
                    "id": STRING,
                    "name": STRING,
                    "title": NULLABLE_STRING,
                    "language": NULLABLE_STRING,
                    "state": enum(*get_args(GraphState)),
                    "ontology": ref("ontology_ref"),
                }
            )
        )
    }
)
SHOW_OUTPUT: Final = obj(  # services_admin.graphs.show
    {
        **_MANIFEST,
        "seq": INTEGER,
        "releases": STRINGS,
        "skill": nullable(ref("skill_summary")),
        "functions": nullable(obj({"count": INTEGER, "hash": STRING})),
        "actions": nullable(obj({"count": INTEGER, "hash": STRING})),
    }
)
CHECK_OUTPUT: Final = obj(  # services_admin.check.check
    {
        "graph_id": STRING,
        "ontology": ref("ontology_ref"),
        "pinned": BOOLEAN,
        "counts": ref("finding_counts"),
        "codes": obj(extra=INTEGER),
        "findings": array(ref("finding")),
        "functions": obj(  # the published catalog against the same model
            {"count": INTEGER, "problems": array(ref("function_problem"))}
        ),
        "actions": obj(  # the published action catalog against the same model
            {"count": INTEGER, "problems": array(ref("action_problem"))}
        ),
    }
)
REINDEX_OUTPUT: Final = obj(  # services_admin.reindex.reindex
    {"graph_id": STRING, "seq": INTEGER, "dry_run": BOOLEAN, "rebuilt": BOOLEAN}
)
PURGE_OUTPUT: Final = obj(  # services_admin.purge.purge
    {
        "graph_id": STRING,
        "entities": STRINGS,
        "retracted": STRINGS,
        "keys": obj({"entity": INTEGER, "tx": INTEGER, "file": INTEGER, "pending": INTEGER}),
        "blobs_removed": array(obj({"hash": STRING, "size": INTEGER})),
        "copies_removed": INTEGER,
        "dry_run": BOOLEAN,
        "purged": BOOLEAN,
    }
)
# ``path`` is null in the admin API's answer, which returns the files instead; the remote
# client writes them and answers its own path.
PULL_OUTPUT: Final = obj(  # services_admin.sources.pull
    {
        "graph_id": STRING,
        "path": NULLABLE_STRING,
        "ontology": obj(
            {
                "bundle": STRING,
                "version": STRING,
                "artifact_hash": STRING,
                "modules": STRINGS,
                "pinned": BOOLEAN,
            }
        ),
        "skill": nullable(ref("source_skill")),
        "functions": nullable(ref("source_functions")),
        "actions": nullable(ref("source_actions")),
        "files": STRINGS,
    },
    {"pulled": RETURNED_FILES},
)
PUSH_OUTPUT: Final = obj(  # services_admin.sources.push
    {
        "graph_id": STRING,
        "ontology": ref("release"),
        "skill": nullable(
            obj(
                {
                    "skill": STRING,
                    "from": nullable(ref("skill_summary")),
                    "to": nullable(ref("skill_summary")),
                    "files": STRINGS,
                    "published": BOOLEAN,
                }
            )
        ),
        "functions": nullable(
            obj(
                {
                    "from": nullable(ref("functions_summary")),
                    "to": nullable(ref("functions_summary")),
                    "functions": STRINGS,
                    "published": BOOLEAN,
                }
            )
        ),
        "actions": nullable(
            obj(
                {
                    "from": nullable(ref("actions_summary")),
                    "to": nullable(ref("actions_summary")),
                    "actions": STRINGS,
                    "published": BOOLEAN,
                }
            )
        ),
        "unchanged": BOOLEAN,
        "dry_run": BOOLEAN,
        "source": obj(  # services_admin.sources.base_document: the workspace's new base
            {
                "schema": {"const": sources.SOURCE_SCHEMA},
                "graph": obj({"id": STRING, "name": STRING}),
                "ontology": ref("ontology_pin"),
                "skill": nullable(ref("source_skill")),
                "functions": nullable(ref("source_functions")),
                "actions": nullable(ref("source_actions")),
            }
        ),
    }
)
PUBLISH_OUTPUT: Final = obj(  # services_admin.skill.publish
    {
        "graph_id": STRING,
        "skill": STRING,
        "from": nullable(ref("skill_summary")),
        "to": ref("skill_summary"),
        "files": STRINGS,
        "published": BOOLEAN,
        "dry_run": BOOLEAN,
    }
)
PUBLISH_FUNCTIONS_OUTPUT: Final = obj(  # services_admin.functions.publish
    {
        "graph_id": STRING,
        "from": nullable(ref("functions_summary")),
        "to": ref("functions_summary"),
        "functions": STRINGS,
        "published": BOOLEAN,
        "dry_run": BOOLEAN,
    }
)
SHOW_FUNCTIONS_OUTPUT: Final = obj(  # services_admin.functions.show
    {
        "graph_id": STRING,
        "slot": nullable(ref("functions_summary")),
        "functions": array(
            obj(
                {
                    "name": STRING,
                    "module": NULLABLE_STRING,
                    "summary": STRING,
                    "status": enum("active", "deprecated"),
                    "featured": BOOLEAN,
                    "hash": STRING,
                }
            )
        ),
    }
)
PUBLISH_ACTIONS_OUTPUT: Final = obj(  # services_admin.actions.publish
    {
        "graph_id": STRING,
        "from": nullable(ref("actions_summary")),
        "to": ref("actions_summary"),
        "actions": STRINGS,
        "published": BOOLEAN,
        "dry_run": BOOLEAN,
    }
)
SHOW_ACTIONS_OUTPUT: Final = obj(  # services_admin.actions.show
    {
        "graph_id": STRING,
        "slot": nullable(ref("actions_summary")),
        "actions": array(
            obj(
                {
                    "name": STRING,
                    "module": NULLABLE_STRING,
                    "summary": STRING,
                    "status": enum("active", "deprecated"),
                    "featured": BOOLEAN,
                    "hash": STRING,
                }
            )
        ),
    }
)
RENDER_OUTPUT: Final = obj(  # services_admin.skill.render; ``path`` as for pull
    {
        "graph_id": STRING,
        "skill": STRING,
        "kind": SKILL_KIND,
        "hash": STRING,
        "model": ref("ontology_pin"),
        "path": NULLABLE_STRING,
        "files": STRINGS,
        "problems": array(obj({"rule": STRING, "path": STRING, "message": STRING})),
    },
    {"rendered": RETURNED_FILES},
)


SPECS: Final = (
    CommandSpec(
        path=("graph", "create"),
        kind="write",
        surface="act",
        action="maint.graph_create",
        summary="Create graph NAME pinned to the first release of an authored ontology.",
        params=(
            ParamSpec("name", "string", "Name of the new graph.", required=True, positional=True),
            ParamSpec(
                "--ontology",
                "directory",
                "Directory with the authored ontology; becomes the first release.",
                required=True,
                keyword="source",
            ),
            ParamSpec("--module", "string", "Main module id, if the directory holds several."),
            ParamSpec("--title", "string", "Human-readable title."),
            ParamSpec(
                "--language",
                "string",
                "BCP 47 default language of the values (e.g. en, de-CH); fixed after create.",
            ),
            DATA_ROOT,
        ),
        examples=(
            Example("graph-admin graph create <graph> --ontology <dir>", "Create a graph."),
            Example(
                "graph-admin graph create <graph> --ontology <dir> --title <value> --json",
                "Create it with a title, as JSON.",
            ),
            Example(
                "graph-admin graph create <graph> --ontology <dir> --language en",
                "Create it with a default language.",
            ),
        ),
        errors=(
            *ROOT_ERRORS,
            *CHANGE_ERRORS,
            "admin.model_invalid",
            "admin.graph_name_taken",
        ),
        handler=create,
        output=CREATE_OUTPUT,
        remote="hooked",
    ),
    CommandSpec(
        path=("graph", "list"),
        kind="read",
        surface="retrieve",
        action=None,
        summary="List the graphs of the data root.",
        params=(
            ParamSpec("--all", "flag", "Include archived graphs.", keyword="include_archived"),
            DATA_ROOT,
        ),
        examples=(
            Example("graph-admin graph list", "List the graphs."),
            Example("graph-admin graph list --all --json", "Archived ones too, as JSON."),
        ),
        errors=ROOT_ERRORS,
        handler=list_command,
        output=LIST_OUTPUT,
        description="A project admin (an admin token bound to a project) sees only the graphs"
        " it maintains.",
    ),
    CommandSpec(
        path=("graph", "show"),
        kind="read",
        surface="retrieve",
        action="maint.graph_configure",
        summary="Show the manifest, head seq and installed releases of graph REF (name or id).",
        params=(REF, DATA_ROOT),
        examples=(
            Example("graph-admin graph show <graph>", "Show one graph."),
            Example("graph-admin graph show <graph> --json", "The same as JSON."),
        ),
        errors=(*GRAPH_ERRORS, "access.denied"),
        handler=show,
        output=SHOW_OUTPUT,
    ),
    CommandSpec(
        path=("graph", "check"),
        kind="read",
        surface="trace",
        action="maint.audit_verify",
        summary="Check every entity of graph REF (name or id) against its ontology.",
        params=(
            REF,
            ParamSpec(
                "--ontology",
                "directory",
                "Check against this authored ontology instead of the pinned release.",
                keyword="source",
            ),
            ParamSpec(
                "--module", "string", "Main module id, if the --ontology directory holds several."
            ),
            DATA_ROOT,
        ),
        examples=(
            Example("graph-admin graph check <graph>", "Check the data."),
            Example(
                "graph-admin graph check <graph> --ontology <dir> --json",
                "Check it under an authored version, as JSON.",
            ),
        ),
        errors=(
            *GRAPH_ERRORS,
            "access.denied",
            "admin.model_invalid",
            "admin.data_errors",
            "admin.functions_broken",
            "admin.actions_broken",
        ),
        handler=check_command,
        output=CHECK_OUTPUT,
        description="Runs all data checks, including rules with when: batch and the in"
        " cardinality of relations, which no write checks. Exits 1 (admin.data_errors) on an"
        " error finding; warnings are listed only. Also checks the published functions against the"
        " model (admin.functions_broken, for example function.stale after an interrupted"
        " push) and the published actions (admin.actions_broken, for example action.stale; the"
        " functions are reported first). With --ontology, checks the data under an"
        " authored version before it is released.",
        remote="hooked",
    ),
    CommandSpec(
        path=("graph", "reindex"),
        kind="write",
        surface="act",
        action="maint.reindex",
        summary="Rebuild the revision files, checkpoint and index of graph REF from its tx log.",
        params=(
            REF,
            ParamSpec("--reason", "string", "Why it is rebuilt; recorded in the audit event."),
            ParamSpec("--dry-run", "flag", "Show the seq it would rebuild from without writing."),
            DATA_ROOT,
        ),
        examples=(
            Example("graph-admin graph reindex <graph> --dry-run", "Show what would be rebuilt."),
            Example(
                "graph-admin graph reindex <graph> --reason <reason> --json",
                "Rebuild, with a reason.",
            ),
        ),
        errors=(*GRAPH_ERRORS, *CHANGE_ERRORS, "backend.corrupt", "backend.unavailable"),
        handler=reindex_command,
        output=REINDEX_OUTPUT,
        description="The tx log is not changed. Use it when graph-admin audit verify reports"
        " derived files that differ from the log, or when reads warn storage.index_stale. A"
        " damaged tx log is backend.corrupt (repair first). Reads during the rebuild may miss"
        " a revision file for a moment.",
    ),
    CommandSpec(
        path=("graph", "purge"),
        kind="write",
        surface="act",
        action="maint.purge",
        summary="Erase entities of the sealed graph REF for good (keys shredded, files deleted).",
        params=(
            REF,
            ParamSpec(
                "entity",
                "entity_ref",
                "Entities to erase: full id, alias or abbreviation.",
                required=True,
                repeatable=True,
                positional=True,
                keyword="refs",
            ),
            ParamSpec(
                "--with-files",
                "flag",
                "Also erase the stored files their revisions name, unless another entity names"
                " them.",
            ),
            ParamSpec(
                "--reason",
                "string",
                "Why they are erased; recorded in the clear in the audit stream, so name a"
                " ticket, not the person.",
                required=True,
            ),
            ParamSpec("--dry-run", "flag", "Show what would be erased without writing."),
            DATA_ROOT,
        ),
        examples=(
            Example(
                "graph-admin graph purge <graph> <entity> --reason <ticket> --dry-run",
                "Show what would be erased.",
            ),
            Example(
                "graph-admin graph purge <graph> <entity> <file-entity> --with-files"
                " --reason <ticket> --json",
                "Erase an entity and its stored file.",
            ),
        ),
        errors=(
            *GRAPH_ERRORS,
            *CHANGE_ERRORS,
            "admin.graph_not_sealed",
            "entity.not_found",
            "usage.invalid_argument",
            "backend.corrupt",
            "backend.unavailable",
        ),
        handler=purge_command,
        output=PURGE_OUTPUT,
        description="Retracts the entities with empty values in one tx, then destroys the keys"
        " of their revisions and of every tx that wrote one of them, and with --with-files"
        " deletes their stored files. Afterwards they read as shredded: true, and their values"
        " cannot be recovered from the data root or any backup of it, once no copy of the key"
        " store from before the purge is left. Ids, types, edges and the audit trail stay;"
        " unlink first when a relation itself must go. Running the same purge again completes"
        " an interrupted one. Only sealed graphs (admin.graph_not_sealed otherwise).",
    ),
    CommandSpec(
        path=("graph", "pull"),
        kind="read",
        surface="model",
        action="maint.graph_configure",
        summary="Write graph REF's ontology release, skill, functions and actions into DIR.",
        params=(
            REF,
            ParamSpec(
                "dir",
                "directory",
                "Workspace directory; created if missing.",
                required=True,
                positional=True,
                keyword="target",
            ),
            ParamSpec("--version", "string", "Pull this installed release instead of the pin."),
            ParamSpec(
                "--force",
                "flag",
                "Replace the version, skill, functions and actions directories and base file"
                " if they exist.",
            ),
            DATA_ROOT,
        ),
        examples=(
            Example("graph-admin graph pull <graph> <dir>", "Pull a graph's sources."),
            Example(
                "graph-admin graph pull <graph> <dir> --force --json",
                "Pull them again over an earlier pull, as JSON.",
            ),
        ),
        errors=(
            *GRAPH_ERRORS,
            "access.denied",
            "admin.source_exists",
            "backend.corrupt",
            "ontology.artifact_missing",
            "skills.source_invalid",
            "functions.slot_invalid",
            "actions.slot_invalid",
        ),
        handler=pull,
        output=PULL_OUTPUT,
        description="Writes DIR/ontology/<version>/<module>.yaml (the authored modules of the"
        " release, byte for byte), DIR/skill/graph-<graph>/ (the published skill, if any),"
        " DIR/functions/<name>.yaml (the published functions, if any),"
        " DIR/actions/<name>.yaml (the published actions, if any) and"
        " DIR/.graph_cli_source.json, the base push checks. Other files in DIR stay. Changes"
        " nothing in the data root.",
        remote="hooked",
    ),
    CommandSpec(
        path=("graph", "push"),
        kind="write",
        surface="act",
        action="maint.graph_configure",
        summary="Release and publish what changed in DIR, a directory pulled from graph REF.",
        params=(
            REF,
            ParamSpec(
                "dir",
                "directory",
                "A directory written by graph pull.",
                required=True,
                positional=True,
                keyword="source",
                must_exist=True,
            ),
            ParamSpec("--reason", "string", "Why it changes; recorded in every audit event."),
            ParamSpec("--dry-run", "flag", "Run every check and show the change without writing."),
            DATA_ROOT,
        ),
        examples=(
            Example("graph-admin graph push <graph> <dir> --dry-run", "Show what would change."),
            Example(
                "graph-admin graph push <graph> <dir> --reason <reason> --json",
                "Release and publish, with a reason.",
            ),
        ),
        errors=(
            *GRAPH_ERRORS,
            *CHANGE_ERRORS,
            "graph.archived",
            "admin.source_conflict",
            "admin.model_invalid",
            "admin.version_bump_insufficient",
            "admin.release_exists",
            "admin.migration_required",
            "admin.skill_invalid",
            "admin.functions_invalid",
            "admin.functions_broken",
            "admin.actions_invalid",
            "admin.actions_broken",
        ),
        handler=push,
        output=PUSH_OUTPUT,
        description="The graph must still have the ontology release, skill, functions and actions"
        " DIR was pulled from (or already what DIR holds), else admin.source_conflict. The"
        " highest ontology/<version>/ is released and pinned when it differs"
        " (maint.ontology_release, maint.ontology_pin), the skill directory, the functions"
        " directory and the actions directory published when they differ (maint.graph_configure;"
        " a missing functions or actions directory is an empty one); whenever the ontology"
        " changes, the skill, the functions and the actions are checked against the ontology"
        " they will have (the published functions and actions too, if the push does not replace"
        " them: admin.functions_broken, admin.actions_broken). A function and an action may not"
        " share a name. Everything is checked before the first write; the new base is written"
        " into DIR.",
        remote="hooked",
    ),
    CommandSpec(
        path=("graph", "skill", "publish"),
        kind="write",
        surface="act",
        action="maint.graph_configure",
        summary="Publish DIR as the skill of graph REF (name or id).",
        params=(
            REF,
            SKILL_DIR,
            ParamSpec("--reason", "string", "Why the skill changes; recorded in the audit event."),
            ParamSpec("--dry-run", "flag", "Validate and show the change without writing."),
            DATA_ROOT,
        ),
        examples=(
            Example("graph-admin graph skill publish <graph> <dir>", "Publish a skill."),
            Example(
                "graph-admin graph skill publish <graph> <dir> --dry-run --json",
                "Check it and show the change, as JSON.",
            ),
        ),
        errors=(*GRAPH_ERRORS, *CHANGE_ERRORS, "graph.archived", "admin.skill_invalid"),
        handler=publish,
        output=PUBLISH_OUTPUT,
        description="DIR is a skill directory named graph-<graph> (underscores become dashes)."
        " A SKILL.md with block markers (<!-- graph_cli:block <name> -->) is a template: the"
        " app renders it with the graph's live model at every install (kind generated); it is"
        " rendered and checked against the current pin first. Without markers the files are"
        " installed as they are (kind static). Publishing the same files again changes nothing.",
        remote="hooked",
    ),
    CommandSpec(
        path=("graph", "skill", "render"),
        kind="read",
        surface="meta",
        action="maint.graph_configure",
        summary="Preview DIR as graph REF's skill: write what graph skills install would install.",
        params=(
            REF,
            SKILL_DIR,
            ParamSpec(
                "--out",
                "directory",
                "Existing directory; the skill is written to OUT/graph-<graph>/.",
                required=True,
            ),
            ParamSpec("--force", "flag", "Replace OUT/graph-<graph>/ if it exists."),
            DATA_ROOT,
        ),
        examples=(
            Example("graph-admin graph skill render <graph> <dir> --out <dir>", "Write a preview."),
            Example(
                "graph-admin graph skill render <graph> <dir> --out <dir> --force --json",
                "Replace an earlier preview, as JSON.",
            ),
        ),
        errors=(
            *GRAPH_ERRORS,
            "access.denied",
            "graph.archived",
            "admin.skill_invalid",
            "admin.skill_render_exists",
        ),
        handler=render,
        output=RENDER_OUTPUT,
        description="Renders against the graph's current ontology pin with this checkout's"
        " program and runs the checks of publish; changes nothing in the data root. Problems"
        " publish would refuse are reported (exit 1) next to the written preview.",
        remote="hooked",
    ),
    CommandSpec(
        path=("graph", "functions", "publish"),
        kind="write",
        surface="act",
        action="maint.graph_configure",
        summary="Publish DIR as the function catalog of graph REF (name or id).",
        params=(
            REF,
            FUNCTIONS_DIR,
            ParamSpec(
                "--reason", "string", "Why the functions change; recorded in the audit event."
            ),
            ParamSpec("--dry-run", "flag", "Check and show the change without writing."),
            DATA_ROOT,
        ),
        examples=(
            Example("graph-admin graph functions publish <graph> <dir>", "Publish the functions."),
            Example(
                "graph-admin graph functions publish <graph> <dir> --dry-run --json",
                "Check them and show the change, as JSON.",
            ),
        ),
        errors=(*GRAPH_ERRORS, *CHANGE_ERRORS, "graph.archived", "admin.functions_invalid"),
        handler=publish_functions,
        output=PUBLISH_FUNCTIONS_OUTPUT,
        description="DIR holds one <name>.yaml per function (graph_cli.function/1). Every"
        " function is checked against the graph's current ontology pin; all problems are"
        " reported together (admin.functions_invalid, details.problems). An empty DIR"
        " unpublishes every function. Publishing the same files again changes nothing."
        " --dry-run is the check.",
        remote="hooked",
    ),
    CommandSpec(
        path=("graph", "functions", "show"),
        kind="read",
        surface="meta",
        action="maint.graph_configure",
        summary="Show the published function catalog of graph REF.",
        params=(REF, DATA_ROOT),
        examples=(
            Example("graph-admin graph functions show <graph>", "List the published functions."),
            Example("graph-admin graph functions show <graph> --json", "The same, as JSON."),
        ),
        errors=(*GRAPH_ERRORS, "access.denied"),
        handler=show_functions,
        output=SHOW_FUNCTIONS_OUTPUT,
        description="The slot (hash, ontology pin it was published against) and one line per"
        " function; read-only.",
        remote="hooked",
    ),
    CommandSpec(
        path=("graph", "actions", "publish"),
        kind="write",
        surface="act",
        action="maint.graph_configure",
        summary="Publish DIR as the action catalog of graph REF (name or id).",
        params=(
            REF,
            ACTIONS_DIR,
            ParamSpec("--reason", "string", "Why the actions change; recorded in the audit event."),
            ParamSpec("--dry-run", "flag", "Check and show the change without writing."),
            DATA_ROOT,
        ),
        examples=(
            Example("graph-admin graph actions publish <graph> <dir>", "Publish the actions."),
            Example(
                "graph-admin graph actions publish <graph> <dir> --dry-run --json",
                "Check them and show the change, as JSON.",
            ),
        ),
        errors=(*GRAPH_ERRORS, *CHANGE_ERRORS, "graph.archived", "admin.actions_invalid"),
        handler=publish_actions,
        output=PUBLISH_ACTIONS_OUTPUT,
        description="DIR holds one <name>.yaml per action (graph_cli.action/1). Every"
        " action is checked against the graph's current ontology pin; all problems are"
        " reported together (admin.actions_invalid, details.problems). An action may not"
        " be named like a published function. An empty DIR unpublishes every action."
        " Publishing the same files again changes nothing. --dry-run is the check.",
        remote="hooked",
    ),
    CommandSpec(
        path=("graph", "actions", "show"),
        kind="read",
        surface="meta",
        action="maint.graph_configure",
        summary="Show the published action catalog of graph REF.",
        params=(REF, DATA_ROOT),
        examples=(
            Example("graph-admin graph actions show <graph>", "List the published actions."),
            Example("graph-admin graph actions show <graph> --json", "The same, as JSON."),
        ),
        errors=(*GRAPH_ERRORS, "access.denied"),
        handler=show_actions,
        output=SHOW_ACTIONS_OUTPUT,
        description="The slot (hash, ontology pin it was published against) and one line per"
        " action; read-only.",
        remote="hooked",
    ),
)
