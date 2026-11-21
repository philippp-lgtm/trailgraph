"""The description of a generated graph skill: the sentence that sends an agent to ``graph start``.

An agent chooses a skill by its description, so the one place every graph skill is sure to be read
before it is loaded is the description. The renderer appends ``START_SENTENCE`` to a curated
description that does not mention ``graph start`` yet, unless the result would be longer than the
1,024 characters the Agent Skills format allows: then the description stays as the maintainer wrote
it (a render is never refused for this) and ``graph-admin`` warns at render and publish
(``Size.description_over``) so the maintainer shortens it or writes the sentence in.
"""

from __future__ import annotations

import json
from typing import Final

from graph_cli.skills.tree import MAX_DESCRIPTION, frontmatter_keys

START_SENTENCE: Final = "Run `graph start` first and load graph-cli with it."
MENTION: Final = "graph start"


def frontmatter_bounds(lines: list[str]) -> tuple[int, int] | None:
    """``(first key line, closing ---)`` of the frontmatter of a ``SKILL.md``, else ``None``."""
    if not lines or lines[0] != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index] == "---":
            return 1, index
    return None


def description_of(lines: list[str]) -> str | None:
    bounds = frontmatter_bounds(lines)
    if bounds is None:
        return None
    return frontmatter_keys("\n".join(lines[bounds[0] : bounds[1]]) + "\n").get("description")


def lacks_start(description: str) -> bool:
    return MENTION not in description


def fits(description: str) -> bool:
    """Whether ``START_SENTENCE`` can be added without passing the limit."""
    return len(f"{description} {START_SENTENCE}") <= MAX_DESCRIPTION


def with_start_sentence(lines: list[str]) -> list[str]:
    """``lines`` with ``START_SENTENCE`` appended to the description when it lacks ``graph start``
    and the sentence fits; the lines unchanged otherwise (also for a plain or quoted description
    wrapped over several lines: rewriting it would need a YAML emitter, and the render is never
    refused for the sentence, so publish only warns when the description is over its limit)."""
    bounds = frontmatter_bounds(lines)
    description = description_of(lines)
    if bounds is None or description is None or not lacks_start(description):
        return lines
    if not description.strip() or not fits(description):
        return lines
    first, end = bounds
    index = next((i for i in range(first, end) if lines[i].startswith("description:")), None)
    if index is None:
        return lines
    value = lines[index].partition(":")[2].strip()
    # The last line that belongs to the value: a following line is part of it while it is
    # indented or empty; the empty lines at its end are not (they may precede the next key).
    last = index
    for number in range(index + 1, end):
        text = lines[number]
        if text and text[0] not in " \t":
            break
        if text.strip():
            last = number
    found = list(lines)
    if value[:1] in ("|", ">"):  # a block scalar: one more line at the indent of its body
        if last == index:
            return lines
        body = lines[next(n for n in range(index + 1, last + 1) if lines[n].strip())]
        indent = body[: len(body) - len(body.lstrip())]
        found.insert(last + 1, f"{indent}{START_SENTENCE}")
        return found
    if (
        last != index
    ):  # a plain or quoted scalar wrapped over lines: left as the maintainer wrote it
        return lines
    found[index] = (
        f"description: {json.dumps(f'{description} {START_SENTENCE}', ensure_ascii=False)}"
    )
    return found
