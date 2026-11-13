"""Where installed skills come from (D15 Q2, Q6).

Skill 1 is rendered from this program's own inventory (the same document
``graph describe commands`` prints), never from another ``graph`` on
``PATH`` (F6). A graph skill comes from the graph's published slot and is
dispatched on the slot kind: ``static`` files are the skill as is; a
``generated`` slot holds a template, rendered with the model of the graph's
current pin (``graph_cli.skills.graph_skill``), so an ontology release
changes the render and an installed copy becomes ``stale`` without a
republish. Its curated text is checked against that model and this
program's inventory; problems are the warning ``skills.source_unknown_names``
and the skill is still installed (the model part is right).

A reader with type read limits gets the skill rendered from its model view (the documents
``model``), so the check of the curated text takes the documents of the full model
(``names``): curated text is the maintainer's prose, and a problem about a type the reader
cannot see would name it. The marker records a digest of what the reader sees (``visible``),
which the freshness check compares.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.json_access import as_object, req_obj, req_str
from graph_cli.kernel.ports.catalog import GraphEntry, SkillSlot
from graph_cli.kernel.skill_marker import GENERATED, STATIC, graph_skill_name
from graph_cli.skills.app_skill import SKILL_NAME, inventory_hash, render_app_skill
from graph_cli.skills.graph_skill import graph_block, render_checked
from graph_cli.skills.install import RenderedSkill, rendered
from graph_cli.skills.names import check_names
from graph_cli.skills.template import parse_template
from graph_cli.skills.tree import Problem, validate

KINDS: Final = (STATIC, GENERATED)


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
    entry: GraphEntry,
    slot: SkillSlot,
    program: Mapping[str, JsonValue],
    model: Mapping[str, JsonValue] | None = None,
    inventory: Mapping[str, JsonValue] | None = None,
    names: Mapping[str, JsonValue] | None = None,
    visible: str | None = None,
) -> tuple[RenderedSkill, list[WarningRecord]]:
    """The skill of ``entry`` from its slot; ``skills.source_invalid`` if it cannot be used.

    A ``generated`` slot needs ``model`` (the documents of the current pin) and
    ``inventory`` (this program's, for the checks of the curated text). For a reader with
    type read limits ``model`` is its view, ``names`` the documents of the full model the
    curated text is checked against (default: ``model``) and ``visible`` the digest of what
    it sees, recorded in the marker.
    """
    name = graph_skill_name(entry.name)
    details: dict[str, JsonValue] = {"graph": entry.name, "skill": name}
    if slot.kind not in KINDS:
        raise GraphCliError(
            "skills.source_invalid",
            f"the skill of graph {entry.name} has kind {slot.kind!r}, which this program"
            f" cannot install (known: {', '.join(KINDS)}); a newer graph may be needed",
            details={**details, "kind": slot.kind},
        )
    pin = slot.published_against
    source: dict[str, JsonValue] = {
        "graph": {"id": str(entry.id), "name": entry.name},
        "source_kind": slot.kind,
        "hash": slot.hash,
        "published_against": {"bundle": pin.bundle, "version": pin.version},
    }
    if visible is not None:
        source["visible"] = visible
    warnings: list[WarningRecord] = []
    if slot.kind == STATIC:
        files: Mapping[str, bytes] = slot.files
        _require(entry, details, validate(slot.files, name=name))
    else:
        if model is None or inventory is None:
            raise ValueError("a generated graph skill needs the model and the inventory")
        graph = graph_block(entry)
        found, problems = render_checked(slot.files, model, graph, name=name)
        _require(entry, details, problems)
        template, _ = parse_template(slot.files)
        assert found is not None and template is not None  # render_checked found no problem
        files = found
        ontology = req_obj(req_obj(as_object(dict(model), "model"), "types"), "ontology")
        source["model"] = {
            "bundle": req_str(ontology, "bundle"),
            "version": req_str(ontology, "version"),
        }
        unknown = check_names(template, model if names is None else names, graph, inventory)
        if unknown:
            warnings.append(unknown_names(entry, unknown))
    return rendered(name, "graph", files, program=program, source=source), warnings


def unknown_names(entry: GraphEntry, problems: list[Problem]) -> WarningRecord:
    """``skills.source_unknown_names``: curated text names what the model or this program
    does not have (Q3); the skill is installed anyway, its model part is right."""
    first = problems[0]
    more = f" and {len(problems) - 1} more" if len(problems) > 1 else ""
    return WarningRecord(
        "skills.source_unknown_names",
        f"the hand-written part of the skill of graph {entry.name} names something this"
        f" graph's model or this program does not have ({first.path}: {first.message}{more});"
        " ask the graph's maintainer to update and publish the skill again",
        {"graph": entry.name, "problems": [p.to_json() for p in problems]},
    )


def _require(entry: GraphEntry, details: dict[str, JsonValue], problems: list[Problem]) -> None:
    if not problems:
        return
    newer = any(p.rule == "template" for p in problems)
    raise GraphCliError(
        "skills.source_invalid",
        f"the published skill of graph {entry.name} is invalid: {problems[0].message}"
        + ("; a newer version of graph may know this template" if newer else ""),
        details={**details, "problems": [p.to_json() for p in problems]},
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
