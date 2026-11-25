"""The error map of the tool manifest: what a harness does with each code a tool may return.

Every code carries its registry facts (``category``, ``retryable``, ``summary``) and ``on_error``,
the one move the harness makes next:

- ``retry``: the same call again, later (the registry marks the code retryable);
- ``fix_input``: the arguments are wrong or name something that does not fit; correct them (look
  a reference up again, drop a value) and call again;
- ``check_state``: the arguments are fine, the graph's current state decides (a precondition, an
  action with nothing to do); read the details, do not repeat the call unchanged;
- ``restart``: start again from the first page;
- ``refresh``: the harness's tool definitions are out of date; fetch the manifest again;
- ``stop``: nothing the harness can change helps; report it.

``on_error`` follows the category (``ON_ERROR_BY_CATEGORY``) unless the code is listed in
``OVERRIDES``. A ``refused`` code is either an input error (the reference names an entity of
the wrong class) or a state of the graph that stays (archived, read-only), and the category cannot
tell them apart: every ``refused`` code a tool lists has an explicit entry (checked by a test).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from graph_cli.kernel.canonical import JsonObject
from graph_cli.kernel.error_codes import ErrorCategory, lookup_error_code

RETRY: Final = "retry"
FIX_INPUT: Final = "fix_input"
CHECK_STATE: Final = "check_state"
RESTART: Final = "restart"
REFRESH: Final = "refresh"
STOP: Final = "stop"
ON_ERROR_VALUES: Final = (RETRY, FIX_INPUT, CHECK_STATE, RESTART, REFRESH, STOP)

ON_ERROR_BY_CATEGORY: Final[Mapping[ErrorCategory, str]] = {
    ErrorCategory.USAGE: FIX_INPUT,
    ErrorCategory.INVALID: FIX_INPUT,
    ErrorCategory.DENIED: STOP,
    ErrorCategory.NOT_FOUND: STOP,
    ErrorCategory.REFUSED: STOP,
    ErrorCategory.CONFLICT: STOP,
    ErrorCategory.UNAVAILABLE: STOP,
    ErrorCategory.INTERNAL: STOP,
}

OVERRIDES: Final[Mapping[str, str]] = {
    # the graph's state, not the input, decides
    "action.precondition_failed": CHECK_STATE,
    "action.nothing_to_do": CHECK_STATE,
    # the page cursor belongs to a graph state that is gone
    "view.cursor_stale": RESTART,
    # the tool definition the harness holds is not the current one
    "tool.hash_mismatch": REFRESH,
    "tool.not_found": REFRESH,  # the entry may have gone since the manifest was fetched
    # the stored entry no longer fits the ontology: only a maintainer can fix that
    "function.stale": STOP,
    "action.stale": STOP,
    "view.stale": STOP,
    "actions.slot_invalid": STOP,
    # refused, but caused by the arguments
    "function.parameter_type": FIX_INPUT,
    "action.parameter_type": FIX_INPUT,
    "view.parameter_type": FIX_INPUT,
    "view.class_mismatch": FIX_INPUT,
    "entity.ambiguous_ref": FIX_INPUT,
    "query.too_large": FIX_INPUT,
    # not found, but only because the argument names an entity that does not exist
    "entity.not_found": FIX_INPUT,
    # denied, but the call only lacks its reason
    "access.reason_required": FIX_INPUT,
    # refused by a state of the graph or the installation that stays
    "graph.not_in_project": STOP,
    "graph.archived": STOP,
    "graph.readonly": STOP,
    "changeset.too_large": STOP,
    "skills.outdated": STOP,
}


def on_error(code: str) -> str:
    """What a harness does about ``code`` (``ON_ERROR_VALUES``); a code the registry does not
    know is a bug of the caller (``KeyError``)."""
    if code in OVERRIDES:
        return OVERRIDES[code]
    spec = lookup_error_code(code)
    if spec is None:
        raise KeyError(code)
    return RETRY if spec.retryable else ON_ERROR_BY_CATEGORY[spec.category]


def error_info(code: str) -> JsonObject:
    """The entry of ``code`` in the ``errors`` map of the manifest."""
    spec = lookup_error_code(code)
    if spec is None:
        raise KeyError(code)
    return {
        "category": spec.category.value,
        "retryable": spec.retryable,
        "summary": spec.summary,
        "on_error": on_error(code),
    }
