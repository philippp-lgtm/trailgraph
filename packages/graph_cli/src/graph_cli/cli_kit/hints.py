"""Copy-pasteable hints for error codes (contracts B9.8, B9.9).

The only place that knows CLI syntax for errors. Every registered error
code has an entry: a function of ``(details, program)`` or an explicit
``None`` (U test). Admin and extension codes register theirs at startup.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable, Mapping
from typing import Final

from graph_cli.kernel.canonical import JsonValue

ADMIN_PROGRAM: Final = "graph-admin"

Hint = Callable[[Mapping[str, JsonValue], str], str | None]


def _init(details: Mapping[str, JsonValue], program: str) -> str | None:
    """A missing data root (B12.4); other unavailability has no single fix."""
    return f"{ADMIN_PROGRAM} init" if "data_root" in details else None


def _help(details: Mapping[str, JsonValue], program: str) -> str | None:
    return f"{program} --help"


MAX_LOOKUP_TEXT: Final = 200  # a longer text is not a name worth echoing into a command


def _lookup_text(text: str, program: str, options: str = "") -> str | None:
    """``graph lookup`` for a text the caller gave where an entity reference goes: options first
    and the text after ``--`` when it starts with ``-``. ``graph-admin`` has no ``lookup``, and a
    text that is not a short name has no useful lookup."""
    if program == ADMIN_PROGRAM or len(text) > MAX_LOOKUP_TEXT:
        return None
    if text.startswith("-"):
        return f"{program} lookup --json{options} -- {shlex.quote(text)}"
    return f"{program} lookup {shlex.quote(text)} --json{options}"


def _usage(details: Mapping[str, JsonValue], program: str) -> str | None:
    """A name where an entity reference goes: look it up (for the slot it is meant for, when the
    error names one); any other usage error, and a text that cannot be looked up: the help."""
    actual = details.get("actual")
    if details.get("expected") != "entity_ref" or not isinstance(actual, str):
        return _help(details, program)
    options = ""
    for key, flag in (("for", "--for"), ("for_source", "--for-source"), ("class", "--type")):
        value = details.get(key)
        if isinstance(value, str):
            options = f" {flag} {shlex.quote(value)}"
            break
    return _lookup_text(actual, program, options) or _help(details, program)


def _graphs(details: Mapping[str, JsonValue], program: str) -> str | None:
    """Where the selectable graphs are listed (B12.5)."""
    return f"{program} graph list" if program == ADMIN_PROGRAM else f"{program} graphs"


def _whoami(details: Mapping[str, JsonValue], program: str) -> str | None:
    """What the caller may do, per graph (B2.10); access is changed by the maintainer, never
    by the caller (B6.15)."""
    return None if program == ADMIN_PROGRAM else f"{program} whoami"


def _same_key(details: Mapping[str, JsonValue], program: str) -> str | None:
    """A lost write answer: the same command with the key it was sent with is safe (B14.10)."""
    argv = details.get("argv")
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        return None
    return shlex.join([program, *(str(a) for a in argv)])


def _types(details: Mapping[str, JsonValue], program: str) -> str | None:
    return f"{program} describe types"


def _term(details: Mapping[str, JsonValue], program: str) -> str | None:
    """Where the terms of the unknown kind are listed: modules for a module, else types."""
    if details.get("kind") == "module":
        return f"{program} describe modules"
    return _types(details, program)


def _function_search(details: Mapping[str, JsonValue], program: str) -> str | None:
    """Find a function by what it is for."""
    return f"{program} describe search <words> --kind function"


def _action_search(details: Mapping[str, JsonValue], program: str) -> str | None:
    """Find an action by what it is for."""
    return f"{program} describe search <words> --kind action"


def _view_search(details: Mapping[str, JsonValue], program: str) -> str | None:
    """Find a view by what it shows."""
    return f"{program} describe search <words> --kind view"


def _tools(details: Mapping[str, JsonValue], program: str) -> str | None:
    """A tool the caller cannot use, or one that changed: list the tools it may use."""
    return f"{program} describe tools"


def _view_same_state(details: Mapping[str, JsonValue], program: str) -> str | None:
    """A stale page cursor: the next page of the state it was issued for."""
    seq = details.get("cursor_seq")
    if not isinstance(seq, int):
        return None
    return f"{program} view --name <name> --cursor <cursor> --as-of tx:{seq}"


def _precondition(details: Mapping[str, JsonValue], program: str) -> str | None:
    """A refused action: read its preconditions, then look at the dry run."""
    action = details.get("action")
    return f"{program} describe action {action}" if isinstance(action, str) else None


def _scope(details: Mapping[str, JsonValue], program: str) -> str | None:
    """A refused reconcile scope: the entities that hold its keys now."""
    kind, attribute, prefix = details.get("type"), details.get("attribute"), details.get("prefix")
    if isinstance(kind, str) and isinstance(attribute, str) and isinstance(prefix, str):
        where = shlex.quote(f"{attribute}:prefix={prefix}")
        return f"{program} find --type {kind} --where {where}"
    return None


def _key_attribute(details: Mapping[str, JsonValue], program: str) -> str | None:
    """A key attribute that cannot be one: the fields of the scope's type."""
    kind = details.get("type")
    return f"{program} describe type {kind}" if isinstance(kind, str) else None


