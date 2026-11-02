"""Checking and compiling actions against a compiled ontology (actions plan A9).

``compile_actions`` reads the action files of a graph, checks each against the
model and returns the catalog document ``graph_cli.action_catalog/1`` together with
every problem found; the document holds only actions without problems, and a
caller publishes only when there are none. Nothing needs data or parameter
values. The checks, in the order of the plan:

1. the file (from the parser), the name against the functions of the graph,
   ``module``, ``replaced_by``;
2. parameters: every ``$p`` declared and every declared parameter used, ``ref``
   classes and enums exist, defaults bind;
3. ops: types, relations, attributes, qualifiers and states exist; a
   placeholder fits the datatype of the attribute or qualifier it stands for,
   or the domain and range of the relation (with subclasses); a lifecycle
   attribute changes only by ``transition`` (as the write validator demands);
4. the template assembles: with typed placeholder values (and one fake edge
   per ``all: true``) the ops form a document ``parse_changeset`` accepts, also
   with every optional parameter absent;
5. preconditions: ``on`` names a ``ref`` parameter and the conditions compile
   against its class (the checks of rule conditions in the ontology
   validator);
6. limits.

The compiled entry stores the declaration, the authored source (ops and
preconditions as JSON, enough to compile again), the normalized ops (CURIEs),
the lowered preconditions, ``touches``, ``needs`` (the ``write.*`` action and
type pairs the ops are authorized as) and the hash. ``check_actions`` is the
release invariant: it compiles the stored sources against another model, and
the stored normalized form must still compile (``action.stale``).
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final

from graph_cli.kernel.canonical import JsonObject, JsonValue, hash_document
from graph_cli.kernel.changeset import OP_ACTIONS, parse_changeset
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.json_access import as_object, req_list, req_obj, req_str
from graph_cli.kinetic.action_format import (
    ABSENT,
    CATALOG_SCHEMA,
    PLACEHOLDER_RE,
    Absent,
    ActionDef,
    OpScan,
    PreconditionDef,
    Use,
    is_dynamic_unlink,
    load_action,
    path_text,
    scan_op,
    substitute,
)
from graph_cli.kinetic.bind import placeholder
from graph_cli.kinetic.compile import FITS, resolve_module
from graph_cli.kinetic.params import (
    PARAM_RE,
    ParamDef,
    Problem,
    bind_json,
    json_value,
    text_value,
)
from graph_cli.ontology_layer.connecting import connecting
from graph_cli.ontology_layer.expressions import (
    AttrCond,
    AttrRef,
    Cond,
    CountCond,
    SumCond,
    iter_conditions,
    lower,
)
from graph_cli.ontology_layer.model import AttributeDef, CompiledModel
from graph_cli.ontology_layer.vocabulary import LANG_BASES
from graph_cli.query.plan import resolve_term

MAX_ACTIONS: Final = 1000
MAX_OPS: Final = 50
MAX_PRECONDITIONS: Final = 10

# Placeholders for the entity ids and the edge id a template is assembled with.
_FAKE_ENTITY: Final = "00000000-0000-7000-8000-000000000001"
_FAKE_EDGE: Final = "00000000-0000-7000-8000-0000000000e1"

_ENTITY_ROLES: Final = ("entity", "entities")
_LIST_KEYS: Final = ("add", "add_text", "remove", "remove_text")


def compile_actions(
    sources: Mapping[str, str | bytes],
    model: CompiledModel,
    *,
    function_names: frozenset[str] = frozenset(),
) -> tuple[dict[str, JsonValue], list[Problem]]:
    """Compile the action files (file name -> content) of one graph against ``model``.

    ``function_names`` are the functions of the graph: actions and functions share
    one namespace (``action.name_taken``).
    """
    problems: list[Problem] = []
    defs: list[ActionDef] = []
    for file_name in sorted(sources):
        defn, found = load_action(file_name, sources[file_name])
        problems.extend(found)
        if defn is not None:
            defs.append(defn)
    if len(sources) > MAX_ACTIONS:
        problems.append(_too_many(len(sources)))
    document, found = _compile(defs, model, function_names)
    return document, [*problems, *found]


def check_actions(
    document: JsonObject,
    model: CompiledModel,
    *,
    function_names: frozenset[str] = frozenset(),
) -> list[Problem]:
    """The problems a published catalog has against ``model`` (none: it still holds).

    Every action is compiled again from its stored source, and its stored
    normalized form must still compile (``action.stale``). A document of another
    shape raises ``JsonShapeError``, an unknown schema ``format.unsupported_version``.
    """
    schema = req_str(document, "schema")
    if schema != CATALOG_SCHEMA:
        raise GraphCliError(
            "format.unsupported_version",
            f"unsupported action catalog format {schema!r}; expected {CATALOG_SCHEMA}",
            details={"expected": CATALOG_SCHEMA, "actual": schema},
        )
    entries = [as_object(e, "actions") for e in req_list(document, "actions")]
    active_names = frozenset(
        req_str(e, "name") for e in entries if req_str(e, "status") == "active"
    )
    problems: list[Problem] = []
    for entry in entries:
        try:
            defn = ActionDef.from_json(entry, req_obj(entry, "source", "actions."))
        except GraphCliError as exc:
            problems.append(
                _stale_problem(
                    str(entry.get("name")),
                    f"the stored action no longer reads: {exc.message}",
                    "source",
                )
            )
            continue
        check = _Compile(defn, model, active_names, function_names)
        fresh = check.run()
        problems.extend(check.problems)
        found = _check_stored(defn, entry, model, function_names)
        if found is not None:
            problems.append(found)
        elif fresh is not None and (
            fresh["ops"] != entry.get("ops") or fresh["preconditions"] != entry.get("preconditions")
        ):
            problems.append(
                _stale_problem(
                    defn.name, "the stored ops or preconditions read differently against this model"
                )
            )
    if len(entries) > MAX_ACTIONS:
        problems.append(_too_many(len(entries)))
    return problems


def _too_many(count: int) -> Problem:
    return Problem(
        "(catalog)",
        "action.too_many_actions",
        f"{count} actions; at most {MAX_ACTIONS} per graph",
        details={"count": count, "limit": MAX_ACTIONS},
        kind="action",
    )


def _compile(
    defs: Sequence[ActionDef], model: CompiledModel, function_names: frozenset[str]
) -> tuple[dict[str, JsonValue], list[Problem]]:
    active = frozenset(d.name for d in defs if d.status == "active")
    entries: list[JsonValue] = []
    problems: list[Problem] = []
    for defn in sorted(defs, key=lambda d: d.name):
        check = _Compile(defn, model, active, function_names)
        entry = check.run()
        problems.extend(check.problems)
        if entry is not None and not check.problems:
            entries.append(entry)
    return {"schema": CATALOG_SCHEMA, "actions": entries}, problems


def _check_stored(
    defn: ActionDef, entry: JsonObject, model: CompiledModel, function_names: frozenset[str]
) -> Problem | None:
    """The stored normalized ops must still compile (every CURIE in them still resolves)."""
    ops = tuple(as_object(o, "ops") for o in req_list(entry, "ops"))
    check = _Compile(replace(defn, ops=ops), model, frozenset(), function_names, stored_form=True)
    check.run()
    if check.problems:
        first = check.problems[0]
        return _stale_problem(
            defn.name, f"the stored form no longer compiles: {first.message}", "ops", first.code
        )
    return None


def _stale_problem(
    name: str, message: str, path: str | None = None, error: str | None = None
) -> Problem:
    details: dict[str, JsonValue] = {} if error is None else {"error": error}
    return Problem(name, "action.stale", message, path, details=details, kind="action")


@dataclass(frozen=True, slots=True)
class _End:
    """The entity an op names: the classes it can have and the class it is declared as."""

    classes: frozenset[str]
    subject: str  # the class the write is authorized as
    label: str


@dataclass(frozen=True, slots=True)
class _Slot:
    """An attribute or qualifier a value is written to."""

    kind: str  # base | datatype | enum | lifecycle
    type: str
    many: bool
    label: str
    lifecycle: bool = False


class _Compile:
    """The checks of one action; ``run`` returns the entry, problems collect in ``problems``."""

    def __init__(
        self,
        defn: ActionDef,
        model: CompiledModel,
        active: frozenset[str],
        function_names: frozenset[str],
        *,
        stored_form: bool = False,
    ) -> None:
        self.defn = defn
        self.model = model
        self.active = active
        self.function_names = function_names
        self.stored_form = stored_form  # compiling the normalized form: the checks of 3 to 5
        self.problems: list[Problem] = []
        self.declared = {p.name: p for p in defn.params}
        self.used: set[str] = set()
        self.reported: set[str] = set()  # undeclared parameters already reported
        self.locals: dict[str, str] = {}  # local name -> create type (CURIE)
        self.types: set[str] = set()
        self.relations: set[str] = set()
        self.needs: set[tuple[str, str]] = set()
        self.uses: dict[tuple[str | int, ...], Use] = {}
        self.misplaced: set[tuple[str | int, ...]] = set()

    def add(self, code: str, path: str | None, message: str, **details: JsonValue) -> None:
        self.problems.append(Problem(self.defn.name, code, message, path, None, details, "action"))

    def run(self) -> dict[str, JsonValue] | None:
        module = None
        if not self.stored_form:
            module = self.module()
            self.name()
            self.replacement()
        self.parameters()
        self.limits()
        self.prepare_locals()
        ops = [self.op(index, op) for index, op in enumerate(self.defn.ops)]
        preconditions = self.preconditions()
        self.unused()
        if not self.problems:
            self.assemble(ops)
        if self.problems:
            return None
        return self.entry(module, ops, preconditions)

    # Declaration

    def module(self) -> str | None:
        return resolve_module(self.model, self.defn.module, "action", self.add)

    def name(self) -> None:
        if self.defn.name in self.function_names:
            self.add(
                "action.name_taken",
                "name",
                f"{self.defn.name!r} is the name of a function of the graph; functions and "
                "actions share one namespace",
                name=self.defn.name,
            )

    def replacement(self) -> None:
        target = self.defn.replaced_by
        if target is not None and target not in self.active:
            self.add(
                "action.replaced_by_invalid",
                "replaced_by",
                f"{target!r} is not an active action of the graph",
                replaced_by=target,
            )

    def limits(self) -> None:
        if len(self.defn.ops) > MAX_OPS:
            self.add(
                "action.too_many_ops",
                "ops",
                f"{len(self.defn.ops)} ops; at most {MAX_OPS} per action",
                count=len(self.defn.ops),
                limit=MAX_OPS,
            )
        if len(self.defn.preconditions) > MAX_PRECONDITIONS:
            self.add(
                "action.too_many_preconditions",
                "preconditions",
                f"{len(self.defn.preconditions)} preconditions; at most {MAX_PRECONDITIONS}",
                count=len(self.defn.preconditions),
                limit=MAX_PRECONDITIONS,
            )

    # Parameters

    def parameters(self) -> None:
        for param in self.defn.params:
            path = f"params.{param.name}"
            usable = True
            if param.type == "ref" and param.of_class not in self.model.classes:
                self.add(
                    "action.unknown_class",
                    f"{path}.class",
                    f"the model has no class {param.of_class!r}",
                    curie=param.of_class,
                )
                usable = False
            if param.type == "enum" and param.enum not in self.model.enums:
                self.add(
                    "action.unknown_enum",
                    f"{path}.enum",
                    f"the model has no enum {param.enum!r}",
                    curie=param.enum,
                )
                usable = False
            if param.default is None or not usable:
                continue
            if param.type == "ref":
                self.add(
                    "action.invalid_default",
                    f"{path}.default",
                    "a ref has no default; entity ids go stale",
                )
                continue
            try:
                bind_json(
                    "action", self.defn.name, [param], {param.name: param.default}, self.model
                )
            except GraphCliError as exc:
                self.add(
                    "action.invalid_default", f"{path}.default", exc.message, **dict(exc.details)
                )

    def unused(self) -> None:
        for name in self.declared:
            if name not in self.used:
                self.add(
                    "action.parameter_unused",
                    f"params.{name}",
                    f"parameter {name!r} is declared but no op or precondition uses it",
                    parameter=name,
                )

    def note_use(self, use: Use) -> None:
        """Count the use; the first use of an undeclared parameter is reported. Any string
        that starts with ``$`` is a placeholder, so a text such as ``$5`` ends here too."""
        self.used.add(use.name)
        if use.name not in self.declared and use.name not in self.reported:
            self.reported.add(use.name)
            grammar = (
                ""
                if PLACEHOLDER_RE.match(f"${use.name}")
                else f" (a placeholder is $ and a name like {PARAM_RE.pattern}; there is no "
                "escape for a literal $ text)"
            )
            self.add(
                "action.parameter_undeclared",
                path_text(use.path),
                f"parameter ${use.name} is not declared under params{grammar}",
                parameter=use.name,
            )

    def declared_param(self, use: Use) -> ParamDef | None:
        """The parameter a placeholder names, unless it is undeclared or optional where its
        entry cannot be dropped (reported once per position)."""
        param = self.declared.get(use.name)
        if param is None:
            return None
        if param.optional and not use.droppable:
            if use.path not in self.misplaced:
                self.misplaced.add(use.path)
                self.add(
                    "action.optional_position",
                    path_text(use.path),
                    f"parameter {param.name!r} is optional but this position cannot be left "
                    "out; an optional parameter may only stand for an attribute, qualifier, "
                    "validity or list value, whose entry is dropped when it is not given",
                    parameter=param.name,
                )
            return None
        return param

    # Terms

    def term(self, kind: str, name: str, known: Mapping[str, object], path: str) -> str | None:
        try:
            return resolve_term(kind, name, known)
        except GraphCliError as exc:
            code = (
                f"action.unknown_{kind}" if exc.code == "term.unknown" else "action.ambiguous_term"
            )
            self.add(code, path, exc.message, **dict(exc.details))
            return None

    def unknown(self, code: str, path: str, what: str, name: str, known: Sequence[str]) -> None:
        import difflib

        self.add(
            code,
            path,
            f"{what} has no {code.removeprefix('action.unknown_')} {name!r}",
            name=name,
            expected=sorted(known),
            did_you_mean=difflib.get_close_matches(name, sorted(known), n=5),
        )

    # Locals

    def prepare_locals(self) -> None:
        """The type each ``@local`` of a create stands for (errors are reported at the op)."""
        seen: set[str] = set()
        for index, op in enumerate(self.defn.ops):
            local = op.get("local")
            if op.get("op") != "create" or not isinstance(local, str):
                continue
            if local in seen:
                self.add(
                    "action.local_duplicate",
                    f"ops[{index}].local",
                    f"@{local} is bound twice",
                    name=local,
                )
                continue
            seen.add(local)
            kind = op.get("type")
            if isinstance(kind, str):
                with contextlib.suppress(GraphCliError):
                    self.locals[local] = resolve_term("type", kind, self.model.classes)

    # Ops

    def op(self, index: int, op: JsonObject) -> dict[str, JsonValue]:
        out = dict(op)
        scan = scan_op(op, index)
        for use in scan.uses:
            self.uses[use.path] = use
            self.note_use(use)
        self.check_locals(scan, index)
        kind = op["op"]
        if kind == "create":
            self.create(index, op, out)
        elif kind == "update":
            self.update(index, op)
        elif kind == "link":
            self.link(index, op, out)
        elif kind == "unlink":
            self.unlink(index, op, out)
        elif kind == "transition":
            self.transition(index, op)
        elif kind == "supersede":
            self.supersede(index, op)
        elif kind == "retract":
            self.retract(index, op)
        elif kind == "set_alias":
            self.set_alias(index, op)
        return out

    def check_locals(self, scan: OpScan, index: int) -> None:
        for path, name in scan.locals_used:
            if name not in self.locals:
                self.add(
                    "action.local_unresolved",
                    path_text(path),
                    f"@{name} is not created by an op of this action",
                    name=name,
                )

    def need(self, op_kind: str, subject: str) -> None:
        self.needs.add((OP_ACTIONS[op_kind], subject))
        self.types.add(subject)

    # Entity references

    def end(
        self, index: int, op: Mapping[str, JsonValue], key: str, role: str = "entity"
    ) -> _End | None:
        value = op.get(key)
        if isinstance(value, str):
            return self.end_of(value, ("ops", index, key), role)
        return None

    def end_of(self, text: str, path: tuple[str | int, ...], role: str = "entity") -> _End | None:
        if text.startswith("@"):
            found = self.locals.get(text[1:])
            return None if found is None else _End(frozenset({found}), found, f"@{text[1:]}")
        use = self.uses.get(path)
        if use is None:
            return None
        param = self.declared_param(use)
        if param is None:
            return None
        if param.type != "ref" or param.many != (role == "entities"):
            wanted = "a list of references" if role == "entities" else "one reference"
            self.add(
                "action.type_mismatch",
                path_text(path),
                f"parameter {param.name!r} is {param.type_text}; this position takes {wanted}",
                parameter=param.name,
            )
            return None
        if param.of_class not in self.model.classes:
            return None  # reported at the parameter
        assert param.of_class is not None
        self.types.add(param.of_class)
        return _End(
            frozenset(self.model.subtypes(param.of_class)),
            param.of_class,
            f"${param.name} ({param.of_class})",
        )

    def ends_of(self, index: int, op: Mapping[str, JsonValue], key: str) -> list[_End]:
        value = op.get(key)
        path = ("ops", index, key)
        if isinstance(value, str):  # a many parameter
            found = self.end_of(value, path, "entities")
            return [] if found is None else [found]
        result: list[_End] = []
        for i, item in enumerate(value if isinstance(value, list) else []):
            if isinstance(item, str):
                found = self.end_of(item, (*path, i))
                if found is not None:
                    result.append(found)
        return result

    # Attributes and qualifiers

    def attribute_slots(self, cls: str) -> dict[str, _Slot]:
        return {
            name: _Slot(
                a.kind, a.type, a.many, f"attribute {name!r} of {cls}", a.kind == "lifecycle"
            )
            for name, a in self.model.classes[cls].attributes.items()
        }

    def value_map(
        self,
        index: int,
        container: Mapping[str, JsonValue],
        key: str,
        slots: Mapping[str, _Slot],
        owner: str,
        unknown_code: str,
        *,
        base: tuple[str | int, ...] | None = None,
        direct: bool = False,
    ) -> None:
        """Names and placeholders of ``attrs``, ``set``, ``qualifiers`` (``key``)."""
        values = container.get(key)
        if not isinstance(values, Mapping):
            return
        path = (*(base if base is not None else ("ops", index)), key)
        for name in values:
            self.slot(path, name, slots, owner, unknown_code, direct=direct)

    def slot(
        self,
        path: tuple[str | int, ...],
        name: str,
        slots: Mapping[str, _Slot],
        owner: str,
        unknown_code: str,
        *,
        direct: bool,
        fit: bool = True,
    ) -> _Slot | None:
        """Check that ``name`` exists and may be written; check the placeholder on it."""
        where = (*path, name)
        found = slots.get(name)
        if found is None:
            self.unknown(unknown_code, path_text(where), owner, name, list(slots))
            return None
        if direct and found.lifecycle:
            self.add(
                "action.lifecycle_direct_write",
                path_text(where),
                f"{found.label} changes only through a transition op (the write validator "
                "refuses a direct update)",
                name=name,
            )
            return None
        use = self.uses.get(where)
        if use is not None and fit:
            param = self.declared_param(use)
            if param is not None:
                self.fit(use, param, found)
        return found

    def list_map(
        self,
        index: int,
        op: Mapping[str, JsonValue],
        slots: Mapping[str, _Slot],
        owner: str,
    ) -> None:
        for key in _LIST_KEYS:
            lists = op.get(key)
            if not isinstance(lists, Mapping):
                continue
            path = ("ops", index, key)
            for name, items in lists.items():
                found = self.slot(
                    path, name, slots, owner, "action.unknown_attribute", direct=True, fit=False
                )
                if found is None:
                    continue
                if not found.many:
                    self.add(
                        "action.type_mismatch",
                        path_text((*path, name)),
                        f"{found.label} holds one value; add and remove need a list attribute "
                        "(many: true)",
                        name=name,
                    )
                    continue
                for where in [(*path, name)] + [
                    (*path, name, i) for i in range(len(items) if isinstance(items, list) else 0)
                ]:
                    use = self.uses.get(where)
                    param = None if use is None else self.declared_param(use)
                    if use is not None and param is not None:
                        self.fit(use, param, found)

    def fit(self, use: Use, param: ParamDef, slot: _Slot) -> None:
        """The parameter's type and list-ness against the attribute or qualifier."""
        if param.type == "enum" and param.enum not in self.model.enums:
            return  # reported at the parameter
        reason = self.misfit(use, param, slot)
        if reason is not None:
            self.add(
                "action.type_mismatch",
                path_text(use.path),
                f"parameter {param.name!r} ({param.type_text}) cannot stand for {slot.label}: "
                f"{reason}",
                parameter=param.name,
            )

    def misfit(self, use: Use, param: ParamDef, slot: _Slot) -> str | None:
        if param.type == "ref":
            return "a ref names an entity; it is no value"
        if use.role == "items":
            if not slot.many:
                return "the attribute holds one value, not a list"
            if not param.many:
                return "the whole list needs a list parameter"
        elif use.role == "item":
            if param.many:
                return "one element of the list needs a single value, not a list parameter"
        else:
            if use.text and (slot.many or param.many):
                return "the text forms take one value at a time; use add_text for a list"
            if slot.many != param.many:
                return (
                    "a list parameter needs a list attribute"
                    if param.many
                    else "a list attribute needs a list parameter"
                )
        if slot.kind == "enum":
            if param.type != "enum" or param.enum != slot.type:
                return f"the value is an enum value of {slot.type}; use an enum parameter of it"
            return None
        base = self.model.value_base(slot.kind, slot.type)  # type: ignore[arg-type]
        if FITS[param.type] & {base}:
            return None
        if use.text and param.type == "string" and base in LANG_BASES:
            return None
        return f"a {param.type} does not fit {base}"

    def time(self, index: int, op: Mapping[str, JsonValue]) -> None:
        for key in ("valid_from", "valid_to"):
            use = self.uses.get(("ops", index, key))
            param = None if use is None else self.declared_param(use)
            if (
                use is not None
                and param is not None
                and (param.type not in ("date", "datetime") or param.many)
            ):
                self.add(
                    "action.type_mismatch",
                    path_text(use.path),
                    f"parameter {param.name!r} ({param.type_text}) cannot stand for {key}; "
                    "it takes one date or datetime",
                    parameter=param.name,
                )

    # Relations

    def relation(self, path: str, name: object) -> str | None:
        """The CURIE of the relation an op names (recorded in ``touches``)."""
        if not isinstance(name, str):
            return None
        curie = self.term("relation", name, self.model.relations, path)
        if curie is not None:
            self.relations.add(curie)
        return curie

    def edge(
        self,
        path: str,
        rel: str,
        source: _End | None,
        target: _End | None,
    ) -> None:
        """Domain and range of ``rel`` against the classes the two ends can have."""
        definition = self.model.relations[rel]
        ok_source = source is None or _accepts(self.model, definition.domain, source.classes)
        ok_target = target is None or _accepts(self.model, definition.range, target.classes)
        if ok_source and ok_target:
            return
        details: dict[str, JsonValue] = {"relation": rel}
        names = []
        if source is not None:
            details["source"] = source.subject
            names.append(source.label)
        if target is not None:
            details["target"] = target.subject
            names.append(target.label)
        if source is not None and target is not None:
            details["connecting"] = connecting(
                self.model,
                _general(self.model, source.classes),
                _general(self.model, target.classes),
            )
        which = "domain" if not ok_source else "range"
        self.add(
            "action.op_impossible",
            path,
            f"{rel} cannot connect {' to '.join(names)} (its {which} does not accept it); "
            "the write would always be refused",
            **details,
        )

    def qualifier_slots(self, rel: str) -> dict[str, _Slot]:
        return {
            name: _Slot(q.kind, q.type, q.many, f"qualifier {name!r} of {rel}")
            for name, q in self.model.relations[rel].qualifiers.items()
        }

    # Op kinds

    def create(self, index: int, op: JsonObject, out: dict[str, JsonValue]) -> None:
        where = f"ops[{index}]"
        curie = self.term("type", str(op["type"]), self.model.classes, f"{where}.type")
        if curie is None:
            return
        out["type"] = curie
        self.types.add(curie)
        self.needs.add((OP_ACTIONS["create"], curie))
        if self.model.classes[curie].abstract:
            self.add(
                "action.abstract_type",
                f"{where}.type",
                f"{curie} is abstract; a create names a concrete class",
                curie=curie,
                expected=[
                    c for c in self.model.subtypes(curie) if not self.model.classes[c].abstract
                ],
            )
            return
        slots = self.attribute_slots(curie)
        for key in ("attrs", "attrs_text"):
            self.value_map(index, op, key, slots, curie, "action.unknown_attribute")
        self.time(index, op)
        source = _End(frozenset({curie}), curie, curie)
        edges = op.get("edges")
        normalized = []
        for i, edge in enumerate(edges if isinstance(edges, list) else []):
            if not isinstance(edge, Mapping):
                continue
            base = ("ops", index, "edges", i)
            rel_path = path_text((*base, "rel"))
            found = self.relation(rel_path, edge.get("rel"))
            copy = dict(edge)
            target = (
                self.end_of(str(edge["target"]), (*base, "target")) if "target" in edge else None
            )
            if found is None:
                normalized.append(copy)
                continue
            copy["rel"] = found
            normalized.append(copy)
            self.edge(path_text(base), found, source, target)
            self.value_map(
                index,
                edge,
                "qualifiers",
                self.qualifier_slots(found),
                found,
                "action.unknown_qualifier",
                base=base,
            )
        if normalized:
            out["edges"] = normalized

    def update(self, index: int, op: JsonObject) -> None:
        end = self.end(index, op, "id")
        if end is None:
            return
        self.need("update", end.subject)
        slots = self.attribute_slots(end.subject)
        for key in ("set", "set_text"):
            self.value_map(
                index, op, key, slots, end.subject, "action.unknown_attribute", direct=True
            )
        unset = op.get("unset")
        for i, name in enumerate(unset if isinstance(unset, list) else []):
            self.unset_name(index, i, str(name), slots, end.subject)
        self.list_map(index, op, slots, end.subject)

    def unset_name(
        self, index: int, position: int, name: str, slots: Mapping[str, _Slot], owner: str
    ) -> None:
        path = path_text(("ops", index, "unset", position))
        found = slots.get(name)
        if found is None:
            self.unknown("action.unknown_attribute", path, owner, name, list(slots))
        elif found.lifecycle:
            self.add(
                "action.lifecycle_direct_write",
                path,
                f"{found.label} changes only through a transition op",
                name=name,
            )

    def link(self, index: int, op: JsonObject, out: dict[str, JsonValue]) -> None:
        where = f"ops[{index}]"
        source = self.end(index, op, "id")
        target = self.end(index, op, "target")
        if source is not None:
            self.need("link", source.subject)
        found = self.relation(f"{where}.rel", op.get("rel"))
        if found is None:
            return
        out["rel"] = found
        self.edge(where, found, source, target)
        self.value_map(
            index, op, "qualifiers", self.qualifier_slots(found), found, "action.unknown_qualifier"
        )

    def unlink(self, index: int, op: JsonObject, out: dict[str, JsonValue]) -> None:
        where = f"ops[{index}]"
        source = self.end(index, op, "id")
        found = self.relation(f"{where}.rel", op.get("rel"))
        if found is not None:
            out["rel"] = found
        if is_dynamic_unlink(op):
            direction = str(op.get("direction", "out"))
            out["direction"] = direction
            subject = op.get("id")
            if isinstance(subject, str) and subject.startswith("@"):
                self.add(
                    "action.invalid_op",
                    f"{where}.id",
                    "all: true reads the edges of an existing entity; it takes a reference "
                    "parameter, not an @local",
                )
                return
            if source is None or found is None:
                return
            definition = self.model.relations[found]
            accepted = definition.domain if direction == "in" else definition.range
            end_side = definition.range if direction == "in" else definition.domain
            if not _accepts(self.model, end_side, source.classes):
                self.add(
                    "action.op_impossible",
                    where,
                    f"{found} never has {source.label} as its "
                    f"{'target' if direction == 'in' else 'source'}; the unlink would find nothing",
                    relation=found,
                    direction=direction,
                    entity=source.subject,
                    expected=list(end_side),
                )
                return
            if direction == "in":
                for cls in accepted:  # the edges are removed at their sources
                    self.need("unlink", cls)
            else:
                self.need("unlink", source.subject)
            return
        target = self.end(index, op, "target")
        if source is not None:
            self.need("unlink", source.subject)
        if found is not None:
            self.edge(where, found, source, target)

    def transition(self, index: int, op: JsonObject) -> None:
        end = self.end(index, op, "id")
        if end is None:
            return
        self.need("transition", end.subject)
        where = f"ops[{index}]"
        attributes = self.model.classes[end.subject].attributes
        lifecycles = {n: a for n, a in attributes.items() if a.kind == "lifecycle"}
        attr = op.get("attr")
        chosen: AttributeDef | None
        if isinstance(attr, str):
            chosen = lifecycles.get(attr)
            if chosen is None:
                code = (
                    "action.lifecycle_missing" if attr in attributes else "action.unknown_attribute"
                )
                self.add(
                    code,
                    f"{where}.attr",
                    f"{attr!r} is not a lifecycle attribute of {end.subject}",
                    name=attr,
                    expected=sorted(lifecycles),
                )
                return
        elif len(lifecycles) == 1:
            chosen = next(iter(lifecycles.values()))
        else:
            self.add(
                "action.lifecycle_missing",
                where,
                f"{end.subject} has no lifecycle attribute"
                if not lifecycles
                else f"{end.subject} has several lifecycle attributes; name one with attr",
                expected=sorted(lifecycles),
            )
            return
        states = self.model.lifecycles[chosen.type].state_names()
        if op.get("to") not in states:
            self.add(
                "action.unknown_state",
                f"{where}.to",
                f"{op.get('to')!r} is not a state of {chosen.type}",
                name=str(op.get("to")),
                expected=list(states),
            )

    def supersede(self, index: int, op: JsonObject) -> None:
        by = self.end(index, op, "by")
        if by is not None:
            self.need("supersede", by.subject)
        for end in self.ends_of(index, op, "targets"):
            self.need("supersede", end.subject)

    def retract(self, index: int, op: JsonObject) -> None:
        end = self.end(index, op, "id")
        if end is not None:
            self.need("retract", end.subject)
        by = self.end(index, op, "by")
        if by is not None:
            self.need("retract", by.subject)

    def set_alias(self, index: int, op: JsonObject) -> None:
        end = self.end(index, op, "id")
        if end is not None:
            self.need("set_alias", end.subject)
        use = self.uses.get(("ops", index, "alias"))
        param = None if use is None else self.declared_param(use)
        if use is not None and param is not None and (param.type != "string" or param.many):
            self.add(
                "action.type_mismatch",
                path_text(use.path),
                f"parameter {param.name!r} ({param.type_text}) cannot stand for an alias; "
                "it takes one string",
                parameter=param.name,
            )

    # Template

    def assemble(self, ops: Sequence[Mapping[str, JsonValue]]) -> None:
        """The ops with typed placeholder values must form a document ``parse_changeset``
        accepts; also with every optional parameter absent."""
        self.assembled(ops, absent=False)
        if any(p.optional for p in self.defn.params):
            self.assembled(ops, absent=True)

    def assembled(self, ops: Sequence[Mapping[str, JsonValue]], *, absent: bool) -> None:
        def value_of(use: Use) -> JsonValue | Absent:
            param = self.declared[use.name]
            if absent and param.optional:
                return ABSENT
            return self.sample(param, use)

        built: list[JsonValue] = []
        for index, op in enumerate(ops):
            scan = scan_op(op, index)
            instance = substitute(op, scan.uses, value_of)
            if is_dynamic_unlink(instance):
                instance = {"op": "unlink", "id": instance["id"], "edge_id": _FAKE_EDGE}
            built.append(instance)
        try:
            parse_changeset({"ops": built})
        except GraphCliError as exc:
            path = exc.details.get("path")
            prefix = "with every optional parameter absent: " if absent else ""
            self.add(
                "action.template_invalid",
                path if isinstance(path, str) else None,
                f"{prefix}{exc.message}",
                error=exc.code,
            )

    def sample(self, param: ParamDef, use: Use) -> JsonValue:
        if param.type == "ref":
            return [_FAKE_ENTITY] if param.many else _FAKE_ENTITY
        value = placeholder(param, self.model)
        if use.text:
            return [text_value(v) for v in value] if isinstance(value, list) else text_value(value)
        return json_value(value)

    # Preconditions

    def preconditions(self) -> list[JsonValue]:
        result: list[JsonValue] = []
        for index, pre in enumerate(self.defn.preconditions):
            before = len(self.problems)
            expr = self.precondition(index, pre)
            if len(self.problems) == before and expr is not None:
                result.append({"id": pre.id, "on": pre.on, "summary": pre.summary, "expr": expr})
        return result

    def precondition(self, index: int, pre: PreconditionDef) -> JsonValue | None:
        path = f"preconditions[{index}]"
        param = self.declared.get(pre.on)
        self.used.add(pre.on)
        if param is None:
            self.add(
                "action.precondition_on_invalid",
                f"{path}.on",
                f"{pre.on!r} is not a declared parameter",
                parameter=pre.on,
                expected=[p.name for p in self.defn.params if p.type == "ref"],
            )
            return None
        if param.type != "ref" or param.many or param.optional:
            self.add(
                "action.precondition_on_invalid",
                f"{path}.on",
                f"{pre.on!r} is {'an optional ' if param.optional else ''}{param.type_text}; a "
                "precondition is checked on one entity, a required ref parameter",
                parameter=pre.on,
            )
            return None
        if param.of_class not in self.model.classes:
            return None  # reported at the parameter
        assert param.of_class is not None
        self.types.add(param.of_class)
        self.touch_conditions(pre.conds)
        before = len(self.problems)
        resolved = _ConditionCheck(self, param.of_class, pre.conds).run()
        if len(self.problems) > before:
            return None
        return resolved

    def touch_conditions(self, conds: tuple[Cond, ...]) -> None:
        for cond in iter_conditions(conds):
            if isinstance(cond, CountCond | SumCond):
                with contextlib.suppress(GraphCliError):
                    self.relations.add(resolve_term("relation", cond.rel, self.model.relations))
                for name in cond.types:
                    with contextlib.suppress(GraphCliError):
                        self.types.add(resolve_term("type", name, self.model.classes))

    # Entry

    def entry(
        self, module: str | None, ops: list[dict[str, JsonValue]], preconditions: list[JsonValue]
    ) -> dict[str, JsonValue]:
        entry: dict[str, JsonValue] = {
            **self.defn.to_json(),
            "module": module,
            "source": self.defn.source_json(),
            "ops": [dict(o) for o in ops],
            "preconditions": preconditions,
            "touches": self.touches(),
            "needs": [{"action": action, "type": curie} for action, curie in sorted(self.needs)],
        }
        entry["hash"] = hash_document(entry)
        return entry

    def touches(self) -> dict[str, JsonValue]:
        modules = {m.prefix: m.id for m in self.model.modules}

        def named(curies: set[str]) -> list[JsonValue]:
            return [
                {"curie": c, "module": modules.get(c.partition(":")[0])} for c in sorted(curies)
            ]

        return {"types": named(self.types), "relations": named(self.relations)}


