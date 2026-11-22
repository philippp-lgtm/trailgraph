"""The check that the template of an action assembles (actions plan A9, check 4).

``TemplateCheck`` substitutes typed sample values for the placeholders of the
ops and hands the result to ``parse_changeset``; a template it refuses is
``action.template_invalid``. Problems go to the shared ``CheckContext``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.changeset import parse_changeset
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kinetic.action_context import CheckContext
from graph_cli.kinetic.action_format import (
    ABSENT,
    Absent,
    Use,
    is_dynamic_unlink,
    scan_op,
    spread_edges,
    substitute,
)
from graph_cli.kinetic.bind import placeholder
from graph_cli.kinetic.params import ParamDef, json_value, text_value

# Placeholders for the entity ids and the edge id a template is assembled with.
_FAKE_ENTITY: Final = "00000000-0000-7000-8000-000000000001"
_FAKE_EDGE: Final = "00000000-0000-7000-8000-0000000000e1"


class TemplateCheck:
    """The assembly of the template of one action with typed sample values."""

    def __init__(self, ctx: CheckContext) -> None:
        self.ctx = ctx

    def assemble(self, ops: Sequence[Mapping[str, JsonValue]]) -> None:
        """The ops with typed placeholder values must form a document ``parse_changeset``
        accepts; also with every optional parameter absent."""
        self.assembled(ops, absent=False)
        if any(p.optional for p in self.ctx.defn.params):
            self.assembled(ops, absent=True)

    def assembled(self, ops: Sequence[Mapping[str, JsonValue]], *, absent: bool) -> None:
        def value_of(use: Use) -> JsonValue | Absent:
            param = self.ctx.declared[use.name]
            if absent and param.optional:
                return ABSENT
            return self.sample(param, use)

        built: list[JsonValue] = []
        for index, op in enumerate(ops):
            scan = scan_op(op, index)
            instance = spread_edges(substitute(op, scan.uses, value_of))
            if is_dynamic_unlink(instance):
                instance = {"op": "unlink", "id": instance["id"], "edge_id": _FAKE_EDGE}
            built.append(instance)
        try:
            parse_changeset({"ops": built})
        except GraphCliError as exc:
            path = exc.details.get("path")
            prefix = "with every optional parameter absent: " if absent else ""
            self.ctx.add(
                "action.template_invalid",
                path if isinstance(path, str) else None,
                f"{prefix}{exc.message}",
                error=exc.code,
            )

    def sample(self, param: ParamDef, use: Use) -> JsonValue:
        if param.type == "ref":
            return [_FAKE_ENTITY] if param.many else _FAKE_ENTITY
        value = placeholder(param, self.ctx.model)
        if use.text:
            return [text_value(v) for v in value] if isinstance(value, list) else text_value(value)
        return json_value(value)
