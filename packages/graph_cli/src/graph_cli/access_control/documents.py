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
- ``roles`` (installation): the declared roles, ``{name: {description}}``; a rule's
  ``principal`` key ``role`` names roles an actor holds (a server token's roles);
- ``types`` (graph): the declared type limits (``read``, ``write``, ``none``), for display.

Resources (B6.9): ``installation``, ``graph:<id>``, a type ``graph:<id>/type:<curie>`` and,
below a type, one attribute ``.../attr:<name>`` or one relation ``.../rel:<curie>`` (the
terms a write touches). A published catalog entry is a resource of its own below the graph:
``graph:<id>/function:<name>``, ``.../action:<name>`` and ``.../view:<name>`` (the entry
resources; ``.../function:*`` is the glob of every function). ``read.function``, ``read.view``
and ``write.action`` are decided on them: a rule that names an entry resource names only these
actions or an area glob, and these actions are named on entry and graph resources only.
``read.view`` and ``write.action`` are in no level, a type or term rule never names
``write.action``, and only a ``catalog`` grant allows them. A rule on a type names write
actions (not ``write.action``), ``read.get`` or ``read.*``:
``read.get`` on a type is the type's visibility, the one read decision below a graph (every
other read is decided on ``graph:<id>`` itself); a rule on a term names write actions only.
A marking (field visibility) is the exact resource ``marking:<graph uuid>/<curie>``, outside
the ``graph:`` namespace on purpose: no glob of a graph, a scope or a deny rule covers it, so a
marked attribute stays hidden until a rule names its marking. A rule on a marking resource names
``read.get`` only (the marking is held or not) and never a glob of markings.
Globs match by prefix, so ``graph:<id>*``, ``graph:<id>/type:*`` and ``graph:<id>/type:ex:*``
cover the terms of every type they cover, as ``.../type:<curie>/attr:*`` and ``.../rel:*`` cover
the terms of one type. An exact type resource matches the type only, never its terms.
``fine_types`` tells which types some rule names terms of; ``read_limited`` tells whether some
rule decides the visibility of types (a rule on the glob ``graph:<id>*`` alone does not).