def _lookup_class(details: Mapping[str, JsonValue], program: str) -> str | None:
    """A reference of the wrong class: find the entity that fits the parameter by its name. A
    view is named by its kind (it may share its name with a function), and the entity of an entity
    view is its ``_entity``."""
    function = details.get("function", details.get("action"))
    parameter = details.get("parameter")
    if isinstance(function, str) and isinstance(parameter, str):
        return f"{program} lookup <text> --for {function}.{parameter}"
    view = details.get("view")
    if isinstance(view, str):
        slot = parameter if isinstance(parameter, str) else "_entity"
        return f"{program} lookup <text> --for view:{view}.{slot}"
    expected = details.get("expected")
    if isinstance(expected, str):
        return f"{program} lookup <text> --type {expected}"
    return None


def _lookup_ref(details: Mapping[str, JsonValue], program: str) -> str | None:
    """A name where a ``ref`` parameter needs an id: look it up for that parameter."""
    kind = details.get("type")
    if isinstance(kind, str) and kind.startswith("ref"):
        return _lookup_class({k: v for k, v in details.items() if k != "expected"}, program)
    return None


def _schema(details: Mapping[str, JsonValue], program: str) -> str | None:
    """A prefixed name where a local one is expected: the labels as the query writes them."""
    return f"{program} describe schema" if details.get("prefixed") is True else None


def _fields(details: Mapping[str, JsonValue], program: str) -> str | None:
    """Where the fields of a type show, with their base type and ``role``."""
    types = details.get("types")
    if isinstance(types, list) and len(types) == 1 and isinstance(types[0], str):
        return f"{program} describe type {types[0]}"
    return f"{program} describe types"


def _get(details: Mapping[str, JsonValue], program: str) -> str | None:
    """Re-read the entity before retrying (B3.11)."""
    entity = details.get("id")
    return f"{program} get {entity}" if isinstance(entity, str) else None


def _history(details: Mapping[str, JsonValue], program: str) -> str | None:
    """The revisions that exist (B8.15)."""
    entity = details.get("id")
    return f"{program} history {entity}" if isinstance(entity, str) else None


def _apply(details: Mapping[str, JsonValue], program: str) -> str | None:
    """Queries only read; writes go through the change set route (B3.9)."""
    return f"{program} apply -" if details.get("kind") == "write" else None


def _param(details: Mapping[str, JsonValue], program: str) -> str | None:
    name = details.get("name")
    return f"{program} query --param {name}=<value>" if isinstance(name, str) else None


def _mkdir(details: Mapping[str, JsonValue], program: str) -> str | None:
    """A missing configuration directory of the skill target (D15)."""
    path = details.get("path")
    if details.get("cause") == "missing" and isinstance(path, str):
        return f"mkdir {shlex.quote(path)}"
    return None


def _install(details: Mapping[str, JsonValue], program: str, *, force: bool) -> str | None:
    """Re-run ``skills install`` (or ``skills emit``) for the same target and graphs (D15)."""
    scope, out = details.get("scope"), details.get("out")
    if isinstance(scope, str):
        words = [program, "skills", "install", "--scope", scope]
        target = details.get("dir")
        if isinstance(target, str):
            words += ["--dir", shlex.quote(target)]
    elif isinstance(out, str):
        words = [program, "skills", "emit", "--out", shlex.quote(out)]
    else:
        return None
    graphs = details.get("graphs")
    if isinstance(graphs, list):
        for graph in graphs:
            if isinstance(graph, str):
                words += ["--graph", shlex.quote(graph)]
    return " ".join([*words, "--force"] if force else words)


def _force(details: Mapping[str, JsonValue], program: str) -> str | None:
    return _install(details, program, force=True)


def _refresh(details: Mapping[str, JsonValue], program: str) -> str | None:
    return _install(details, program, force=False)


def _update(details: Mapping[str, JsonValue], program: str) -> str | None:
    """Install the server's build and refresh the skills (B14.18): the program the details
    name (a refused client), else this one; ``--dir`` for a project's skills."""
    named = details.get("program")
    words = [named if isinstance(named, str) else program, "self", "update"]
    target = details.get("dir")
    if isinstance(target, str):
        words += ["--dir", shlex.quote(target)]
    return " ".join(words)


