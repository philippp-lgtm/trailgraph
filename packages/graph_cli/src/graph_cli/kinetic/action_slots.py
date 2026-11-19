"""The typing of the attributes and qualifiers an action op writes (actions plan A9, check 3).

``SlotChecks`` knows the slots of a class or relation (``attribute_slots``,
``qualifier_slots``), checks that the names an op writes exist and may be
written, and that a placeholder standing for a value fits the datatype, enum
and list-ness of its slot. Problems and the placeholder uses come from the
shared ``CheckContext``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kinetic.action_context import CheckContext
from graph_cli.kinetic.action_format import Use, path_text
from graph_cli.kinetic.compile import FITS
from graph_cli.kinetic.params import ParamDef
from graph_cli.ontology_layer.vocabulary import LANG_BASES

_LIST_KEYS: Final = ("add", "add_text", "remove", "remove_text")


@dataclass(frozen=True, slots=True)
class Slot:
    """An attribute or qualifier a value is written to."""

    kind: str  # base | datatype | enum | lifecycle
    type: str
    many: bool
    label: str
    lifecycle: bool = False


class SlotChecks:
    """The checks of the attributes and qualifiers one action writes."""

    def __init__(self, ctx: CheckContext) -> None:
        self.ctx = ctx

    def attribute_slots(self, cls: str) -> dict[str, Slot]:
        return {
            name: Slot(
                a.kind, a.type, a.many, f"attribute {name!r} of {cls}", a.kind == "lifecycle"
            )
            for name, a in self.ctx.model.classes[cls].attributes.items()
        }

    def value_map(
        self,
        index: int,
        container: Mapping[str, JsonValue],
        key: str,
        slots: Mapping[str, Slot],
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
        slots: Mapping[str, Slot],
        owner: str,
        unknown_code: str,
        *,
        direct: bool,
        fit: bool = True,
    ) -> Slot | None:
        """Check that ``name`` exists and may be written; check the placeholder on it."""
        where = (*path, name)
        found = slots.get(name)
        if found is None:
            self.ctx.unknown(unknown_code, path_text(where), owner, name, list(slots))
            return None
        if direct and found.lifecycle:
            self.ctx.add(
                "action.lifecycle_direct_write",
                path_text(where),
                f"{found.label} changes only through a transition op (the write validator "
                "refuses a direct update)",
                name=name,
            )
            return None
        use = self.ctx.uses.get(where)
        if use is not None and fit:
            param = self.ctx.declared_param(use)
            if param is not None:
                self.fit(use, param, found)
        return found

    def list_map(
        self,
        index: int,
        op: Mapping[str, JsonValue],
        slots: Mapping[str, Slot],
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
                    self.ctx.add(
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
                    use = self.ctx.uses.get(where)
                    param = None if use is None else self.ctx.declared_param(use)
                    if use is not None and param is not None:
                        self.fit(use, param, found)

    def fit(self, use: Use, param: ParamDef, slot: Slot) -> None:
        """The parameter's type and list-ness against the attribute or qualifier."""
        if param.type == "enum" and param.enum not in self.ctx.model.enums:
            return  # reported at the parameter
        reason = self.misfit(use, param, slot)
        if reason is not None:
            self.ctx.add(
                "action.type_mismatch",
                path_text(use.path),
                f"parameter {param.name!r} ({param.type_text}) cannot stand for {slot.label}: "
                f"{reason}",
                parameter=param.name,
            )

    def misfit(self, use: Use, param: ParamDef, slot: Slot) -> str | None:
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
        base = self.ctx.model.value_base(slot.kind, slot.type)  # type: ignore[arg-type]
        if FITS[param.type] & {base}:
            return None
        if use.text and param.type == "string" and base in LANG_BASES:
            return None
        return f"a {param.type} does not fit {base}"

    def qualifier_slots(self, rel: str) -> dict[str, Slot]:
        return {
            name: Slot(q.kind, q.type, q.many, f"qualifier {name!r} of {rel}")
            for name, q in self.ctx.model.relations[rel].qualifiers.items()
        }

    def unset_name(
        self, index: int, position: int, name: str, slots: Mapping[str, Slot], owner: str
    ) -> None:
        path = path_text(("ops", index, "unset", position))
        found = slots.get(name)
        if found is None:
            self.ctx.unknown("action.unknown_attribute", path, owner, name, list(slots))
        elif found.lifecycle:
            self.ctx.add(
                "action.lifecycle_direct_write",
                path,
                f"{found.label} changes only through a transition op",
                name=name,
            )