Allow rules match verified facets and the entrypoint only (B6.7): their
``project`` is the project a bound token verifies, never an asserted one, and
a ``role`` is verified (it comes from the server's token record only); a
graph layer has no allow rules, it only narrows. ``document_problems`` lists
every violation with its path; the loader turns them into
``policy.invalid``, ``authorize`` into ``internal.error``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from graph_cli.kernel.actions import (
    ENTRY_ACTIONS,
    ENTRY_NAME_RE,
    EXT_ACTION_RE,
    is_known_action,
)
from graph_cli.kernel.actor import (
    ActorContext,
    Assurance,
    Facet,
    is_valid_asserted_value,
    is_valid_role_name,
)
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.ids import GraphId

POLICY_SCHEMA: Final = "graph_cli.policy/1"
INSTALLATION_LAYER: Final = "installation"
GRAPH_LAYER: Final = "graph"
VERIFIED_KEYS: Final = ("entrypoint", "principal_id", "principal_name", "role")
# An allow rule may also name a project: it matches only a verified one (a bound token).
ALLOW_KEYS: Final = (*VERIFIED_KEYS, "project")
ASSERTED_KEYS: Final = ("agent_id", "agent_type", "session_id", "device_id", "project")
TYPE_LIMITS: Final = ("read", "write", "none")

_UUID: Final = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_CURIE: Final = r"[a-z][a-z0-9_]*:[A-Za-z][A-Za-z0-9_]*"
# An attribute name: the lower names of the ontology layer and of change sets.
_NAME: Final = r"[a-z][a-z0-9_]{0,63}"
# A marking is referred to as a lower-case CURIE (``ex:personal``).
_MARKING: Final = rf"[a-z][a-z0-9_]*:{_NAME}"
NAME_RE: Final = re.compile(rf"^{_NAME}$")
CURIE_RE: Final = re.compile(rf"^{_CURIE}$")
MARKING_RE: Final = re.compile(rf"^{_MARKING}$")
MARKING_RESOURCE_PREFIX: Final = "marking:"
# The name of a catalog entry, as in ``kernel.actions`` without its anchors.
_ENTRY: Final = ENTRY_NAME_RE.pattern.strip("^$")
_ENTRY_KIND: Final = r"(?:function|action|view)"
RESOURCE_RE: Final = re.compile(
    rf"^installation$|^graph:{_UUID}(/type:{_CURIE}(/attr:{_NAME}|/rel:{_CURIE})?"
    rf"|/{_ENTRY_KIND}:{_ENTRY})?$"
    rf"|^marking:{_UUID}/{_MARKING}$"
)
TYPE_GLOB_RE: Final = re.compile(rf"^(?:{_CURIE}|[a-z][a-z0-9_]*:\*)$")
_RESOURCE_GLOB_RE: Final = re.compile(
    rf"^(?:\*|installation|graph:\*|graph:{_UUID}(?:\*|/type:\*|/type:[a-z][a-z0-9_]*:\*"
    rf"|/type:{_CURIE}(?:/attr:(?:{_NAME}|\*)|/rel:(?:{_CURIE}|\*))?"
    rf"|/{_ENTRY_KIND}:(?:{_ENTRY}|\*))?"
    rf"|marking:{_UUID}/{_MARKING})$"
)
# A resource pattern that names an entry resource, and one that is a graph or below it only
# through an entry (what the entry-only actions are named on).
_ENTRY_PATTERN_RE: Final = re.compile(rf"^graph:{_UUID}/{_ENTRY_KIND}:(?:{_ENTRY}|\*)$")
_GRAPH_PATTERN_RE: Final = re.compile(rf"^graph:(?:\*|{_UUID}\*?)$")
_AREA_GLOBS: Final = frozenset({"*", "read.*", "write.*", "maint.*", "ext.*"})
# What a rule on an entry resource may name: the actions decided on entries, or an area glob.
_ENTRY_RESOURCE_ACTIONS: Final = frozenset({"read.function", *ENTRY_ACTIONS}) | _AREA_GLOBS
_EXT_GLOB_RE: Final = re.compile(r"^ext\.[a-z][a-z0-9_]{1,31}\.\*$")

_DOCUMENT_KEYS: Final = frozenset(
    {"schema", "id", "layer", "graph", "projects", "roles", "types", "rules", "obligations"}
)
_RULE_KEYS: Final = frozenset({"id", "effect", "principal", "action", "resource", "unless"})
_CONDITION_KEYS: Final = frozenset({"principal", "resource"})
_OBLIGATION_KEYS: Final = frozenset({"action", "require_reason", "max_batch"})
_ROLE_KEYS: Final = frozenset({"description"})

type _Add = Callable[[str, str], None]
# One value of a principal fact: a single value, the values of a multi-valued fact (a
# role matches when the actor holds at least one of the expected ones), or none.
type Fact = str | tuple[str, ...] | None


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


def is_write_glob(pattern: str) -> bool:
    """``write.*`` or one write action: all that term resources and obligations apply to
    (other reads and extensions are checked per graph, never per type)."""
    return pattern == "write.*" or (pattern.startswith("write.") and is_known_action(pattern))


def is_term_write_glob(pattern: str) -> bool:
    """``is_write_glob`` without the entry-only ``write.action``: a write the types and terms
    of a graph decide (``write.*`` covers it, but is never decided there)."""
    return is_write_glob(pattern) and pattern not in ENTRY_ACTIONS


def is_type_action_glob(pattern: str) -> bool:
    """What a rule on a type may name: a write glob but ``write.action`` (decided on entries
    only), ``read.get`` (the visibility of the type) or ``read.*``, which covers it. Any other
    read action is never decided on a type."""
    return is_term_write_glob(pattern) or pattern in ("read.get", "read.*")


def is_resource_glob(pattern: str) -> bool:
    return _RESOURCE_GLOB_RE.match(pattern) is not None


def glob(pattern: str, value: str) -> bool:
    """``*`` matches everything, ``prefix*`` a prefix, anything else itself."""
    if pattern.endswith("*"):
        return value.startswith(pattern[:-1])
    return pattern == value


def fine_types(
    documents: Sequence[Mapping[str, JsonValue]], graph: GraphId | str
) -> frozenset[str]:
    """The type CURIEs some rule of ``documents`` names terms of in ``graph``: those with a
    resource (of any rule, any effect, ``unless`` conditions included) that starts with
    ``graph:<graph>/type:<curie>/``. Only for these types can a decision on an attribute or
    relation differ from the decision on the type, so a caller records term decisions for
    them alone. Pure and cheap: cache it per policy hash."""
    prefix = f"graph:{graph}/type:"
    found: set[str] = set()
    for document in documents:
        for rule in rules(document):
            patterns = strings(rule, "resource")
            for condition in rules(rule, "unless"):
                patterns += strings(condition, "resource")
            for pattern in patterns:
                if pattern.startswith(prefix):
                    curie, below, _ = pattern[len(prefix) :].partition("/")
                    if below:
                        found.add(curie)
    return frozenset(found)


def read_limited(documents: Sequence[Mapping[str, JsonValue]], graph: GraphId | str) -> bool:
    """Whether some rule of ``documents`` (any layer, any effect) decides the visibility of the
    types of ``graph``: it names an action that covers ``read.get`` (``read.get``, ``read.*``,
    ``*``) and a type resource of the graph (``graph:<graph>/type:<curie>`` or a glob of types),
    as the compiled ``grant.<n>.reads`` rules, the ``type.<T>`` rules of ``none`` and graph deny
    rules with ``read.get`` do. A rule on ``graph:<graph>*`` or ``graph:*`` does not make a graph
    read-limited, whatever actions it names: it names no type, so no caller needs a filter
    for it. Pure and cheap: cache it per policy hash."""
    prefix = f"graph:{graph}/type:"
    for document in documents:
        for rule in rules(document):
            if not any(glob(pattern, "read.get") for pattern in strings(rule, "action")):
                continue
            for pattern in strings(rule, "resource"):
                if pattern.startswith(prefix) and "/" not in pattern[len(prefix) :]:
                    return True
    return False


def principal_facts(actor: ActorContext, *, verified_only: bool = False) -> dict[str, Fact]:
    """The values principal matchers compare with; ``None`` for an absent facet, and with
    ``verified_only`` (allow rules) for every facet that is not verified. ``role`` is the
    tuple of the actor's roles, ``None`` (matches nothing) when it holds none."""

    def asserted(facet: Facet, value: str | None) -> str | None:
        if verified_only:
            return value if facet.assurance is Assurance.VERIFIED else None
        return None if facet.assurance is Assurance.ABSENT else value

    return {
        "entrypoint": actor.client.entrypoint.value,
        "principal_id": actor.principal.id,
        "principal_name": actor.principal.name,
        "role": actor.roles or None,
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


def declared_roles(document: Mapping[str, JsonValue]) -> frozenset[str]:
    """The role names an installation document declares (none for a graph layer)."""
    value = document.get("roles", {})
    return frozenset(value) if isinstance(value, Mapping) else frozenset()


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
        if "roles" in document:
            add("roles", "only the installation layer declares roles")
        _types_problems(document.get("types", {}), add)
    else:
        for key in ("graph", "types"):
            if key in document:
                add(key, "only a graph layer has this key")
        _projects_problems(document.get("projects", []), add)
        _roles_problems(document.get("roles", {}), add)
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
        add("types", "must map types to none, read or write")
        return
    for name, limit in sorted(types.items()):
        if not TYPE_GLOB_RE.match(name):
            add(f"types.{name}", "is not a type CURIE or prefix:*")
        if limit not in TYPE_LIMITS:
            add(f"types.{name}", "must be none, read or write")


def _projects_problems(projects: JsonValue, add: _Add) -> None:
    if not isinstance(projects, list) or not all(
        isinstance(p, str) and is_valid_asserted_value(p) for p in projects
    ):
        add("projects", "must list project names")


def _roles_problems(roles: JsonValue, add: _Add) -> None:
    if not isinstance(roles, Mapping):
        add("roles", "must map role names to their declaration")
        return
    for name, declaration in sorted(roles.items()):
        path = f"roles.{name}"
        if not is_valid_role_name(name):
            add(path, "is not a role name (lowercase letters, digits and _)")
        if not isinstance(declaration, Mapping):
            add(path, "must be an object with a description")
            continue
        for key in sorted(set(declaration) - _ROLE_KEYS):
            add(f"{path}.{key}", "unknown key")
        description = declaration.get("description")
        if not isinstance(description, str) or not description.strip():
            add(f"{path}.description", "must be a non-empty string")


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
        keys = ALLOW_KEYS if effect == "allow" else (*VERIFIED_KEYS, *ASSERTED_KEYS)
        _principal_problems(rule.get("principal", {}), keys, f"{path}.principal", add)
        actions = rule.get("action")
        if not isinstance(actions, list) or not actions:
            add(f"{path}.action", "must be a non-empty list of actions")
        else:
            for pattern in actions:
                if not isinstance(pattern, str) or not is_action_glob(pattern):
                    add(f"{path}.action", f"unknown action {pattern!r}")
            resource = rule.get("resource")
            if _names_markings(resource) and not all(p == "read.get" for p in actions):
                add(
                    f"{path}.action",
                    "a rule on a marking names read.get only: a marking is held or not",
                )
            elif _names_terms(resource):
                if not all(isinstance(p, str) and is_term_write_glob(p) for p in actions):
                    add(
                        f"{path}.action",
                        "a rule on attributes or relations applies to writes only; name write"
                        " actions (not write.action, which is decided on entries)",
                    )
            elif _names_types(resource) and not all(
                isinstance(p, str) and is_type_action_glob(p) for p in actions
            ):
                add(
                    f"{path}.action",
                    "a rule on types names write actions (not write.action), read.get or"
                    " read.*; no other read is decided on a type",
                )
            _entry_problems(actions, resource, path, add)
        _resource_problems(rule.get("resource"), f"{path}.resource", add)
        if "unless" in rule:
            _unless_problems(rule["unless"], effect, f"{path}.unless", add)


def _entry_problems(actions: list[JsonValue], resource: JsonValue, path: str, add: _Add) -> None:
    """The entry resources (``graph:<id>/function:<name>`` and its siblings) are decided with
    ``read.function``, ``read.view`` and ``write.action`` only, and those three are decided on
    a graph or an entry only: never on ``installation``, a type, a term, a marking or ``*``."""
    patterns = [resource] if isinstance(resource, str) else resource
    if not isinstance(patterns, list):
        return
    if any(isinstance(p, str) and _ENTRY_PATTERN_RE.match(p) for p in patterns) and not all(
        isinstance(p, str) and p in _ENTRY_RESOURCE_ACTIONS for p in actions
    ):
        add(
            f"{path}.action",
            "a rule on a function, action or view names read.function, read.view or"
            " write.action (or an area such as read.*); no other action is decided on an entry",
        )
    named = sorted(p for p in actions if isinstance(p, str) and _is_entry_action(p))
    if named and not all(
        isinstance(p, str) and (_ENTRY_PATTERN_RE.match(p) or _GRAPH_PATTERN_RE.match(p))
        for p in patterns
    ):
        add(
            f"{path}.action",
            f"{named[0]} is decided on a graph or on one of its entries, not on installation,"
            " a type, an attribute, a relation, a marking or every resource",
        )


def _is_entry_action(pattern: str) -> bool:
    return pattern == "read.function" or pattern in ENTRY_ACTIONS


def _names_types(resource: JsonValue) -> bool:
    patterns = [resource] if isinstance(resource, str) else resource
    return isinstance(patterns, list) and any(
        isinstance(p, str) and "/type:" in p for p in patterns
    )


def _names_markings(resource: JsonValue) -> bool:
    patterns = [resource] if isinstance(resource, str) else resource
    return isinstance(patterns, list) and any(
        isinstance(p, str) and p.startswith(MARKING_RESOURCE_PREFIX) for p in patterns
    )


def _names_terms(resource: JsonValue) -> bool:
    patterns = [resource] if isinstance(resource, str) else resource
    return isinstance(patterns, list) and any(
        isinstance(p, str) and ("/attr:" in p or "/rel:" in p) for p in patterns
    )


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
        elif key == "role" and not all(is_valid_role_name(str(v)) for v in values):
            add(f"{path}.{key}", "must name roles (lowercase letters, digits and _)")


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
                elif not is_write_glob(pattern):
                    add(f"{path}.action", f"obligations apply to writes only, not {pattern!r}")
        if not isinstance(entry.get("require_reason", False), bool):
            add(f"{path}.require_reason", "must be true or false")
        batch = entry.get("max_batch")
        if batch is not None and (
            not isinstance(batch, int) or isinstance(batch, bool) or batch < 1
        ):
            add(f"{path}.max_batch", "must be a positive integer")
