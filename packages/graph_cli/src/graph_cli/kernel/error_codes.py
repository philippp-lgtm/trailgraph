"""Error and warning code registry v1 (contracts R.1-R.3, section 1.5.1).

One registry for the whole process. The base set lives here; the admin
composition root and extensions add their codes once at startup through
``register_error_codes``. Codes are never reused with a different meaning.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

ERROR_CODE_RE: Final = re.compile(r"^[a-z]+(\.[a-z0-9_]+)+$")


class ErrorCategory(StrEnum):
    NOT_FOUND = "not_found"
    REFUSED = "refused"
    INVALID = "invalid"
    USAGE = "usage"
    DENIED = "denied"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"
    INTERNAL = "internal"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    code: str
    category: ErrorCategory
    retryable: bool
    summary: str  # one line, English, no CLI syntax

    def __post_init__(self) -> None:
        if not ERROR_CODE_RE.match(self.code):
            raise ValueError(f"invalid error code: {self.code!r}")


_C = ErrorCategory

_BASE: Final[tuple[ErrorSpec, ...]] = (
    ErrorSpec("usage.invalid_argument", _C.USAGE, False, "An argument has an invalid value."),
    ErrorSpec("usage.unknown_command", _C.USAGE, False, "The command does not exist."),
    ErrorSpec("usage.missing_argument", _C.USAGE, False, "A required argument is missing."),
    ErrorSpec("usage.invalid_input", _C.USAGE, False, "Input is not valid JSON, JSONL or UTF-8."),
    ErrorSpec("graph.not_selected", _C.USAGE, False, "No graph was selected."),
    ErrorSpec("graph.not_found", _C.NOT_FOUND, False, "The graph does not exist."),
    ErrorSpec("graph.not_in_project", _C.REFUSED, False, "The graph is not allowed here."),
    ErrorSpec("graph.archived", _C.REFUSED, False, "The graph is archived."),
    ErrorSpec("graph.readonly", _C.REFUSED, False, "The graph is read-only."),
    ErrorSpec("entity.not_found", _C.NOT_FOUND, False, "The entity does not exist."),
    ErrorSpec("entity.ambiguous_ref", _C.REFUSED, False, "The reference matches several entities."),
    ErrorSpec("entity.revision_not_found", _C.NOT_FOUND, False, "The entity has no such revision."),
    ErrorSpec("term.unknown", _C.NOT_FOUND, False, "Unknown type, relation or attribute name."),
    ErrorSpec("term.no_labels", _C.REFUSED, False, "The requested types have no label roles."),
    ErrorSpec("term.no_text_fields", _C.REFUSED, False, "The requested types have no text fields."),
    ErrorSpec("changeset.malformed", _C.USAGE, False, "The change set document is malformed."),
    ErrorSpec("changeset.local_ref_unresolved", _C.REFUSED, False, "A local name is not bound."),
    ErrorSpec("changeset.local_ref_duplicate", _C.REFUSED, False, "A local name is bound twice."),
    ErrorSpec("changeset.too_large", _C.REFUSED, False, "The change set exceeds the size limits."),
    ErrorSpec("write.invalid", _C.INVALID, False, "The write violates the ontology."),
    # Reconcile guards (plan R-K7): found under the graph lock, so audited ``invalid`` refusals.
    ErrorSpec(
        "reconcile.source_empty",
        _C.INVALID,
        False,
        "A complete scope has no items while the graph holds entities in it.",
    ),
    ErrorSpec(
        "reconcile.retire_limit",
        _C.INVALID,
        False,
        "A complete scope would retire more entities than the limit allows.",
    ),
    ErrorSpec(
        "reconcile.shape",
        _C.INVALID,
        False,
        "The number of items of a scope is outside its expected range.",
    ),
    ErrorSpec(
        "reconcile.key_attribute",
        _C.INVALID,
        False,
        "The key attribute of a scope is missing or not usable as a key.",
    ),
    ErrorSpec(
        "reconcile.scope_overlap",
        _C.INVALID,
        False,
        "Two scopes of one document cover the same entities.",
    ),
    ErrorSpec(
        "reconcile.scope_conflict",
        _C.INVALID,
        False,
        "An op of the document touches an entity of a reconcile scope.",
    ),
    ErrorSpec(
        "reconcile.move_invalid",
        _C.INVALID,
        False,
        "A move names no entity, a taken key, or a key twice.",
    ),
    ErrorSpec(
        "reconcile.target_not_found",
        _C.INVALID,
        False,
        "An edge target of a reconcile item matches no single entity.",
    ),
    ErrorSpec(
        "reconcile.type_mismatch",
        _C.INVALID,
        False,
        "The type of an item does not fit its scope or the entity holding its key.",
    ),
    ErrorSpec("query.syntax", _C.USAGE, False, "The query text is not valid Cypher."),
    ErrorSpec("query.unsupported", _C.REFUSED, False, "The query uses an unsupported construct."),
    ErrorSpec("query.ambiguous_term", _C.REFUSED, False, "A short name matches several terms."),
    ErrorSpec("query.unbound_variable", _C.USAGE, False, "The query uses an undefined variable."),
    ErrorSpec("query.parameter_missing", _C.USAGE, False, "A query parameter has no value."),
    ErrorSpec("query.too_large", _C.REFUSED, False, "The query produces too many rows."),
    ErrorSpec(
        "query.invalid_value",
        _C.INVALID,
        False,
        "A query value has the wrong type or cannot be computed.",
    ),
    ErrorSpec("function.not_found", _C.NOT_FOUND, False, "The graph has no such function."),
    ErrorSpec("function.parameter_missing", _C.USAGE, False, "A function parameter has no value."),
    ErrorSpec("function.parameter_unknown", _C.USAGE, False, "The function has no such parameter."),
    ErrorSpec(
        "function.parameter_invalid",
        _C.USAGE,
        False,
        "A function parameter value does not fit its declared type.",
    ),
    ErrorSpec(
        "function.parameter_type",
        _C.REFUSED,
        False,
        "A reference parameter names an entity of the wrong class.",
    ),
    ErrorSpec("function.builtin_module", _C.INVALID, False, "A function names a built-in module."),
    ErrorSpec(
        "function.invalid_default",
        _C.INVALID,
        False,
        "A parameter default does not fit its declared type.",
    ),
    ErrorSpec(
        "function.invalid_file", _C.INVALID, False, "The functions folder holds something else."
    ),
    ErrorSpec(
        "function.invalid_example",
        _C.INVALID,
        False,
        "An example of a function is malformed.",
    ),
    ErrorSpec(
        "function.invalid_value",
        _C.INVALID,
        False,
        "A key of a function file has a value of the wrong kind.",
    ),
    ErrorSpec("function.invalid_yaml", _C.INVALID, False, "A function file is not valid YAML."),
    ErrorSpec("function.missing_key", _C.INVALID, False, "A function file lacks a required key."),
    ErrorSpec(
        "function.module_required",
        _C.INVALID,
        False,
        "A function of a multi-module graph names no module.",
    ),
    ErrorSpec(
        "function.name_mismatch",
        _C.INVALID,
        False,
        "A function's name differs from its file name.",
    ),
    ErrorSpec(
        "function.parameter_paging",
        _C.INVALID,
        False,
        "A function uses a parameter as SKIP or LIMIT.",
    ),
    ErrorSpec(
        "function.parameter_undeclared",
        _C.INVALID,
        False,
        "A function's query uses a parameter that is not declared.",
    ),
    ErrorSpec(
        "function.parameter_unused",
        _C.INVALID,
        False,
        "A function declares a parameter its query does not use.",
    ),
    ErrorSpec(
        "function.pattern_impossible",
        _C.INVALID,
        False,
        "A relationship pattern no relation domain and range allows.",
    ),
    ErrorSpec(
        "function.query_too_long",
        _C.INVALID,
        False,
        "A function's query text is too long.",
    ),
    ErrorSpec(
        "function.replaced_by_invalid",
        _C.INVALID,
        False,
        "A function is replaced by a function that is not active.",
    ),
    ErrorSpec(
        "function.returns_mismatch",
        _C.INVALID,
        False,
        "A function's returns do not list the result columns in order.",
    ),
    ErrorSpec(
        "function.returns_type_mismatch",
        _C.INVALID,
        False,
        "A column's declared type contradicts the type its query makes certain.",
    ),
    ErrorSpec(
        "function.returns_star",
        _C.INVALID,
        False,
        "A function's query returns *, which has no stable columns.",
    ),
    ErrorSpec(
        "function.round_trip_failed",
        _C.INVALID,
        False,
        "A function's normalized query does not read back to the same query (a defect).",
    ),
    ErrorSpec(
        "function.stale",
        _C.INVALID,
        False,
        "A function no longer resolves against the graph's ontology.",
    ),
    ErrorSpec(
        "function.too_many_functions",
        _C.INVALID,
        False,
        "A graph has more functions than the limit.",
    ),
    ErrorSpec(
        "function.type_mismatch",
        _C.INVALID,
        False,
        "A parameter's type does not fit the field it is compared with.",
    ),
    ErrorSpec(
        "function.unknown_class",
        _C.INVALID,
        False,
        "A parameter names a class the ontology does not have.",
    ),
    ErrorSpec(
        "function.unknown_enum",
        _C.INVALID,
        False,
        "A parameter names an enum the ontology does not have.",
    ),
    ErrorSpec(
        "function.unknown_key",
        _C.INVALID,
        False,
        "A function file has a key the format does not define.",
    ),
    ErrorSpec(
        "function.unknown_module",
        _C.INVALID,
        False,
        "A function names a module the ontology does not have.",
    ),
    ErrorSpec(
        "function.unsupported_schema",
        _C.INVALID,
        False,
        "A function file has a format version this build cannot read.",
    ),
    ErrorSpec(
        "functions.slot_invalid", _C.INVALID, False, "The published function catalog is invalid."
    ),
    ErrorSpec(
        "actions.slot_invalid", _C.INVALID, False, "The published action catalog is invalid."
    ),
    ErrorSpec("views.slot_invalid", _C.INVALID, False, "The published view catalog is invalid."),
    ErrorSpec(
        "function.name_taken",
        _C.INVALID,
        False,
        "A function is named like a published action.",
    ),
    ErrorSpec(
        "action.not_found",
        _C.NOT_FOUND,
        False,
        "The graph has no such action.",
    ),
    ErrorSpec(
        "action.parameter_missing",
        _C.USAGE,
        False,
        "An action parameter has no value.",
    ),
    ErrorSpec(
        "action.parameter_unknown",
        _C.USAGE,
        False,
        "The action has no such parameter.",
    ),
    ErrorSpec(
        "action.parameter_invalid",
        _C.USAGE,
        False,
        "An action parameter value does not fit its declared type.",
    ),
    ErrorSpec(
        "action.parameter_type",
        _C.REFUSED,
        False,
        "A reference parameter names an entity of the wrong class.",
    ),
    ErrorSpec(
        "action.precondition_failed",
        _C.INVALID,
        False,
        "A precondition of the action does not hold in the current state.",
    ),
    ErrorSpec(
        "action.nothing_to_do",
        _C.REFUSED,
        False,
        "The action expands to no op in the current state.",
    ),
    ErrorSpec(
        "action.reason_required",
        _C.USAGE,
        False,
        "The action demands a reason and none was given.",
    ),
    ErrorSpec(
        "action.abstract_type",
        _C.INVALID,
        False,
        "An action creates an entity of an abstract class.",
    ),
    ErrorSpec(
        "action.ambiguous_term",
        _C.INVALID,
        False,
        "An action names a short term that matches several terms.",
    ),
    ErrorSpec(
        "action.builtin_module",
        _C.INVALID,
        False,
        "An action names a built-in module.",
    ),
    ErrorSpec(
        "action.invalid_condition",
        _C.INVALID,
        False,
        "A precondition's conditions are not valid.",
    ),
    ErrorSpec(
        "action.invalid_default",
        _C.INVALID,
        False,
        "A parameter default does not fit its declared type.",
    ),
    ErrorSpec("action.invalid_file", _C.INVALID, False, "The actions folder holds something else."),
    ErrorSpec(
        "action.invalid_op",
        _C.INVALID,
        False,
        "An op of an action template is not allowed.",
    ),
    ErrorSpec(
        "action.invalid_value",
        _C.INVALID,
        False,
        "A key of an action file has a value of the wrong kind.",
    ),
    ErrorSpec(
        "action.invalid_yaml",
        _C.INVALID,
        False,
        "An action file is not valid YAML.",
    ),
    ErrorSpec(
        "action.lifecycle_direct_write",
        _C.INVALID,
        False,
        "An action writes a lifecycle attribute directly.",
    ),
    ErrorSpec(
        "action.lifecycle_missing",
        _C.INVALID,
        False,
        "A transition names a class without that lifecycle attribute.",
    ),
    ErrorSpec(
        "action.literal_reference",
        _C.INVALID,
        False,
        "An op names an entity by a literal reference.",
    ),
    ErrorSpec(
        "action.local_duplicate",
        _C.INVALID,
        False,
        "An action binds the same local name twice.",
    ),
    ErrorSpec(
        "action.local_unresolved",
        _C.INVALID,
        False,
        "An op uses a local name no create of the action binds.",
    ),
    ErrorSpec(
        "action.missing_key",
        _C.INVALID,
        False,
        "An action file lacks a required key.",
    ),
    ErrorSpec(
        "action.module_required",
        _C.INVALID,
        False,
        "An action of a multi-module graph names no module.",
    ),
    ErrorSpec(
        "action.name_mismatch",
        _C.INVALID,
        False,
        "An action's name differs from its file name.",
    ),
    ErrorSpec(
        "action.name_taken",
        _C.INVALID,
        False,
        "An action has the name of a function of the graph.",
    ),
    ErrorSpec(
        "action.not_numeric",
        _C.INVALID,
        False,
        "A precondition compares or sums an attribute that is not a number.",
    ),
    ErrorSpec(
        "action.op_impossible",
        _C.INVALID,
        False,
        "An op no relation domain and range allows.",
    ),
    ErrorSpec(
        "action.optional_position",
        _C.INVALID,
        False,
        "An optional parameter stands where its entry cannot be dropped.",
    ),
    ErrorSpec(
        "action.parameter_undeclared",
        _C.INVALID,
        False,
        "An action uses a parameter that is not declared.",
    ),
    ErrorSpec(
        "action.parameter_unused",
        _C.INVALID,
        False,
        "An action declares a parameter nothing uses.",
    ),
    ErrorSpec(
        "action.placeholder_position",
        _C.INVALID,
        False,
        "A parameter stands where no placeholder is allowed.",
    ),
    ErrorSpec(
        "action.precondition_on_invalid",
        _C.INVALID,
        False,
        "A precondition names something other than a ref parameter.",
    ),
    ErrorSpec(
        "action.replaced_by_invalid",
        _C.INVALID,
        False,
        "An action is replaced by an action that is not active.",
    ),
    ErrorSpec(
        "action.stale",
        _C.INVALID,
        False,
        "An action no longer compiles against the graph's ontology.",
    ),
    ErrorSpec(
        "action.template_invalid",
        _C.INVALID,
        False,
        "An action's ops do not assemble into a change set.",
    ),
    ErrorSpec(
        "action.too_many_actions",
        _C.INVALID,
        False,
        "A graph has more actions than the limit.",
    ),
    ErrorSpec(
        "action.too_many_ops",
        _C.INVALID,
        False,
        "An action has more ops than the limit.",
    ),
    ErrorSpec(
        "action.too_many_preconditions",
        _C.INVALID,
        False,
        "An action has more preconditions than the limit.",
    ),
    ErrorSpec(
        "action.type_mismatch",
        _C.INVALID,
        False,
        "A parameter's type does not fit what it stands for.",
    ),
    ErrorSpec(
        "action.unknown_attribute",
        _C.INVALID,
        False,
        "An action names an attribute the class does not have.",
    ),
    ErrorSpec(
        "action.unknown_class",
        _C.INVALID,
        False,
        "A parameter names a class the ontology does not have.",
    ),
    ErrorSpec(
        "action.unknown_enum",
        _C.INVALID,
        False,
        "A parameter names an enum the ontology does not have.",
    ),
    ErrorSpec(
        "action.unknown_key",
        _C.INVALID,
        False,
        "An action file has a key the format does not define.",
    ),
    ErrorSpec(
        "action.unknown_module",
        _C.INVALID,
        False,
        "An action names a module the ontology does not have.",
    ),
    ErrorSpec(
        "action.unknown_qualifier",
        _C.INVALID,
        False,
        "An action names a qualifier the relation does not have.",
    ),
    ErrorSpec(
        "action.unknown_relation",
        _C.INVALID,
        False,
        "An action names a relation the ontology does not have.",
    ),
    ErrorSpec(
        "action.unknown_state",
        _C.INVALID,
        False,
        "A transition names a state the lifecycle does not have.",
    ),
    ErrorSpec(
        "action.unknown_type",
        _C.INVALID,
        False,
        "An action names a class the ontology does not have.",
    ),
    ErrorSpec(
        "action.unsupported_schema",
        _C.INVALID,
        False,
        "An action file has a format version this build cannot read.",
    ),
    ErrorSpec("view.invalid_file", _C.INVALID, False, "The views folder holds something else."),
    ErrorSpec(
        "view.invalid_yaml",
        _C.INVALID,
        False,
        "A view file is not valid YAML.",
    ),
    ErrorSpec(
        "view.missing_key",
        _C.INVALID,
        False,
        "A view file lacks a required key.",
    ),
    ErrorSpec(
        "view.unknown_key",
        _C.INVALID,
        False,
        "A view file has a key the format does not know.",
    ),
    ErrorSpec(
        "view.invalid_value",
        _C.INVALID,
        False,
        "A key of a view file has a value of the wrong kind.",
    ),
    ErrorSpec(
        "view.unsupported_schema",
        _C.INVALID,
        False,
        "A view file has a format version this build cannot read.",
    ),
    ErrorSpec(
        "view.name_mismatch",
        _C.INVALID,
        False,
        "A view file is not named like the view.",
    ),
    ErrorSpec(
        "view.reserved_name",
        _C.INVALID,
        False,
        "A view is named like the generated card.",
    ),
    ErrorSpec(
        "view.flavour_mismatch",
        _C.INVALID,
        False,
        "A key or section kind belongs to the other kind of view.",
    ),
    ErrorSpec(
        "view.duplicate_section",
        _C.INVALID,
        False,
        "Two sections of a view have the same id.",
    ),
    ErrorSpec(
        "view.module_required",
        _C.INVALID,
        False,
        "A view of a multi-module graph names no module.",
    ),
    ErrorSpec(
        "view.builtin_module",
        _C.INVALID,
        False,
        "A view names a built-in module.",
    ),
    ErrorSpec(
        "view.unknown_module",
        _C.INVALID,
        False,
        "A view names a module the graph does not have.",
    ),
    ErrorSpec(
        "view.replaced_by_invalid",
        _C.INVALID,
        False,
        "A view names a replacement that is not an active view.",
    ),
    ErrorSpec(
        "view.unknown_class",
        _C.INVALID,
        False,
        "A view names a class the ontology does not have.",
    ),
    ErrorSpec(
        "view.unknown_enum",
        _C.INVALID,
        False,
        "A view parameter names an enum the ontology does not have.",
    ),
    ErrorSpec(
        "view.invalid_default",
        _C.INVALID,
        False,
        "A parameter default does not fit its declared type.",
    ),
    ErrorSpec(
        "view.invalid_example",
        _C.INVALID,
        False,
        "An example of a view is malformed.",
    ),
    ErrorSpec(
        "view.parameter_undeclared",
        _C.INVALID,
        False,
        "A view uses a parameter it does not declare.",
    ),
    ErrorSpec(
        "view.parameter_unused",
        _C.INVALID,
        False,
        "A view declares a parameter that nothing uses.",
    ),
    ErrorSpec(
        "view.unknown_attribute",
        _C.INVALID,
        False,
        "A view names an attribute no class of its section has.",
    ),
    ErrorSpec(
        "view.unknown_relation",
        _C.INVALID,
        False,
        "A view names a relation the ontology does not have.",
    ),
    ErrorSpec(
        "view.ambiguous_term",
        _C.INVALID,
        False,
        "A view names a relation or class by a local name several terms have.",
    ),
    ErrorSpec(
        "view.relation_mismatch",
        _C.INVALID,
        False,
        "A view reads a relation from a class it does not start at.",
    ),
    ErrorSpec(
        "view.invalid_where",
        _C.INVALID,
        False,
        "A where entry of a view section does not plan.",
    ),
    ErrorSpec(
        "view.invalid_order",
        _C.INVALID,
        False,
        "An order entry of a view section does not plan.",
    ),
    ErrorSpec(
        "view.type_mismatch",
        _C.INVALID,
        False,
        "A view parameter or argument does not fit what it is used for.",
    ),
    ErrorSpec(
        "view.placeholder_position",
        _C.INVALID,
        False,
        "A placeholder stands where it is not allowed.",
    ),
    ErrorSpec(
        "view.unknown_function",
        _C.INVALID,
        False,
        "A rows section names a function the graph does not have.",
    ),
    ErrorSpec(
        "view.unknown_argument",
        _C.INVALID,
        False,
        "A rows section passes an argument the function does not take.",
    ),
    ErrorSpec(
        "view.argument_missing",
        _C.INVALID,
        False,
        "A rows section leaves out a required function parameter.",
    ),
    ErrorSpec(
        "view.literal_reference",
        _C.INVALID,
        False,
        "A rows section gives an entity reference as a literal.",
    ),
    ErrorSpec(
        "view.default_taken",
        _C.INVALID,
        False,
        "Two views are the default view of the same class.",
    ),
    ErrorSpec(
        "view.stale",
        _C.INVALID,
        False,
        "A published view no longer reads as compiled against this model.",
    ),
    ErrorSpec(
        "view.too_many_views",
        _C.INVALID,
        False,
        "A graph has more views than the limit.",
    ),
    ErrorSpec(
        "view.section_unavailable",
        _C.REFUSED,
        False,
        "A section of a view cannot be answered here.",
    ),
    ErrorSpec(
        "view.cursor_invalid",
        _C.USAGE,
        False,
        "A page cursor is malformed or belongs to another view or other parameters.",
    ),
    ErrorSpec(
        "view.cursor_stale",
        _C.REFUSED,
        False,
        "The graph changed since the page cursor was issued.",
    ),
    ErrorSpec(
        "view.parameter_missing",
        _C.USAGE,
        False,
        "A view parameter has no value.",
    ),
    ErrorSpec(
        "view.parameter_unknown",
        _C.USAGE,
        False,
        "The view has no such parameter.",
    ),
    ErrorSpec(
        "view.parameter_type",
        _C.REFUSED,
        False,
        "A reference parameter names an entity of the wrong class.",
    ),
    ErrorSpec(
        "view.parameter_invalid",
        _C.USAGE,
        False,
        "A view parameter value does not fit its declared type.",
    ),
    ErrorSpec(
        "view.not_found",
        _C.NOT_FOUND,
        False,
        "The graph has no such view.",
    ),
    ErrorSpec(
        "view.takes_entity",
        _C.USAGE,
        False,
        "The view shows one entity; name it.",
    ),
    ErrorSpec(
        "view.takes_no_entity",
        _C.USAGE,
        False,
        "The view takes no entity; leave the reference out.",
    ),
    ErrorSpec(
        "view.class_mismatch",
        _C.REFUSED,
        False,
        "The entity is not of the class the view takes.",
    ),
    ErrorSpec(
        "temporal.as_of_unsupported",
        _C.REFUSED,
        False,
        "The storage backend cannot read an earlier state.",
    ),
    ErrorSpec("tool.not_found", _C.NOT_FOUND, False, "The graph has no such tool for this caller."),
    ErrorSpec(
        "tool.hash_mismatch",
        _C.CONFLICT,
        False,
        "The tool is not the one the caller built its definition from.",
    ),
    ErrorSpec("conflict.version_mismatch", _C.CONFLICT, True, "The entity has changed."),
    ErrorSpec("conflict.seq_mismatch", _C.CONFLICT, True, "The graph changed in the meantime."),
    ErrorSpec("conflict.idempotency_key_reuse", _C.CONFLICT, False, "Key reused, content differs."),
    ErrorSpec("access.denied", _C.DENIED, False, "The action is not permitted."),
    ErrorSpec("access.ceiling", _C.DENIED, False, "The action is not available to this program."),
    ErrorSpec("access.reason_required", _C.DENIED, False, "The action requires a reason."),
    ErrorSpec("access.batch_too_large", _C.DENIED, False, "The batch exceeds the permitted size."),
    ErrorSpec("ontology.pin_mismatch", _C.UNAVAILABLE, False, "Ontology artifact hash differs."),
    ErrorSpec("ontology.artifact_missing", _C.UNAVAILABLE, False, "Ontology release is missing."),
    ErrorSpec("backend.unavailable", _C.UNAVAILABLE, True, "The storage backend is unavailable."),
    ErrorSpec("backend.lock_timeout", _C.UNAVAILABLE, True, "The graph lock was not acquired."),
    ErrorSpec(
        "write.migration_incomplete",
        _C.UNAVAILABLE,
        True,
        "A data migration is committed but not pinned yet; writes wait.",
    ),
    ErrorSpec("backend.corrupt", _C.UNAVAILABLE, False, "The transaction log is damaged."),
    ErrorSpec("audit.unavailable", _C.UNAVAILABLE, True, "The audit log could not be written."),
    ErrorSpec("format.unsupported_version", _C.UNAVAILABLE, False, "Unsupported format version."),
    ErrorSpec("config.invalid", _C.UNAVAILABLE, False, "The configuration is invalid."),
    ErrorSpec("config.forbidden_key", _C.UNAVAILABLE, False, "A configuration key is not allowed."),
    ErrorSpec("policy.invalid", _C.UNAVAILABLE, False, "The policy document is invalid."),
    ErrorSpec("skills.target_invalid", _C.REFUSED, False, "The skill directory is not usable."),
    ErrorSpec("skills.not_owned", _C.REFUSED, False, "A skill directory was not installed by us."),
    ErrorSpec("skills.modified", _C.REFUSED, False, "An installed skill was edited by hand."),
    ErrorSpec("skills.source_missing", _C.NOT_FOUND, False, "The graph has no published skill."),
    ErrorSpec("skills.source_invalid", _C.INVALID, False, "The published skill is invalid."),
    ErrorSpec("skills.check_failed", _C.INVALID, False, "Installed skills are not up to date."),
    ErrorSpec(
        "skills.outdated", _C.REFUSED, False, "An installed skill is older than what it describes."
    ),
    ErrorSpec("file.unreadable", _C.USAGE, False, "The path is not a readable regular file."),
    ErrorSpec("file.too_large", _C.REFUSED, False, "The file exceeds the size limit."),
    ErrorSpec("file.module_missing", _C.REFUSED, False, "The graph's model has no files module."),
    ErrorSpec("file.root_unknown", _C.NOT_FOUND, False, "The file root is not configured here."),
    ErrorSpec("file.origin_missing", _C.NOT_FOUND, False, "The origin of the file is missing."),
    ErrorSpec("file.no_local_path", _C.REFUSED, False, "The backend gives no local file path."),
    ErrorSpec(
        "files.store_refused", _C.REFUSED, False, "The object store refused to store a file."
    ),
    ErrorSpec(
        "access.project_mismatch", _C.DENIED, False, "The token is bound to another project."
    ),
    ErrorSpec("remote.unreachable", _C.UNAVAILABLE, True, "The server cannot be reached."),
    ErrorSpec("remote.unauthorized", _C.DENIED, False, "The server refused the token."),
    ErrorSpec("remote.protocol", _C.UNAVAILABLE, False, "The server's answer is not understood."),
    ErrorSpec("remote.outcome_unknown", _C.UNAVAILABLE, True, "The answer to a write was lost."),
    ErrorSpec(
        "remote.client_outdated",
        _C.REFUSED,
        False,
        "The server runs another build of this program.",
    ),
    ErrorSpec(
        "update.available", _C.INVALID, False, "Another program build or skill is available."
    ),
    ErrorSpec("update.unavailable", _C.UNAVAILABLE, False, "The server hands out no program."),
    ErrorSpec(
        "update.unsupported_install", _C.REFUSED, False, "The program cannot replace itself."
    ),
    ErrorSpec("update.install_failed", _C.UNAVAILABLE, False, "The program was not installed."),
    ErrorSpec("connect.refused", _C.REFUSED, False, "This machine or folder is set up otherwise."),
    ErrorSpec("internal.error", _C.INTERNAL, False, "Internal error."),
    # Warnings never change the exit code.
    ErrorSpec("actor.facet_malformed", _C.WARNING, False, "An identity value was ignored."),
    ErrorSpec("root.overridden", _C.WARNING, False, "The data root was overridden."),
    ErrorSpec(
        "access.project_unknown", _C.WARNING, False, "The policy has no scope for this project."
    ),
    ErrorSpec(
        "policy.role_unknown", _C.WARNING, False, "A policy rule names a role that is not declared."
    ),
    ErrorSpec(
        "policy.catalog_widened",
        _C.WARNING,
        False,
        "A grant of the whole graph also covers the callers of a catalog grant.",
    ),
    ErrorSpec("entity.superseded", _C.WARNING, False, "The entity is superseded."),
    ErrorSpec("entity.retracted", _C.WARNING, False, "The entity is retracted."),
    ErrorSpec("entity.alias_renamed", _C.WARNING, False, "The alias was renamed."),
    ErrorSpec("entity.unchanged", _C.WARNING, False, "A write left entities as they were."),
    ErrorSpec(
        "storage.materialization_deferred", _C.WARNING, False, "Derived files are not updated yet."
    ),
    ErrorSpec("storage.recovery_pending", _C.WARNING, False, "The graph needs recovery."),
    ErrorSpec("storage.index_stale", _C.WARNING, False, "The index is stale; a scan was used."),
    ErrorSpec("audit.refusal_not_recorded", _C.WARNING, False, "A refusal was not audited."),
    ErrorSpec("ontology.pin_mismatch_read", _C.WARNING, False, "Read with a mismatching artifact."),
    ErrorSpec("graph.archived_read", _C.WARNING, False, "The graph is archived."),
    ErrorSpec(
        "graph.migration_incomplete",
        _C.WARNING,
        False,
        "A data migration is committed but not pinned; reads use its version.",
    ),
    ErrorSpec(
        "query.number_for_text",
        _C.WARNING,
        False,
        "A query compares a text field with a number, which never equals text.",
    ),
    ErrorSpec("function.deprecated", _C.WARNING, False, "The function is deprecated."),
    ErrorSpec("action.deprecated", _C.WARNING, False, "The action is deprecated."),
    ErrorSpec("view.function_deprecated", _C.WARNING, False, "A view uses a deprecated function."),
    ErrorSpec("view.deprecated", _C.WARNING, False, "The view is deprecated."),
    ErrorSpec(
        "view.default_stale",
        _C.WARNING,
        False,
        "The default view of a class no longer fits the model; the generated card was shown.",
    ),
    ErrorSpec(
        "view.section_failed", _C.WARNING, False, "A section of a view could not be answered."
    ),
    ErrorSpec("temporal.as_of_read", _C.WARNING, False, "An earlier state was read."),
    ErrorSpec("output.spilled", _C.WARNING, False, "A large answer was written to a file."),
    ErrorSpec("output.spill_failed", _C.WARNING, False, "A large answer could not go to a file."),
    ErrorSpec(
        "graph.unreadable", _C.WARNING, False, "A graph could not be read; it is not listed."
    ),
    ErrorSpec("config.unknown_key", _C.WARNING, False, "A configuration key is unknown."),
    ErrorSpec(
        "remote.version_differs", _C.WARNING, False, "The server runs another version or build."
    ),
    ErrorSpec("remote.plain_http", _C.WARNING, False, "The token travels without encryption."),
    ErrorSpec(
        "skills.source_behind_ontology",
        _C.WARNING,
        False,
        "A graph skill was published for an older ontology version.",
    ),
    ErrorSpec(
        "skills.source_unknown_names",
        _C.WARNING,
        False,
        "A graph skill names terms, commands or aliases that do not exist.",
    ),
    ErrorSpec(
        "skills.stale", _C.WARNING, False, "An installed skill is older than what it describes."
    ),
    ErrorSpec("extension.api_mismatch", _C.WARNING, False, "An extension targets another API."),
    ErrorSpec("file.already_present", _C.WARNING, False, "The graph already stores this file."),
    ErrorSpec("file.no_root", _C.WARNING, False, "No configured file root contains the file."),
    ErrorSpec(
        "file.extraction_incomplete",
        _C.WARNING,
        False,
        "The file is stored; some of its metadata could not be written now.",
    ),
)

ERROR_CODES: Final[Mapping[str, ErrorSpec]] = MappingProxyType({s.code: s for s in _BASE})
assert len(ERROR_CODES) == len(_BASE), "duplicate error code in the base registry"

_registered: dict[str, ErrorSpec] = {}


def register_error_codes(specs: Iterable[ErrorSpec]) -> None:
    """Add codes owned outside the kernel (``admin.*``, ``ext.<ns>.*``).

    Re-registering an identical spec is a no-op; a different spec under a known
    code is refused, because codes never change meaning.
    """
    for spec in specs:
        known = ERROR_CODES.get(spec.code) or _registered.get(spec.code)
        if known is not None and known != spec:
            raise ValueError(f"error code {spec.code!r} is already registered differently")
        _registered[spec.code] = spec


def lookup_error_code(code: str) -> ErrorSpec | None:
    return ERROR_CODES.get(code) or _registered.get(code)


def all_error_codes() -> Mapping[str, ErrorSpec]:
    """Base registry plus everything registered so far, sorted by code."""
    merged = {**ERROR_CODES, **_registered}
    return MappingProxyType(dict(sorted(merged.items())))
