"""The state and reporting primitives the checks of one action share.

``action_compile`` checks one action with several collaborators (the slot
typing of ``action_slots``, the conditions of ``action_conditions``); the
context holds what they and the driver read and write together: the
definition, the model, the placeholder uses of the ops and the problems found
so far, in one list so that their order is the order of the checks.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kinetic.action_format import ActionDef, Use, path_text
from graph_cli.kinetic.params import ParamDef, Problem
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.query.plan import resolve_term


class CheckContext:
    """The definition, the model, the uses of placeholders and the problems of one action."""

    def __init__(self, defn: ActionDef, model: CompiledModel) -> None:
        self.defn = defn
        self.model = model
        self.problems: list[Problem] = []
        self.declared = {p.name: p for p in defn.params}
        self.uses: dict[tuple[str | int, ...], Use] = {}
        self.misplaced: set[tuple[str | int, ...]] = set()

    def add(self, code: str, path: str | None, message: str, **details: JsonValue) -> None:
        self.problems.append(Problem(self.defn.name, code, message, path, None, details, "action"))

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
