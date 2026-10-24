"""``graph-admin graph create|list|show|check|reindex`` and ``graph skill publish|render``."""

from __future__ import annotations

from pathlib import Path
from typing import Final

from graph_cli.app_cli.commands import all_specs
from graph_cli.app_cli.commands.meta import inventory
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli_admin.maintenance_cli.commands.common import (
    CHANGE_ERRORS,
    DATA_ROOT,
    GRAPH_ERRORS,
    ROOT_ERRORS,
    admin_context,
    path,
)
from graph_cli_admin.services_admin import graphs, skill
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
    state.warnings += ctx.catalog.warnings
    return Result(data={"items": items})


def show(state: CommandState, ref: str, data_root: str | None) -> Result:
    ctx = admin_context(state, data_root)
    entry, data = graphs.show(ctx, ref)
    state.warnings += ctx.catalog.warnings
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
    ),
    CommandSpec(
        path=("graph", "show"),
        kind="read",
        surface="retrieve",
        action=None,
        summary="Show the manifest, head seq and installed releases of graph REF (name or id).",
        params=(REF, DATA_ROOT),
        examples=(
            Example("graph-admin graph show <graph>", "Show one graph."),
            Example("graph-admin graph show <graph> --json", "The same as JSON."),
        ),
        errors=GRAPH_ERRORS,
        handler=show,
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
        description="Runs all data checks, including rules with when: batch and the in"
        " cardinality of relations, which no write checks. Exits 1 (admin.data_errors) on an"
        " error finding; warnings are listed only. With --ontology, checks the data under an"
        " authored version before it is released.",
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
        description="The tx log is not changed. Use it when graph-admin audit verify reports"
        " derived files that differ from the log, or when reads warn storage.index_stale. A"
        " damaged tx log is backend.corrupt (repair first). Reads during the rebuild may miss"
        " a revision file for a moment.",
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
        description="DIR is a skill directory named graph-<graph> (underscores become dashes)."
        " A SKILL.md with block markers (<!-- graph_cli:block <name> -->) is a template: the"
        " app renders it with the graph's live model at every install (kind generated); it is"
        " rendered and checked against the current pin first. Without markers the files are"
        " installed as they are (kind static). Publishing the same files again changes nothing.",
    ),
    CommandSpec(
        path=("graph", "skill", "render"),
        kind="read",
        surface="meta",
        action=None,
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
            "graph.archived",
            "admin.skill_invalid",
            "admin.skill_render_exists",
        ),
        handler=render,
        description="Renders against the graph's current ontology pin with this checkout's"
        " program and runs the checks of publish; changes nothing in the data root. Problems"
        " publish would refuse are reported (exit 1) next to the written preview.",
    ),
)
