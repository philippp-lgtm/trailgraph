"""Old names on input fail with their current name (migration plan M7).

A command that meets a term the pinned model does not know raises ``term.unknown``. When the
term is an old name of a renamed or retyped class, relation or attribute, the error says what
it is called now ("inv:Item was renamed to inv:Product in 2.0.0"), in the message and in
``details.renamed``; the old name is never accepted. The hint comes from the graph the command
selected (``AppServices.explained``), so it is added here, around every command, and reaches
the server's answers too (the server runs the same program). A write refused because a
finding names an unknown type, attribute or relation gets the same text as that finding's
``hint``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any, Final

from graph_cli.app_cli.commands.common import services
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec
from graph_cli.kernel.errors import GraphCliError

# ``term.unknown`` on input, and a write whose findings name an unknown type, attribute or
# relation (the finding's ``hint``).
EXPLAINED: Final = frozenset({"term.unknown", "write.invalid"})


def explained(specs: Sequence[CommandSpec]) -> tuple[CommandSpec, ...]:
    """The declarations with ``term.unknown`` explained by the selected graph's renames."""
    return tuple(replace(spec, handler=_explain(spec)) for spec in specs)


def _explain(spec: CommandSpec) -> Callable[..., Result]:
    handler = spec.handler

    def explain(state: CommandState, **values: Any) -> Result:
        try:
            return handler(state, **values)
        except GraphCliError as exc:
            if exc.code not in EXPLAINED or state.services is None:
                raise
            explained = services(state).explained(exc)
            if explained is exc:
                raise
            raise explained from exc

    return explain