class _ConditionCheck:
    """The checks of rule conditions in the ontology validator, on a ``CompiledModel``."""

    def __init__(self, parent: _Compile, scope: str, conds: tuple[Cond, ...]) -> None:
        self.parent = parent
        self.model = parent.model
        self.scope = scope
        self.conds = conds
        self.rels: dict[str, str] = {}
        self.classes: dict[str, str] = {}

    def run(self) -> JsonValue | None:
        for cond in iter_conditions(self.conds):
            compares_attr = any(isinstance(value, AttrRef) for _, value in cond.ops)
            if isinstance(cond, AttrCond):
                self.scope_attribute(f"{cond.path}.attr", cond.attr, compares_attr)
            else:
                self.aggregate(cond)
            for op, value in cond.ops:
                if isinstance(value, AttrRef):
                    self.scope_attribute(f"{cond.path}.{op}.attr", value.attr, True)
        return lower(self.conds, lambda r: self.rels.get(r, r), lambda c: self.classes.get(c, c))

    def scope_attribute(self, path: str, name: str, numeric: bool) -> None:
        found = self.model.classes[self.scope].attributes.get(name)
        if found is None:
            self.parent.unknown(
                "action.unknown_attribute",
                path,
                self.scope,
                name,
                list(self.model.classes[self.scope].attributes),
            )
        elif numeric and not _numeric(self.model, found):
            self.parent.add(
                "action.not_numeric",
                path,
                f"{name!r} of {self.scope} is not a single integer or decimal value",
                name=name,
            )

    def aggregate(self, cond: CountCond | SumCond) -> None:
        kind = "count" if isinstance(cond, CountCond) else "sum"
        rel = self.parent.term(
            "relation", cond.rel, self.model.relations, f"{cond.path}.{kind}.rel"
        )
        if rel is not None:
            self.rels[cond.rel] = rel
        types: set[str] = set()
        complete = True
        for ref in cond.types:
            curie = self.parent.term(
                "type", ref, self.model.classes, f"{cond.path}.{kind}.where.type"
            )
            if curie is None:
                complete = False
            else:
                self.classes[ref] = curie
                types.add(curie)
        if isinstance(cond, SumCond) and rel is not None and complete:
            self.summed(cond, rel, types)

    def summed(self, cond: SumCond, rel: str, types: set[str]) -> None:
        definition = self.model.relations[rel]
        bases = set(definition.range if cond.direction == "out" else definition.domain)
        ends = []
        for curie, cls in self.model.classes.items():
            lineage = {curie, *cls.ancestors}
            if cls.abstract or (bases and lineage.isdisjoint(bases)):
                continue
            if not types or not lineage.isdisjoint(types):
                ends.append(curie)
        path = f"{cond.path}.sum.attr"
        missing, other = [], []
        for curie in ends:
            attribute = self.model.classes[curie].attributes.get(cond.attr)
            if attribute is None:
                missing.append(curie)
            elif not _numeric(self.model, attribute):
                other.append(curie)
        if missing:
            self.parent.add(
                "action.unknown_attribute",
                path,
                f"{', '.join(missing)} at the other end of {rel} "
                f"{'has' if len(missing) == 1 else 'have'} no attribute {cond.attr!r}; "
                "narrow with where.type",
                name=cond.attr,
            )
        if other:
            self.parent.add(
                "action.not_numeric",
                path,
                f"{cond.attr!r} of {other[0]} is not a single integer or decimal value",
                name=cond.attr,
            )


def _numeric(model: CompiledModel, attribute: AttributeDef) -> bool:
    if attribute.kind in ("lifecycle", "enum") or attribute.many:
        return False
    return model.value_base(attribute.kind, attribute.type) in ("integer", "decimal")


def _accepts(model: CompiledModel, accepted: tuple[str, ...], classes: frozenset[str]) -> bool:
    return any(model.is_a_any(c, accepted) for c in classes)


def _general(model: CompiledModel, classes: frozenset[str]) -> str:
    """The most general of the classes: the one the hint should start from."""
    return min(classes, key=lambda c: (len(model.classes[c].ancestors), len(c), c))
