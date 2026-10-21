"""The compiled ``graph_cli.policy/1`` document: grammar, checks and matching helpers
(contracts B6.6, B6.7, B6.13, B6.14).

A document is one layer: ``installation`` (the default; the built-in policy is
one) or ``graph`` (one graph's own policy, ``graph`` names it). Keys:

- ``rules``: ``{id, effect: allow|deny, principal?, action, resource, unless?}``;
  ``principal`` maps a facet key to a value or a list of alternatives,
  ``action`` is a list of action globs, ``resource`` one resource glob or a
  list; ``unless`` (deny rules only) lists conditions ``{principal?, resource?}``
  that lift the deny;
- ``obligations``: ``{action, require_reason?, max_batch?}``;
- ``projects`` (installation): the project names the scopes cover;
- ``types`` (graph): the declared type limits, for display.

Allow rules match verified facets and the entrypoint only (B6.7); a graph
layer has no allow rules, it only narrows. ``document_problems`` lists
every violation with its path; the loader turns them into
``policy.invalid``, ``authorize`` into ``internal.error``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from graph_cli.kernel.actions import EXT_ACTION_RE, is_known_action
from graph_cli.kernel.actor import ActorContext, Assurance, Facet, is_valid_asserted_value
from graph_cli.kernel.canonical import JsonValue

POLICY_SCHEMA: Final = "graph_cli.policy/1"
INSTALLATION_LAYER: Final = "installation"
GRAPH_LAYER: Final = "graph"
VERIFIED_KEYS: Final = ("entrypoint", "principal_id", "principal_name")
ASSERTED_KEYS: Final = ("agent_id", "agent_type", "session_id", "device_id", "project")
TYPE_LIMITS: Final = ("read", "write")

_UUID: Final = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_CURIE: Final = r"[a-z][a-z0-9_]*:[A-Za-z][A-Za-z0-9_]*"
RESOURCE_RE: Final = re.compile(rf"^installation$|^graph:{_UUID}(/type:{_CURIE})?$")
TYPE_GLOB_RE: Final = re.compile(rf"^(?:{_CURIE}|[a-z][a-z0-9_]*:\*)$")
_RESOURCE_GLOB_RE: Final = re.compile(
    rf"^(?:\*|installation|graph:\*|graph:{_UUID}(?:\*|/type:\*|/type:[a-z][a-z0-9_]*:\*"
    rf"|/type:{_CURIE})?)$"
)
_AREA_GLOBS: Final = frozenset({"*", "read.*", "write.*", "maint.*", "ext.*"})
_EXT_GLOB_RE: Final = re.compile(r"^ext\.[a-z][a-z0-9_]{1,31}\.\*$")

_DOCUMENT_KEYS: Final = frozenset(
    {"schema", "id", "layer", "graph", "projects", "types", "rules", "obligations"}
)
_RULE_KEYS: Final = frozenset({"id", "effect", "principal", "action", "resource", "unless"})
_CONDITION_KEYS: Final = frozenset({"principal", "resource"})
_OBLIGATION_KEYS: Final = frozenset({"action", "require_reason", "max_batch"})

type _Add = Callable[[str, str], None]


@dataclass(frozen=True, slots=True)
class Problem:
    path: str
    message: str

    def to_json(self) -> dict[str, JsonValue]:
        return {"path": self.path, "message": self.message}


def is_action_glob(pattern: str) -> bool:
    """``*``, an area (``read.*``), an extension namespace (``ext.notes.*``) or an action."""
    return (
        pattern in _AREA_GLOBS
        or is_known_action(pattern)
        or _EXT_GLOB_RE.match(pattern) is not None
        or EXT_ACTION_RE.match(pattern) is not None
    )


def is_resource_glob(pattern: str) -> bool:
    return _RESOURCE_GLOB_RE.match(pattern) is not None


def glob(pattern: str, value: str) -> bool:
    """``*`` matches everything, ``prefix*`` a prefix, anything else itself."""
    if pattern.endswith("*"):
        return value.startswith(pattern[:-1])
    return pattern == value


def principal_facts(actor: ActorContext) -> dict[str, str | None]:
    """The values principal matchers compare with; ``None`` for an absent facet."""

    def asserted(facet: Facet, value: str | None) -> str | None:
        return None if facet.assurance is Assurance.ABSENT else value

    return {
        "entrypoint": actor.client.entrypoint.value,
        "principal_id": actor.principal.id,
        "principal_name": actor.principal.name,
        "agent_id": asserted(actor.agent, actor.agent.id),
        "agent_type": asserted(actor.agent, actor.agent.type),
        "session_id": asserted(actor.session, actor.session.id),
        "device_id": asserted(actor.device, actor.device.id),
        "project": asserted(actor.project, actor.project.id),
    }


def layer_of(document: Mapping[str, JsonValue]) -> str:
    layer = document.get("layer", INSTALLATION_LAYER)
    return layer if isinstance(layer, str) else INSTALLATION_LAYER


def rules(document: Mapping[str, JsonValue], key: str = "rules") -> list[Mapping[str, JsonValue]]:
    """The objects of a list key; a checked document has nothing else there."""
    value = document.get(key, [])
    return [v for v in value if isinstance(v, Mapping)] if isinstance(value, list) else []


def strings(entry: Mapping[str, JsonValue], key: str) -> list[str]:
    value = entry.get(key)
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str)]


def document_problems(document: Mapping[str, JsonValue]) -> list[Problem]:
    """Every way ``document`` leaves the compiled grammar, with its path."""
    problems: list[Problem] = []

    def add(path: str, message: str) -> None:
        problems.append(Problem(path, message))

    if document.get("schema") != POLICY_SCHEMA:
        add("schema", f"must be {POLICY_SCHEMA}")
    for key in sorted(set(document) - _DOCUMENT_KEYS):
        add(key, "unknown key")
    if not isinstance(document.get("id"), str) or not document.get("id"):
        add("id", "must be a non-empty string")
    layer = document.get("layer", INSTALLATION_LAYER)
    if layer not in (INSTALLATION_LAYER, GRAPH_LAYER):
        add("layer", "must be installation or graph")
    if layer == GRAPH_LAYER:
        graph = document.get("graph")
        if not isinstance(graph, str) or not re.fullmatch(_UUID, graph):
            add("graph", "a graph layer names its graph by id")
        if "projects" in document:
            add("projects", "only the installation layer has project scopes")
        _types_problems(document.get("types", {}), add)
    else:
        for key in ("graph", "types"):
            if key in document:
                add(key, "only a graph layer has this key")
        _projects_problems(document.get("projects", []), add)
    _rules_problems(document.get("rules"), layer == GRAPH_LAYER, add)
    _obligations_problems(document.get("obligations", []), add)
    return problems


def check_document(
    document: Mapping[str, JsonValue], *, layer: str, graph: str | None = None
) -> list[Problem]:
    """``document_problems`` plus where the document was found: its layer and, for a graph
    layer, the graph whose directory holds it (a copied file is refused)."""
    problems = document_problems(document)
    if layer_of(document) != layer:
        problems.append(Problem("layer", f"a {layer} policy file holds another layer"))
    if graph is not None and document.get("graph") != graph:
        problems.append(Problem("graph", "the policy belongs to another graph"))
    return problems


def _types_problems(types: JsonValue, add: _Add) -> None:
    if not isinstance(types, Mapping):
        add("types", "must map types to read or write")
        return
    for name, limit in sorted(types.items()):
        if not TYPE_GLOB_RE.match(name):
            add(f"types.{name}", "is not a type CURIE or prefix:*")
        if limit not in TYPE_LIMITS:
            add(f"types.{name}", "must be read or write")


def _projects_problems(projects: JsonValue, add: _Add) -> None:
    if not isinstance(projects, list) or not all(
        isinstance(p, str) and is_valid_asserted_value(p) for p in projects
    ):
        add("projects", "must list project names")


def _rules_problems(value: JsonValue, graph_layer: bool, add: _Add) -> None:
    if not isinstance(value, list):
        add("rules", "must be a list of rules")
        return
    seen: set[str] = set()
    for index, rule in enumerate(value):
        path = f"rules[{index}]"
        if not isinstance(rule, Mapping):
            add(path, "is not an object")
            continue
        for key in sorted(set(rule) - _RULE_KEYS):
            add(f"{path}.{key}", "unknown key")
        rule_id = rule.get("id")
        if not isinstance(rule_id, str) or not rule_id:
            add(f"{path}.id", "must be a non-empty string")
        elif rule_id in seen:
            add(f"{path}.id", f"duplicate rule id {rule_id!r}")
        else:
            seen.add(rule_id)
        effect = rule.get("effect")
        if effect not in ("allow", "deny"):
            add(f"{path}.effect", "must be allow or deny")
        elif effect == "allow" and graph_layer:
            add(f"{path}.effect", "a graph layer only narrows; it has no allow rules")
        keys = VERIFIED_KEYS if effect == "allow" else (*VERIFIED_KEYS, *ASSERTED_KEYS)
        _principal_problems(rule.get("principal", {}), keys, f"{path}.principal", add)
        actions = rule.get("action")
        if not isinstance(actions, list) or not actions:
            add(f"{path}.action", "must be a non-empty list of actions")
        else:
            for pattern in actions:
                if not isinstance(pattern, str) or not is_action_glob(pattern):
                    add(f"{path}.action", f"unknown action {pattern!r}")
        _resource_problems(rule.get("resource"), f"{path}.resource", add)
        if "unless" in rule:
            _unless_problems(rule["unless"], effect, f"{path}.unless", add)


def _principal_problems(principal: JsonValue, keys: tuple[str, ...], path: str, add: _Add) -> None:
    if not isinstance(principal, Mapping):
        add(path, "must be an object")
        return
    for key, expected in sorted(principal.items()):
        if key not in keys:
            known = (*VERIFIED_KEYS, *ASSERTED_KEYS)
            add(
                f"{path}.{key}",
                "allow rules match verified facets and the entrypoint only"
                if key in known
                else "unknown facet",
            )
        values = expected if isinstance(expected, list) else [expected]
        if not values or not all(isinstance(v, str) and v for v in values):
            add(f"{path}.{key}", "must be a string or a non-empty list of strings")


def _resource_problems(resource: JsonValue, path: str, add: _Add) -> None:
    patterns = resource if isinstance(resource, list) else [resource]
    if not patterns:
        add(path, "must name at least one resource")
    for pattern in patterns:
        if not isinstance(pattern, str) or not is_resource_glob(pattern):
            add(path, f"unknown resource {pattern!r}")


def _unless_problems(unless: JsonValue, effect: JsonValue, path: str, add: _Add) -> None:
    if effect != "deny":
        add(path, "only deny rules have exceptions")
    if not isinstance(unless, list) or not unless:
        add(path, "must be a non-empty list of conditions")
        return
    for index, condition in enumerate(unless):
        where = f"{path}[{index}]"
        if not isinstance(condition, Mapping) or not condition:
            add(where, "must be a non-empty object")
            continue
        for key in sorted(set(condition) - _CONDITION_KEYS):
            add(f"{where}.{key}", "unknown key")
        if "principal" in condition:
            keys = (*VERIFIED_KEYS, *ASSERTED_KEYS)
            _principal_problems(condition["principal"], keys, f"{where}.principal", add)
        if "resource" in condition:
            _resource_problems(condition["resource"], f"{where}.resource", add)


def _obligations_problems(value: JsonValue, add: _Add) -> None:
    if not isinstance(value, list):
        add("obligations", "must be a list")
        return
    for index, entry in enumerate(value):
        path = f"obligations[{index}]"
        if not isinstance(entry, Mapping):
            add(path, "is not an object")
            continue
        for key in sorted(set(entry) - _OBLIGATION_KEYS):
            add(f"{path}.{key}", "unknown key")
        actions = entry.get("action")
        if not isinstance(actions, list) or not actions:
            add(f"{path}.action", "must be a non-empty list of actions")
        else:
            for pattern in actions:
                if not isinstance(pattern, str) or not is_action_glob(pattern):
                    add(f"{path}.action", f"unknown action {pattern!r}")
        if not isinstance(entry.get("require_reason", False), bool):
            add(f"{path}.require_reason", "must be true or false")
        batch = entry.get("max_batch")
        if batch is not None and (
            not isinstance(batch, int) or isinstance(batch, bool) or batch < 1
        ):
            add(f"{path}.max_batch", "must be a positive integer")
