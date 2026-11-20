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

The op checks of check 3 (with the slot typing in ``action_slots``), the
template of check 4 and the conditions of check 5 are in ``action_ops``,
``action_template`` and ``action_conditions``; they report to the
``CheckContext`` of ``action_context``. The driver keeps the declaration,
parameter, precondition and entry checks.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Final

from graph_cli.kernel.canonical import JsonObject, JsonValue, hash_document
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.json_access import as_object, req_list, req_obj, req_str
from graph_cli.kinetic.action_conditions import ConditionCheck
from graph_cli.kinetic.action_context import CheckContext
from graph_cli.kinetic.action_format import (
    CATALOG_SCHEMA,
    PLACEHOLDER_RE,
    ActionDef,
    PreconditionDef,
    Use,
    load_action,
    path_text,
    scan_op,
)
from graph_cli.kinetic.action_ops import Footprint, OpChecks
from graph_cli.kinetic.action_slots import SlotChecks
from graph_cli.kinetic.action_template import TemplateCheck
from graph_cli.kinetic.compile import resolve_module
from graph_cli.kinetic.params import (
    PARAM_RE,
    Problem,
    bind_json,
)
from graph_cli.ontology_layer.expressions import (
    Cond,
    CountCond,
    SumCond,
    iter_conditions,
)
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.query.plan import resolve_term

MAX_ACTIONS: Final = 1000
MAX_OPS: Final = 50
MAX_PRECONDITIONS: Final = 10

_ENTITY_ROLES: Final = ("entity", "entities")


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
        self.ctx = CheckContext(defn, model)
        self.footprint = Footprint()
        self.ops = OpChecks(self.ctx, SlotChecks(self.ctx), self.footprint)
        self.template = TemplateCheck(self.ctx)
        self.used: set[str] = set()
        self.reported: set[str] = set()  # undeclared parameters already reported

    @property
    def problems(self) -> list[Problem]:
        return self.ctx.problems

    def run(self) -> dict[str, JsonValue] | None:
        module = None
        if not self.stored_form:
            module = self.module()
            self.name()
            self.replacement()
        self.parameters()
        self.limits()
        self.ops.prepare_locals()
        ops = [self.op(index, op) for index, op in enumerate(self.defn.ops)]
        preconditions = self.preconditions()
        self.unused()
        if not self.problems:
            self.template.assemble(ops)
        if self.problems:
            return None
        return self.entry(module, ops, preconditions)

    # Declaration

    def module(self) -> str | None:
        return resolve_module(self.model, self.defn.module, "action", self.ctx.add)

    def name(self) -> None:
        if self.defn.name in self.function_names:
            self.ctx.add(
                "action.name_taken",
                "name",
                f"{self.defn.name!r} is the name of a function of the graph; functions and "
                "actions share one namespace",
                name=self.defn.name,
            )

    def replacement(self) -> None:
        target = self.defn.replaced_by
        if target is not None and target not in self.active:
            self.ctx.add(
                "action.replaced_by_invalid",
                "replaced_by",
                f"{target!r} is not an active action of the graph",
                replaced_by=target,
            )

    def limits(self) -> None:
        if len(self.defn.ops) > MAX_OPS:
            self.ctx.add(
                "action.too_many_ops",
                "ops",
                f"{len(self.defn.ops)} ops; at most {MAX_OPS} per action",
                count=len(self.defn.ops),
                limit=MAX_OPS,
            )
        if len(self.defn.preconditions) > MAX_PRECONDITIONS:
            self.ctx.add(
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
                self.ctx.add(
                    "action.unknown_class",
                    f"{path}.class",
                    f"the model has no class {param.of_class!r}",
                    curie=param.of_class,
                )
                usable = False
            if param.type == "enum" and param.enum not in self.model.enums:
                self.ctx.add(
                    "action.unknown_enum",
                    f"{path}.enum",
                    f"the model has no enum {param.enum!r}",
                    curie=param.enum,
                )
                usable = False
            if param.default is None or not usable:
                continue
            if param.type == "ref":
                self.ctx.add(
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
                self.ctx.add(
                    "action.invalid_default", f"{path}.default", exc.message, **dict(exc.details)
                )

    def unused(self) -> None:
        for name in self.ctx.declared:
            if name not in self.used:
                self.ctx.add(
                    "action.parameter_unused",
                    f"params.{name}",
                    f"parameter {name!r} is declared but no op or precondition uses it",
                    parameter=name,
                )

    def note_use(self, use: Use) -> None:
        """Count the use; the first use of an undeclared parameter is reported. Any string
        that starts with ``$`` is a placeholder, so a text such as ``$5`` ends here too."""
        self.used.add(use.name)
        if use.name not in self.ctx.declared and use.name not in self.reported:
            self.reported.add(use.name)
            grammar = (
                ""
                if PLACEHOLDER_RE.match(f"${use.name}")
                else f" (a placeholder is $ and a name like {PARAM_RE.pattern}; there is no "
                "escape for a literal $ text)"
            )
            self.ctx.add(
                "action.parameter_undeclared",
                path_text(use.path),
                f"parameter ${use.name} is not declared under params{grammar}",
                parameter=use.name,
            )

    # Ops

    def op(self, index: int, op: JsonObject) -> dict[str, JsonValue]:
        out = dict(op)
        scan = scan_op(op, index)
        for use in scan.uses:
            self.ctx.uses[use.path] = use
            self.note_use(use)
        self.ops.check_locals(scan, index)
        self.ops.dispatch(index, op, out)
        return out

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
        param = self.ctx.declared.get(pre.on)
        self.used.add(pre.on)
        if param is None:
            self.ctx.add(
                "action.precondition_on_invalid",
                f"{path}.on",
                f"{pre.on!r} is not a declared parameter",
                parameter=pre.on,
                expected=[p.name for p in self.defn.params if p.type == "ref"],
            )
            return None
        if param.type != "ref" or param.many or param.optional:
            self.ctx.add(
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
        self.footprint.types.add(param.of_class)
        self.touch_conditions(pre.conds)
        before = len(self.problems)
        resolved = ConditionCheck(self.ctx, param.of_class, pre.conds).run()
        if len(self.problems) > before:
            return None
        return resolved

    def touch_conditions(self, conds: tuple[Cond, ...]) -> None:
        for cond in iter_conditions(conds):
            if isinstance(cond, CountCond | SumCond):
                with contextlib.suppress(GraphCliError):
                    self.footprint.relations.add(
                        resolve_term("relation", cond.rel, self.model.relations)
                    )
                for name in cond.types:
                    with contextlib.suppress(GraphCliError):
                        self.footprint.types.add(resolve_term("type", name, self.model.classes))

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
            "needs": [
                {"action": action, "type": curie} for action, curie in sorted(self.footprint.needs)
            ],
        }
        entry["hash"] = hash_document(entry)
        return entry

    def touches(self) -> dict[str, JsonValue]:
        modules = {m.prefix: m.id for m in self.model.modules}

        def named(curies: set[str]) -> list[JsonValue]:
            return [
                {"curie": c, "module": modules.get(c.partition(":")[0])} for c in sorted(curies)
            ]

        return {"types": named(self.footprint.types), "relations": named(self.footprint.relations)}