def _start(details: Mapping[str, JsonValue], program: str) -> str | None:
    """Refresh the installed skills (not the program): ``graph start``, with ``--dir`` for a
    project's copies. A different program build is for ``graph self update`` (``_update``)."""
    words = [program, "start"]
    target = details.get("dir")
    if isinstance(target, str):
        words += ["--dir", shlex.quote(target)]
    return " ".join(words)


def _manual_install(details: Mapping[str, JsonValue], program: str) -> str | None:
    """A program ``update`` cannot replace: the install command it named, if any."""
    command = details.get("command")
    return command if isinstance(command, str) else None


def _publish(details: Mapping[str, JsonValue], program: str) -> str | None:
    """Graph skills are published by the maintainer (D15 Q2)."""
    graph = details.get("graph")
    if not isinstance(graph, str):
        return None
    return f"{ADMIN_PROGRAM} graph skill publish {shlex.quote(graph)} <dir>"


_BASE: Final[dict[str, Hint | None]] = {
    "usage.invalid_argument": _usage,
    "usage.unknown_command": _help,
    "usage.missing_argument": _help,
    "usage.invalid_input": None,
    "graph.not_selected": _graphs,
    "graph.not_found": _graphs,
    "graph.not_in_project": _graphs,
    "graph.archived": None,
    "graph.readonly": None,
    "entity.not_found": None,
    "entity.ambiguous_ref": None,
    "entity.revision_not_found": _history,
    "term.unknown": _term,
    "term.no_labels": _fields,
    "term.no_text_fields": _fields,
    "changeset.malformed": None,
    "changeset.local_ref_unresolved": None,
    "changeset.local_ref_duplicate": None,
    "changeset.too_large": None,
    "write.invalid": None,
    # the message of a reconcile refusal names the document field to change; where a command
    # shows what the field is about, the hint is that command
    "reconcile.source_empty": _scope,
    "reconcile.retire_limit": _scope,
    "reconcile.shape": None,
    "reconcile.key_attribute": _key_attribute,
    "reconcile.scope_overlap": None,
    "reconcile.scope_conflict": None,
    "reconcile.move_invalid": None,
    "reconcile.target_not_found": None,
    "reconcile.type_mismatch": None,
    "query.syntax": _schema,
    "query.unsupported": _apply,
    "query.ambiguous_term": _types,
    "query.unbound_variable": None,
    "query.parameter_missing": _param,
    "query.too_large": None,
    "query.invalid_value": None,
    "function.not_found": _function_search,
    "function.parameter_missing": None,
    "function.parameter_unknown": None,
    "function.parameter_invalid": _lookup_ref,
    "function.parameter_type": _lookup_class,
    "function.builtin_module": None,
    "function.invalid_default": None,
    "function.invalid_example": None,
    "function.invalid_file": None,
    "function.invalid_value": None,
    "function.invalid_yaml": None,
    "function.missing_key": None,
    "function.module_required": None,
    "function.name_mismatch": None,
    "function.parameter_paging": None,
    "function.parameter_undeclared": None,
    "function.parameter_unused": None,
    "function.pattern_impossible": None,
    "function.query_too_long": None,
    "function.replaced_by_invalid": None,
    "function.returns_mismatch": None,
    "function.returns_type_mismatch": None,
    "function.returns_star": None,
    "function.round_trip_failed": None,
    "function.stale": None,
    "function.too_many_functions": None,
    "function.type_mismatch": None,
    "function.unknown_class": None,
    "function.unknown_enum": None,
    "function.unknown_key": None,
    "function.unknown_module": None,
    "function.unsupported_schema": None,
    "functions.slot_invalid": None,
    "actions.slot_invalid": None,
    "views.slot_invalid": None,
    "function.name_taken": None,
    "action.not_found": _action_search,
    "action.parameter_missing": None,
    "action.parameter_unknown": None,
    "action.parameter_invalid": _lookup_ref,
    "action.parameter_type": _lookup_class,
    "action.precondition_failed": _precondition,
    "action.nothing_to_do": None,
    "action.reason_required": None,
    "action.deprecated": None,
    "action.abstract_type": None,
    "action.ambiguous_term": None,
    "action.builtin_module": None,
    "action.invalid_condition": None,
    "action.invalid_default": None,
    "action.invalid_file": None,
    "action.invalid_op": None,
    "action.invalid_value": None,
    "action.invalid_yaml": None,
    "action.lifecycle_direct_write": None,
    "action.lifecycle_missing": None,
    "action.literal_reference": None,
    "action.local_duplicate": None,
    "action.local_unresolved": None,
    "action.missing_key": None,
    "action.module_required": None,
    "action.name_mismatch": None,
    "action.name_taken": None,
    "action.not_numeric": None,
    "action.op_impossible": None,
    "action.optional_position": None,
    "action.parameter_undeclared": None,
    "action.parameter_unused": None,
    "action.placeholder_position": None,
    "action.precondition_on_invalid": None,
    "action.replaced_by_invalid": None,
    "action.stale": None,
    "action.template_invalid": None,
    "action.too_many_actions": None,
    "action.too_many_ops": None,
    "action.too_many_preconditions": None,
    "action.type_mismatch": None,
    "action.unknown_attribute": None,
    "action.unknown_class": None,
    "action.unknown_enum": None,
    "action.unknown_key": None,
    "action.unknown_module": None,
    "action.unknown_qualifier": None,
    "action.unknown_relation": None,
    "action.unknown_state": None,
    "action.unknown_type": None,
    "action.unsupported_schema": None,
    "view.invalid_file": None,
    "view.invalid_yaml": None,
    "view.missing_key": None,
    "view.unknown_key": None,
    "view.invalid_value": None,
    "view.unsupported_schema": None,
    "view.name_mismatch": None,
    "view.reserved_name": None,
    "view.flavour_mismatch": None,
    "view.duplicate_section": None,
    "view.module_required": None,
    "view.builtin_module": None,
    "view.unknown_module": None,
    "view.replaced_by_invalid": None,
    "view.unknown_class": None,
    "view.unknown_enum": None,
    "view.invalid_default": None,
    "view.invalid_example": None,
    "view.parameter_undeclared": None,
    "view.parameter_unused": None,
    "view.unknown_attribute": None,
    "view.unknown_relation": None,
    "view.ambiguous_term": None,
    "view.relation_mismatch": None,
    "view.invalid_where": None,
    "view.invalid_order": None,
    "view.type_mismatch": None,
    "view.placeholder_position": None,
    "view.unknown_function": None,
    "view.unknown_argument": None,
    "view.argument_missing": None,
    "view.literal_reference": None,
    "view.default_taken": None,
    "view.stale": None,
    "view.too_many_views": None,
    "view.section_unavailable": None,
    "view.cursor_invalid": None,
    "view.cursor_stale": _view_same_state,
    "view.parameter_missing": None,
    "view.parameter_unknown": None,
    "view.parameter_invalid": None,
    "view.parameter_type": _lookup_class,
    "view.not_found": _view_search,
    "tool.not_found": _tools,
    "tool.hash_mismatch": _tools,
    "view.takes_entity": None,
    "view.takes_no_entity": None,
    "view.class_mismatch": _lookup_class,
    "view.function_deprecated": None,
    "view.deprecated": None,
    "view.default_stale": None,
    "view.section_failed": None,
    "temporal.as_of_unsupported": None,
    "conflict.version_mismatch": _get,
    "conflict.seq_mismatch": None,
    "conflict.idempotency_key_reuse": None,
    "access.denied": _whoami,
    "access.ceiling": None,
    "access.reason_required": None,
    "access.batch_too_large": None,
    "ontology.pin_mismatch": None,
    "ontology.artifact_missing": None,
    "backend.unavailable": _init,
    "backend.lock_timeout": None,
    "write.migration_incomplete": None,  # the message names the release a maintainer re-runs
    "backend.corrupt": None,
    "audit.unavailable": None,
    "format.unsupported_version": None,
    "config.invalid": None,
    "config.forbidden_key": None,
    "policy.invalid": None,
    "skills.target_invalid": _mkdir,
    "skills.not_owned": _force,
    "skills.modified": _force,
    "skills.source_missing": _publish,
    "skills.source_invalid": None,
    "skills.check_failed": _refresh,
    "skills.outdated": _start,
    "file.unreadable": None,
    "file.too_large": None,
    "file.module_missing": None,
    "file.root_unknown": None,
    "file.origin_missing": None,
    "file.no_local_path": None,
    "files.store_refused": None,  # the message names quota and permissions
    "access.project_mismatch": None,
    "remote.unreachable": None,
    "remote.unauthorized": None,  # the message names the token settings
    "remote.protocol": None,
    "remote.outcome_unknown": _same_key,
    "remote.client_outdated": _update,
    "update.available": _update,
    "update.unavailable": None,
    "update.unsupported_install": _manual_install,
    "update.install_failed": None,
    "connect.refused": None,  # the message says what to change; a hint would echo the code
    "internal.error": None,
}
_registered: dict[str, Hint | None] = {}


def register_hints(hints: Mapping[str, Hint | None]) -> None:
    _registered.update(hints)


def known_hint_codes() -> frozenset[str]:
    return frozenset({*_BASE, *_registered})


def hint_for(code: str, details: Mapping[str, JsonValue], *, program: str) -> str | None:
    hint = _registered.get(code, _BASE.get(code))
    return hint(details, program) if hint is not None else None
