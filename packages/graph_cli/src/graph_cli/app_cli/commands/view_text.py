"""The text of ``graph view``: one line per part of the card, read from the card's ``data`` only.

The JSON document is the contract (``kinetic.card``); this is one renderer of it, shorter than the
JSON, for people and hand checks. The hints name how to read what the card cut. An authored view
adds the ``neighbors``, ``list`` and ``rows`` sections (a heading and one line per row) and the
sections that were cut; a set view has no entity line and a hint that names its next page.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from graph_cli.app_cli.commands.common import Hint, command_line
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.langtag import tagged_parts

HINT_RELATIONS_MAX: Final = 3


def _map(value: JsonValue) -> Mapping[str, JsonValue]:
    return value if isinstance(value, Mapping) else {}


def _seq(value: JsonValue) -> Sequence[JsonValue]:
    return value if isinstance(value, Sequence) and not isinstance(value, str) else ()


def _strs(value: JsonValue) -> list[str]:
    return [str(v) for v in _seq(value)]


def _int(value: JsonValue) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _more(count: int) -> str:
    return f"+{count}" if count else ""


def ref_of(entity: Mapping[str, JsonValue]) -> str:
    return str(entity.get("alias") or entity["id"])


def view_text(data: JsonValue) -> str:
    """The card as lines of text."""
    card = _map(data)
    entity = _map(card.get("entity"))
    lines: list[str] = []
    if entity:
        label = entity.get("label")
        head = f"{ref_of(entity)}  " + (f"{label}  " if label else "")
        lines.append(f"{head}({entity['type']}, rev {entity['rev']}, {entity['record_status']})")
        for key in ("superseded_by", "retracted_by"):
            if entity.get(key):
                lines.append(f"  {key.replace('_', ' ')}: {', '.join(_strs(entity[key]))}")
    else:
        lines.append(f"view {_map(card.get('view')).get('name')}")
    for raw in _seq(card.get("sections")):
        section = _map(raw)
        kind = section.get("kind")
        if section.get("cut"):  # what is left of a section that had to go (an authored view)
            lines.append(f"{section.get('id', kind)}: cut to fit the size limit")
        elif kind == "fields":
            lines += _fields(section)
        elif kind == "relations":
            lines += _relations(section)
        elif kind == "files":
            lines += _cited_by(section) if section.get("variant") == "cited_by" else _files(section)
        elif kind == "affordances":
            lines += _affordances(section)
        elif kind in ("neighbors", "list"):
            lines += _listing(section)
        elif kind == "rows":
            lines += _function_rows(section)
    if card.get("sections_cut"):
        lines.append(f"{card['sections_cut']} more sections cut to fit the size limit")
    if card.get("oversize"):
        lines.append("oversize: one row is larger than the page limit")
    if card.get("more"):
        lines.append(f"more rows follow; next page: --cursor {card['next']}")
    if card.get("cut"):
        lines.append("cut: trimmed to fit the size limit")
    return "".join(f"{line}\n" for line in lines)


def _value(value: JsonValue) -> str:
    if isinstance(value, str):
        return value
    parts = tagged_parts(value)
    if parts is not None:
        return f"{parts[0]}@{parts[1]}"
    if isinstance(value, Sequence):
        return ", ".join(_value(v) for v in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _fields(section: Mapping[str, JsonValue]) -> list[str]:
    lines = []
    for raw in _seq(section.get("items")):
        item = _map(raw)
        text = f"  {item['name']}: {_value(item.get('value'))}"
        if item.get("chars"):
            text += f" ({item['chars']} characters)"
        if item.get("more"):
            text += f" (+{item['more']})"
        if item.get("role") == "state":
            text += " [state]"
        lines.append(text)
    other = _strs(section.get("other"))
    if other:
        text = f"  other fields: {', '.join(other)}"
        if section.get("other_more"):
            text += f" (+{section['other_more']})"
        lines.append(text)
    return lines


def _neighbour(item: Mapping[str, JsonValue]) -> str:
    text = str(item["ref"])
    if item.get("label"):
        text += f" {item['label']}"
    if item.get("type"):
        text += f" ({item['type']})"
    if item.get("state"):
        text += f" [{item['state']}]"
    if item.get("record_status"):
        text += f" <{item['record_status']}>"
    key = _map(item.get("field"))
    if key:
        text += f" ({key['name']}: {key['value']})"
    qualifiers = _map(item.get("qualifiers"))
    if qualifiers:
        text += " {" + ", ".join(f"{k}={_value(v)}" for k, v in qualifiers.items()) + "}"
    return text


def _relations(section: Mapping[str, JsonValue]) -> list[str]:
    lines = []
    for raw in _seq(section.get("groups")):
        group = _map(raw)
        items = [_map(i) for i in _seq(group.get("items"))]
        arrow = "->" if group["direction"] == "out" else "<-"
        count = f"~{group['count']}" if group.get("approx") else str(group["count"])
        names = "; ".join(_neighbour(i) for i in items)
        if group.get("more"):
            names = f"first {len(items)} by name: {names}"
        notes = [_more(_int(group.get("more")))]
        states = _map(group.get("states"))
        notes.append(", ".join(f"{n} {s}" for s, n in states.items()))
        if group.get("sampled"):
            notes.append(f"from a sample of {group['sampled']}")
        if group.get("missing"):
            notes.append(f"{group['missing']} missing")
        notes = [n for n in notes if n]
        suffix = f"  ({'; '.join(notes)})" if notes else ""
        lines.append(f"{group['relation']} {arrow} {count:>3}  {names}{suffix}")
    more = _strs(section.get("more_groups"))
    if more:
        lines.append(f"more relations: {', '.join(more)}")
    return lines


def _pages(page: JsonValue, last: JsonValue) -> str:
    if page is None:
        return ""
    return f" p. {page}-{last}" if last else f" p. {page}"


def _files(section: Mapping[str, JsonValue]) -> list[str]:
    shown = []
    for raw in _seq(section.get("items")):
        item = _map(raw)
        text = str(item["ref"])
        if item.get("title"):
            text += f' "{item["title"]}"'
        text += _pages(item.get("page"), item.get("last_page"))
        if item.get("locator"):
            text += f" at {item['locator']}"
        shown.append(text)
    more = f" (+{section['more']})" if section.get("more") else ""
    return [f"files: {'; '.join(shown)}{more}"]


def _cited_by(section: Mapping[str, JsonValue]) -> list[str]:
    count = f"~{section['count']}" if section.get("approx") else str(section["count"])
    head = f"cited by {count}"
    if section.get("page_min") is not None:
        head += f", pages {section['page_min']}-{section['page_max']}"
        head += f" ({section['pages_cited']} distinct)"
    shown = []
    for raw in _seq(section.get("items")):
        item = _map(raw)
        text = str(item["ref"])
        if item.get("label"):
            text += f' "{item["label"]}"'
        shown.append(text + _pages(item.get("page"), item.get("last_page")))
    more = f" (+{section['more']})" if section.get("more") else ""
    return [f"{head}: {'; '.join(shown)}{more}"]


def _affordances(section: Mapping[str, JsonValue]) -> list[str]:
    can: list[str] = []
    blocked: list[str] = []
    for raw in _seq(section.get("transitions")):
        entry = _map(raw)
        attr = entry.get("attr")
        if entry.get("allowed"):
            more = f" (+{entry['allowed_more']})" if entry.get("allowed_more") else ""
            can.append(f"{attr} -> {', '.join(_strs(entry['allowed']))}{more}")
        for block in _seq(entry.get("blocked")):
            item = _map(block)
            blocked.append(f"{attr} -> {item['state']} ({', '.join(_strs(item.get('by')))})")
    actions = _map(section.get("actions"))
    more_actions = f" (+{section['actions_more']})" if section.get("actions_more") else ""
    if actions.get("apply"):
        can.append(f"do {', '.join(_strs(actions['apply']))}{more_actions}")
    if actions.get("blocked"):
        number = _int(actions["blocked"])
        blocked.append(f"{number} action{'' if number == 1 else 's'}")
    if section.get("functions"):
        more = f" (+{section['functions_more']})" if section.get("functions_more") else ""
        can.append(f"run {', '.join(_strs(section['functions']))}{more}")
    parts = []
    if can:
        parts.append("can: " + "; ".join(can))
    if blocked:
        parts.append("blocked: " + "; ".join(blocked))
    lines = ["; ".join(parts)] if parts else []
    if section.get("detail"):
        lines.append(f"detail: {section['detail']}")
    return lines


def _listing(section: Mapping[str, JsonValue]) -> list[str]:
    """A ``neighbors`` or ``list`` section: a heading, then one line per row."""
    what = section.get("type") or f"{section.get('relation')} {section.get('direction')}"
    if isinstance(what, Sequence) and not isinstance(what, str):
        what = ", ".join(str(w) for w in what)
    count = f"~{section['count']}" if section.get("approx") else str(section["count"])
    notes = [_more(_int(section.get("more")))]
    if section.get("from"):
        notes.append(f"from row {section['from']}")
    if section.get("unread"):
        notes.append(f"{section['unread']} edges not read")
    suffix = "".join(f", {n}" for n in notes if n)
    lines = [f"{_title(section)}: {section['kind']} {what} ({count}{suffix})"]
    lines += [f"  {_row(_map(raw))}" for raw in _seq(section.get("items"))]
    return lines


def _title(section: Mapping[str, JsonValue]) -> str:
    text = str(section.get("id", section["kind"]))
    return f'{text} "{section["title"]}"' if section.get("title") else text


def _row(row: Mapping[str, JsonValue]) -> str:
    text = str(row["ref"])
    if row.get("label"):
        text += f" {row['label']}"
    if row.get("type"):
        text += f" ({row['type']})"
    if row.get("record_status"):
        text += f" <{row['record_status']}>"
    qualifiers = _map(row.get("qualifiers"))
    if qualifiers:
        text += " {" + ", ".join(f"{k}={_value(v)}" for k, v in qualifiers.items()) + "}"
    fields = _map(row.get("fields"))
    if fields:
        text += "  " + " ".join(f"{k}={_value(v)}" for k, v in fields.items())
    groups = []
    for raw in _seq(row.get("relations")):
        group = _map(raw)
        arrow = "->" if group.get("direction") == "out" else "<-"
        items = ", ".join(_neighbour(_map(i)) for i in _seq(group.get("items")))
        more = f" (+{group['more']})" if group.get("more") else ""
        groups.append(f"{group['relation']} {arrow} {items}{more}")
    if groups:
        text += "; " + "; ".join(groups)
    return text


def _function_rows(section: Mapping[str, JsonValue]) -> list[str]:
    """A ``rows`` section: a heading, then the rows of the function as ``column=value``."""
    function = _map(section.get("function"))
    error = _map(section.get("error"))
    if error:
        return [
            f"{_title(section)}: rows {function.get('name')} failed",
            f"  {error['code']}: {error['message']}",
        ]
    notes = [_more(_int(section.get("more")))]
    if section.get("from"):
        notes.append(f"from row {section['from']}")
    if section.get("cells_cut"):
        notes.append(f"{section['cells_cut']} cells cut")
    suffix = "".join(f", {n}" for n in notes if n)
    lines = [f"{_title(section)}: rows {function.get('name')} ({section['count']}{suffix})"]
    for raw in _seq(section.get("items")):
        lines.append("  " + " ".join(f"{k}={_value(v)}" for k, v in _map(raw).items()))
    return lines


def view_hints(
    data: JsonValue,
    graph: str,
    *,
    name: str | None = None,
    params: tuple[str, ...] = (),
    page_bytes: int | None = None,
    pinned: tuple[tuple[str, tuple[str, ...]], ...] = (),
) -> tuple[Hint, ...]:
    """What to run to read what the card cut: the neighbours of the relations it cut, and the
    attributes it only named; for a set view, the next page (``name``, the ``--param`` texts
    and the page size of the call are repeated, the cursor is tied to them)."""
    card = _map(data)
    if "entity" not in card:
        cursor = card.get("next")
        if not isinstance(cursor, str) or name is None:
            return ()
        options: list[tuple[str, tuple[str, ...]]] = [("--name", (name,)), ("--cursor", (cursor,))]
        if params:
            options.append(("--param", params))
        if page_bytes is not None:
            options.append(("--page-bytes", (str(page_bytes),)))
        options += pinned
        line = command_line("view", (), graph, tuple(options), (), 0, default_limit=0)
        return ({"text": "More rows available", "command": line},)
    ref = ref_of(_map(card.get("entity")))
    relations: list[str] = []
    other: list[str] = []
    other_more = 0
    for raw in _seq(card.get("sections")):
        section = _map(raw)
        if section.get("kind") == "relations":
            for group in _seq(section.get("groups")):
                if _map(group).get("more"):
                    relations.append(str(_map(group)["relation"]))
            relations += _strs(section.get("more_groups"))
        elif section.get("kind") == "fields":
            other = _strs(section.get("other"))
            other_more = _int(section.get("other_more"))
    hints: list[Hint] = []
    relations = list(dict.fromkeys(relations))
    if relations:
        line = command_line(
            "neighbors",
            (ref,),
            graph,
            (("--rel", tuple(relations[:HINT_RELATIONS_MAX])),),
            (),
            0,
            default_limit=0,
        )
        hints.append(
            {
                "text": "The card shows the first neighbours by name of each relation; "
                f"more in: {', '.join(relations)}",
                "command": line,
            }
        )
    if other:
        line = command_line(
            "get", (ref,), graph, (("--fields", (",".join(other),)),), (), 0, default_limit=0
        )
        more = f" (and {other_more} more)" if other_more else ""
        named = ", ".join(other)
        hints.append(
            {
                "text": f"Attributes with a value that the card only names: {named}{more}",
                "command": line,
            }
        )
    return tuple(hints)
