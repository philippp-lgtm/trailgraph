"""Parameter values of a function call, coerced by the declared type.

The implementation is shared with actions in ``kinetic.params``; these are the
function-kind entry points (``function.parameter_missing|unknown|invalid`` with
the function name in ``details``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kinetic import params
from graph_cli.kinetic.params import ParamDef
from graph_cli.kinetic.params import placeholder as placeholder
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.query.cypher.values import Value


def bind_cli(
    function: str,
    params_: Sequence[ParamDef],
    pairs: Sequence[tuple[str, str]],
    model: CompiledModel,
) -> dict[str, Value]:
    """Values from ``name=value`` pairs; a ``many`` parameter repeats its name."""
    return params.bind_cli("function", function, params_, pairs, model)


def bind_json(
    function: str,
    params_: Sequence[ParamDef],
    values: Mapping[str, JsonValue],
    model: CompiledModel,
) -> dict[str, Value]:
    """Values from a JSON object; a ``many`` parameter takes a list."""
    return params.bind_json("function", function, params_, values, model)
