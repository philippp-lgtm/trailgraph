"""Building blocks shared by the identity providers (contracts B2.4, B2.6)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from graph_cli.kernel.actor import Assurance, Facet, is_valid_asserted_value


@dataclass(frozen=True, slots=True)
class IdentityInputs:
    """Everything identity needs from the process; read once by ``os.system_inputs``."""

    env: Mapping[str, str]
    uid: int
    user_name: str | None
    pid: int


@dataclass(frozen=True, slots=True)
class Supplied:
    """A provider's answer for one facet: the facet, or the variable that was malformed."""

    facet: Facet | None
    malformed: str | None = None


def asserted_facet(
    env: Mapping[str, str],
    *,
    id_var: str,
    source: str,
    type_var: str | None = None,
    parent_var: str | None = None,
) -> Supplied | None:
    """An asserted facet from env variables; ``None`` when ``id_var`` is unset or empty.

    The facet comes whole from one provider: a malformed id, type or parent
    makes the whole facet malformed (B2.4).
    """
    facet_id = env.get(id_var) or None
    if facet_id is None:
        return None
    values = {"id": (id_var, facet_id)}
    if type_var is not None and env.get(type_var):
        values["type"] = (type_var, env[type_var])
    if parent_var is not None and env.get(parent_var):
        values["parent"] = (parent_var, env[parent_var])
    for variable, value in values.values():
        if not is_valid_asserted_value(value):
            return Supplied(None, malformed=variable)
    return Supplied(
        Facet(
            Assurance.ASSERTED,
            id=facet_id,
            type=values["type"][1] if "type" in values else None,
            parent=values["parent"][1] if "parent" in values else None,
            source=source,
        )
    )
