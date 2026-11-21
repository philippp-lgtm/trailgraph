"""The leak check for hidden catalog entries (graph start plan, hard rule 1).

A function, action or view the caller cannot see must stay absent from everything built on the
caller's answers: names, counts, suggestions, summaries, ``missing`` lists. ``assert_hidden_absent``
serializes an output and fails when any hidden name occurs in it as a whole word; later steps
(lists, counts, search, ``start``, errors) call it on their own output with the names the same
reader's read view hides.
"""

import json
import re
from collections.abc import Iterable


def mentions(output: object, name: str) -> bool:
    """Whether ``name`` occurs in the JSON text of ``output`` as a whole name."""
    text = json.dumps(output, sort_keys=True, default=str)
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", text) is not None


def assert_hidden_absent(output: object, hidden: Iterable[str]) -> None:
    for name in sorted(hidden):
        assert not mentions(output, name), f"hidden name {name!r} leaks into {output!r}"
