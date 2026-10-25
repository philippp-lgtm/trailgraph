"""``graph-admin graph create|list|show|check|reindex|pull|push`` and ``graph skill
publish|render``."""

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
from graph_cli_admin.services_admin import graphs, skill, sources
from graph_cli_admin.services_admin.check import check
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
    }
)
REINDEX_OUTPUT: Final = obj(  # services_admin.reindex.reindex
    {"graph_id": STRING, "seq": INTEGER, "dry_run": BOOLEAN, "rebuilt": BOOLEAN}
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
        "unchanged": BOOLEAN,
        "dry_run": BOOLEAN,
        "source": obj(  # services_admin.sources.base_document: the workspace's new base
            {
                "schema": {"const": sources.SOURCE_SCHEMA},
                "graph": obj({"id": STRING, "name": STRING}),
                "ontology": ref("ontology_pin"),
                "skill": nullable(ref("source_skill")),
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
        errors=(*GRAPH_ERRORS, "access.denied", "admin.model_invalid", "admin.data_errors"),
        handler=check_command,
        output=CHECK_OUTPUT,
        description="Runs all data checks, including rules with when: batch and the in"
        " cardinality of relations, which no write checks. Exits 1 (admin.data_errors) on an"
        " error finding; warnings are listed only. With --ontology, checks the data under an"
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
        path=("graph", "pull"),
        kind="read",
        surface="model",
        action="maint.graph_configure",
        summary="Write graph REF's ontology release and skill into DIR to edit and push them.",
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
                "Replace the version directory, skill directory and base file if they exist.",
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
        ),
        handler=pull,
        output=PULL_OUTPUT,
        description="Writes DIR/ontology/<version>/<module>.yaml (the authored modules of the"
        " release, byte for byte), DIR/skill/graph-<graph>/ (the published skill, if any) and"
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
        ),
        handler=push,
        output=PUSH_OUTPUT,
        description="The graph must still have the ontology release and skill DIR was pulled"
        " from (or already what DIR holds), else admin.source_conflict. The highest"
        " ontology/<version>/ is released and pinned when it differs (maint.ontology_release,"
        " maint.ontology_pin), the skill directory published when it differs"
        " (maint.graph_configure); whenever either changes, the skill is checked against the"
        " ontology it will render with. Everything is checked before the first write; the new"
        " base is written into DIR.",
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
)
