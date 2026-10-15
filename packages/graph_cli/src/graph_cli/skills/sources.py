"""Where installed skills come from (D15 Q2, Q6).

Skill 1 is rendered from this program's own inventory (the same document
``graph describe commands`` prints), never from another ``graph`` on
``PATH`` (F6). A graph skill comes from the graph's published slot and is
dispatched on the slot kind: ``static`` files are the skill as is. Item 3
adds the kind ``generated`` here; the commands do not change.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ports.catalog import GraphEntry, SkillSlot
from graph_cli.skills.app_skill import SKILL_NAME, inventory_hash, render_app_skill
from graph_cli.skills.install import RenderedSkill, rendered
from graph_cli.skills.tree import graph_skill_name, validate

STATIC: Final = "static"
KINDS: Final = (STATIC,)


def app_skill(
    inventory: Mapping[str, JsonValue], program: Mapping[str, JsonValue]
) -> RenderedSkill:
    files = {name: text.encode("utf-8") for name, text in render_app_skill(inventory).items()}
    return rendered(
        SKILL_NAME,
        "app",
        files,
        program=program,
        source={"inventory_hash": inventory_hash(inventory)},
    )


def graph_skill(
    entry: GraphEntry, slot: SkillSlot, program: Mapping[str, JsonValue]
) -> RenderedSkill:
    """The skill of ``entry`` from its slot; ``skills.source_invalid`` if it cannot be used."""
    name = graph_skill_name(entry.name)
    details: dict[str, JsonValue] = {"graph": entry.name, "skill": name}
    if slot.kind not in KINDS:
        raise GraphCliError(
            "skills.source_invalid",
            f"the skill of graph {entry.name} has kind {slot.kind!r}, which this program"
            f" cannot install (known: {', '.join(KINDS)})",
            details={**details, "kind": slot.kind},
        )
    problems = validate(slot.files, name=name)
    if problems:
        raise GraphCliError(
            "skills.source_invalid",
            f"the published skill of graph {entry.name} is invalid: {problems[0].message}",
            details={**details, "problems": [p.to_json() for p in problems]},
        )
    pin = slot.published_against
    return rendered(
        name,
        "graph",
        slot.files,
        program=program,
        source={
            "graph": {"id": str(entry.id), "name": entry.name},
            "source_kind": slot.kind,
            "hash": slot.hash,
            "published_against": {"bundle": pin.bundle, "version": pin.version},
        },
    )


def behind_ontology(entry: GraphEntry, slot: SkillSlot) -> WarningRecord | None:
    """``skills.source_behind_ontology``: a static skill written for another release
    than the graph's pin may describe the model wrongly (F11, F12)."""
    if slot.kind != STATIC or slot.published_against == entry.pin:
        return None
    return WarningRecord(
        "skills.source_behind_ontology",
        f"the skill of graph {entry.name} was published against"
        f" {slot.published_against.bundle} {slot.published_against.version}; the graph is"
        f" at {entry.pin.version}",
        {
            "graph": entry.name,
            "published_against": slot.published_against.version,
            "pinned": entry.pin.version,
        },
    )
