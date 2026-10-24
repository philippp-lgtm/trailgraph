"""Authored policies (YAML, C.7) compiled to ``graph_cli.policy/1`` documents (contracts B6.13).

Two authored forms, told apart by ``id``:

- ``installation``: ``grants`` (who may use which graphs at which level,
  through the app or, with ``entrypoint: server``, the server API),
  optional ``maintainers``, project ``scopes``, the required ``no_project``
  scope, ``deny`` rules and ``obligations``;
- ``graph``: one graph's ``types`` limits, ``obligations`` and ``deny``
  rules. It only narrows.

Levels expand to action globs here (``kernel.actions``), graph names to ids,
so the compiled document says exactly what it means and its hash names that
meaning. Scopes narrow: a scope becomes deny rules for the actions above its
level; the ``no_project`` scope applies unless the actor names a scoped
project, so an absent and an unknown project get it alike. The installation
document always carries the built-in reason obligation.

Validation is total: every problem is collected with its path and the
compile fails with ``policy.invalid`` listing all of them. Types are checked
against the graph's pinned model when the caller passes it; an unknown type
is not an error (it may come with a later release), the caller warns.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from graph_cli.access_control.documents import (
    ASSERTED_KEYS,
    GRAPH_LAYER,
    INSTALLATION_LAYER,
    POLICY_SCHEMA,
    TYPE_GLOB_RE,
    Problem,
    document_problems,
    is_action_glob,
    is_write_glob,
)
from graph_cli.kernel.actions import ACCESS_LEVELS, actions_above, level_actions
from graph_cli.kernel.actor import is_valid_asserted_value
from graph_cli.kernel.canonical import JsonValue, hash_value
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.yaml_strict import (
    YamlError,
    YamlMap,
    YamlNode,
    YamlScalar,
    YamlSeq,
    load_strict,
)

BASE_OBLIGATION: Final[Mapping[str, JsonValue]] = {
    "action": ["write.supersede", "write.retract"],
    "require_reason": True,
}
ALL: Final = "*"
_INSTALLATION_KEYS: Final = frozenset(
    {"schema", "id", "grants", "maintainers", "scopes", "no_project", "deny", "obligations"}
)
_GRAPH_KEYS: Final = frozenset({"schema", "id", "types", "obligations", "deny"})
_PRINCIPAL_KEYS: Final = frozenset({"name", "id"})
_MATCHERS: Final = ("entrypoint", "principal_id", "principal_name", *ASSERTED_KEYS)
_DENY_ID_RE: Final = re.compile(r"^[a-z][a-z0-9_]*$")
# The entrypoints a grant may name; the maintenance entrypoint is for maintainers only.
GRANT_ENTRYPOINTS: Final = ("app", "server")


@dataclass(frozen=True, slots=True)
class Compiled:
    document: dict[str, JsonValue]
    hash: str
    layer: str
    unknown_types: tuple[str, ...] = ()


@dataclass
class _Context:
    graphs: Mapping[str, GraphId]  # name -> id
    graph: GraphId | None
    known_types: frozenset[str] | None
    extended_types: frozenset[str] = frozenset()
    problems: list[Problem] = field(default_factory=list)
    unknown_types: set[str] = field(default_factory=set)

    def add(self, path: str, message: str) -> None:
        self.problems.append(Problem(path, message))


def layer_of_source(data: bytes) -> str | None:
    """``installation`` or ``graph`` from the authored ``id``; ``None`` when unreadable."""
    try:
        root = load_strict(data)
    except YamlError:
        return None
    if isinstance(root, YamlMap):
        value = root.items.get("id")
        if isinstance(value, YamlScalar) and value.text in (INSTALLATION_LAYER, GRAPH_LAYER):
            return value.text
    return None


def compile_policy(
    data: bytes,
    *,
    file: str,
    graphs: Mapping[str, GraphId],
    graph: GraphId | None = None,
    known_types: frozenset[str] | None = None,
    extended_types: frozenset[str] = frozenset(),
) -> Compiled:
    """Compile one authored document. ``graphs`` maps every graph name to its id,
    ``graph`` is the graph a graph policy is compiled for, ``known_types`` the type
    CURIEs of that graph's pinned model (``None``: not checked), ``extended_types``
    those that are abstract or have subclasses: a limit matches the exact type only,
    so naming one of them would miss its subclasses' entities."""
    ctx = _Context(graphs, graph, known_types, extended_types)
    try:
        root = load_strict(data)
    except YamlError as exc:
        raise _invalid(file, [Problem("", str(exc))]) from exc
    document: dict[str, JsonValue] | None = None
    if not isinstance(root, YamlMap):
        ctx.add("", "the policy must be a mapping")
    else:
        schema = _text(root.items.get("schema"), "schema", ctx)
        if schema is not None and schema != POLICY_SCHEMA:
            ctx.add("schema", f"must be {POLICY_SCHEMA}")
        layer = _text(root.items.get("id"), "id", ctx)
        if layer == INSTALLATION_LAYER:
            document = _installation(root, ctx)
        elif layer == GRAPH_LAYER:
            if graph is None:
                ctx.add("id", "a graph policy is compiled for one graph; name it")
            else:
                document = _graph(root, graph, ctx)
        elif layer is not None:
            ctx.add("id", "must be installation or graph")
    if ctx.problems or document is None:
        raise _invalid(file, ctx.problems)
    leftover = document_problems(document)
    if leftover:
        raise GraphCliError("internal.error", f"compiled policy is malformed: {leftover[0]}")
    layer_name = str(document.get("layer"))
    return Compiled(document, hash_value(document), layer_name, tuple(sorted(ctx.unknown_types)))


# ---- installation --------------------------------------------------------------


def _installation(root: YamlMap, ctx: _Context) -> dict[str, JsonValue]:
    _unknown_keys(root, _INSTALLATION_KEYS, "", ctx)
    rules: list[JsonValue] = []
    grants = _seq(root.items.get("grants"), "grants", ctx, required=True)
    if grants is not None and not grants:
        ctx.add("grants", "must grant at least one level")
    for index, node in enumerate(grants or ()):
        rules += _grant(node, f"grants[{index}]", index + 1, ctx)
    maintainers = _seq(root.items.get("maintainers"), "maintainers", ctx)
    if maintainers is None:
        rules.append(_maintainer({}, "maint.any"))
    for index, node in enumerate(maintainers or ()):
        principal = _principal(node, f"maintainers[{index}]", ctx)
        if principal is not None:
            rules.append(_maintainer(principal, f"maint.{index + 1}"))
    scopes = _map(root.items.get("scopes"), "scopes", ctx) or {}
    projects = []
    for project, node in sorted(scopes.items()):
        if not is_valid_asserted_value(project):
            ctx.add(f"scopes.{project}", "is not a valid project name")
            continue
        projects.append(project)
        levels = _scope(node, f"scopes.{project}", ctx)
        rules += _scope_rules(levels, f"scope.{project}", {"project": [project]}, [], ctx)
    if "no_project" not in root.items:
        ctx.add("no_project", "is required: decide what an invocation without a project may do")
    else:
        levels = _scope(root.items["no_project"], "no_project", ctx)
        unless: list[JsonValue] = [{"principal": {"project": projects}}] if projects else []
        rules += _scope_rules(levels, "no_project", {}, unless, ctx)
    rules += _deny_rules(root.items.get("deny"), None, ctx)
    obligations = [dict(BASE_OBLIGATION), *_obligations(root.items.get("obligations"), ctx)]
    return {
        "schema": POLICY_SCHEMA,
        "id": INSTALLATION_LAYER,
        "layer": INSTALLATION_LAYER,
        "projects": list(projects),
        "rules": rules,
        "obligations": obligations,
    }


def _grant(node: YamlNode, path: str, number: int, ctx: _Context) -> list[JsonValue]:
    entry = _map(node, path, ctx, required=True)
    if entry is None:
        return []
    _unknown_keys(node, frozenset({"principal", "entrypoint", "graphs", "level"}), path, ctx)
    principal: dict[str, JsonValue] = {"entrypoint": _entrypoints(entry, path, ctx)}
    if "principal" in entry:
        found = _principal(entry["principal"], f"{path}.principal", ctx)
        principal.update(found or {})
    resources = _graph_resources(entry.get("graphs"), f"{path}.graphs", ctx, required=True)
    level = _level(entry.get("level"), f"{path}.level", ctx, allowed=("read", "write"))
    if level is None or resources is None:
        return []
    return [
        {
            "id": f"grant.{number}",
            "effect": "allow",
            "principal": principal,
            "action": list(level_actions(level)),
            "resource": resources,
        }
    ]


def _entrypoints(entry: Mapping[str, YamlNode], path: str, ctx: _Context) -> JsonValue:
    """``app`` unless the grant names ``server`` or both; one value stays a string, so a
    policy written before the server compiles to the same hash."""
    if "entrypoint" not in entry:
        return "app"
    names = _texts(entry["entrypoint"], f"{path}.entrypoint", ctx) or []
    unknown = [n for n in names if n not in GRANT_ENTRYPOINTS]
    if unknown:
        ctx.add(f"{path}.entrypoint", f"must be {' or '.join(GRANT_ENTRYPOINTS)}")
    chosen = sorted(set(names) & set(GRANT_ENTRYPOINTS)) or ["app"]
    return chosen[0] if len(chosen) == 1 else list(chosen)


def _maintainer(principal: Mapping[str, JsonValue], rule_id: str) -> dict[str, JsonValue]:
    return {
        "id": rule_id,
        "effect": "allow",
        "principal": {"entrypoint": "maint", **principal},
        "action": ["*"],
        "resource": "*",
    }


def _principal(node: YamlNode, path: str, ctx: _Context) -> dict[str, JsonValue] | None:
    entry = _map(node, path, ctx, required=True)
    if entry is None:
        return None
    _unknown_keys(node, _PRINCIPAL_KEYS, path, ctx)
    found: dict[str, JsonValue] = {}
    for key, facet in (("name", "principal_name"), ("id", "principal_id")):
        if key in entry:
            value = _text(entry[key], f"{path}.{key}", ctx)
            if value is not None:
                found[facet] = value
    if not entry:
        ctx.add(path, "names a principal by name or id")
    return found


def _scope(node: YamlNode, path: str, ctx: _Context) -> dict[str, str]:
    entry = _map(node, path, ctx, required=True) or {}
    levels: dict[str, str] = {}
    for name, value in sorted(entry.items()):
        if name != ALL and name not in ctx.graphs:
            ctx.add(f"{path}.{name}", "is not a graph of this installation")
            continue
        level = _level(value, f"{path}.{name}", ctx, allowed=ACCESS_LEVELS)
        if level is not None:
            levels[name] = level
    return levels


def _scope_rules(
    levels: Mapping[str, str],
    prefix: str,
    principal: Mapping[str, JsonValue],
    unless: list[JsonValue],
    ctx: _Context,
) -> list[JsonValue]:
    """Deny what lies above each level: per named graph, then for every other graph."""
    rules: list[JsonValue] = []
    named = {name: level for name, level in levels.items() if name != ALL}
    for name, level in sorted(named.items()):
        above = actions_above(level)
        if above:
            rule = _deny(f"{prefix}.{name}", above, [_graph_glob(ctx.graphs[name])], principal)
            if unless:
                rule["unless"] = list(unless)
            rules.append(rule)
    above = actions_above(levels.get(ALL, "none"))
    if above:
        rule = _deny(f"{prefix}.{ALL}", above, ["graph:*"], principal)
        conditions = list(unless)
        if named:
            conditions.append({"resource": sorted(_graph_glob(ctx.graphs[n]) for n in named)})
        if conditions:
            rule["unless"] = conditions
        rules.append(rule)
    return rules


# ---- graph -------------------------------------------------------------------------


def _graph(root: YamlMap, graph: GraphId, ctx: _Context) -> dict[str, JsonValue]:
    _unknown_keys(root, _GRAPH_KEYS, "", ctx)
    rules: list[JsonValue] = []
    types: dict[str, JsonValue] = {}
    for name, node in sorted((_map(root.items.get("types"), "types", ctx) or {}).items()):
        path = f"types.{name}"
        if not _type_glob(name, path, ctx):
            continue
        level = _level(node, path, ctx, allowed=ACCESS_LEVELS)
        if level == "none":
            ctx.add(path, "type-level read limits are not supported yet; use read or write")
        elif level is not None:
            types[name] = level
            if level == "read":
                rules.append(_deny(f"type.{name}", ("write.*",), [_type_glob_of(graph, name)], {}))
    rules += _deny_rules(root.items.get("deny"), graph, ctx)
    return {
        "schema": POLICY_SCHEMA,
        "id": GRAPH_LAYER,
        "layer": GRAPH_LAYER,
        "graph": str(graph),
        "types": types,
        "rules": rules,
        "obligations": list(_obligations(root.items.get("obligations"), ctx)),
    }


# ---- shared: deny rules, obligations ----------------------------------------------


def _deny_rules(node: YamlNode | None, graph: GraphId | None, ctx: _Context) -> list[JsonValue]:
    """``graph`` is the graph of a graph layer: its deny rules name types, not graphs."""
    rules: list[JsonValue] = []
    seen: set[str] = set()
    target_key = "type" if graph is not None else "graph"
    allowed = frozenset({"id", "action", target_key, *_MATCHERS})
    for index, item in enumerate(_seq(node, "deny", ctx) or ()):
        path = f"deny[{index}]"
        entry = _map(item, path, ctx, required=True)
        if entry is None:
            continue
        _unknown_keys(item, allowed, path, ctx)
        rule_id = _text(entry.get("id"), f"{path}.id", ctx, required=True)
        if rule_id is not None and not _DENY_ID_RE.match(rule_id):
            ctx.add(f"{path}.id", "must be lowercase letters, digits and _")
        elif rule_id is not None and rule_id in seen:
            ctx.add(f"{path}.id", f"duplicate deny id {rule_id!r}")
        actions = _actions(entry.get("action"), f"{path}.action", ctx)
        principal: dict[str, JsonValue] = {}
        for key in _MATCHERS:
            if key in entry:
                values = _texts(entry[key], f"{path}.{key}", ctx)
                if values is not None:
                    principal[key] = sorted(set(values))
        if graph is not None:
            types = _texts(entry["type"], f"{path}.type", ctx) if "type" in entry else None
            if types is not None and actions is not None and not all(map(is_write_glob, actions)):
                ctx.add(f"{path}.action", "a deny rule on types applies to writes only")
                actions = None
            resources = (
                [_graph_glob(graph)]
                if types is None
                else sorted(
                    {_type_glob_of(graph, t) for t in types if _type_glob(t, f"{path}.type", ctx)}
                )
            )
        else:
            found = _graph_resources(
                entry.get("graph", YamlScalar(ALL, True, 0)), f"{path}.graph", ctx
            )
            resources = found or []
        if rule_id is None or actions is None or not resources:
            continue
        seen.add(rule_id)
        rules.append(_deny(f"deny.{rule_id}", actions, resources, principal))
    return rules


def _obligations(node: YamlNode | None, ctx: _Context) -> list[JsonValue]:
    found: list[JsonValue] = []
    for index, item in enumerate(_seq(node, "obligations", ctx) or ()):
        path = f"obligations[{index}]"
        entry = _map(item, path, ctx, required=True)
        if entry is None:
            continue
        _unknown_keys(item, frozenset({"action", "require_reason", "max_batch"}), path, ctx)
        actions = _actions(entry.get("action"), f"{path}.action", ctx)
        if actions is not None and not all(map(is_write_glob, actions)):
            ctx.add(f"{path}.action", "obligations apply to writes only")
            actions = None
        obligation: dict[str, JsonValue] = {"action": list(actions or ())}
        if "require_reason" in entry:
            flag = _scalar(entry["require_reason"])
            value = flag.as_bool() if flag is not None else None
            if value is None:
                ctx.add(f"{path}.require_reason", "must be true or false")
            elif value:
                obligation["require_reason"] = True
        if "max_batch" in entry:
            number = _scalar(entry["max_batch"])
            batch = number.as_int() if number is not None else None
            if batch is None or batch < 1:
                ctx.add(f"{path}.max_batch", "must be a positive integer")
            else:
                obligation["max_batch"] = batch
        if set(obligation) == {"action"}:
            ctx.add(path, "requires a reason or limits the batch size")
        elif actions is not None:
            found.append(obligation)
    return found


def _deny(
    rule_id: str,
    actions: Sequence[str],
    resources: list[str],
    principal: Mapping[str, JsonValue],
) -> dict[str, JsonValue]:
    rule: dict[str, JsonValue] = {"id": rule_id, "effect": "deny"}
    if principal:
        rule["principal"] = dict(principal)
    rule["action"] = list(actions)
    rule["resource"] = list(resources)
    return rule


# ---- nodes -------------------------------------------------------------------------


def _graph_glob(graph: GraphId) -> str:
    return f"graph:{graph}*"


def _type_glob_of(graph: GraphId, name: str) -> str:
    return f"graph:{graph}/type:{name}"


def _graph_resources(
    node: YamlNode | None, path: str, ctx: _Context, *, required: bool = False
) -> list[str] | None:
    if node is None:
        if required:
            ctx.add(path, 'is required: "*" or a list of graph names')
        return None
    if isinstance(node, YamlScalar) and node.text == ALL:
        return ["graph:*"]
    names = _texts(node, path, ctx)
    if names is None:
        return None
    unknown = [n for n in names if n not in ctx.graphs]
    for name in unknown:
        ctx.add(path, f"{name!r} is not a graph of this installation")
    if unknown or not names:
        if not names:
            ctx.add(path, "names no graph")
        return None
    return sorted({_graph_glob(ctx.graphs[n]) for n in names})


def _type_glob(name: str, path: str, ctx: _Context) -> bool:
    if not TYPE_GLOB_RE.match(name):
        ctx.add(path, f"{name!r} is not a type CURIE or prefix:*")
        return False
    if name in ctx.extended_types:
        ctx.add(path, f"{name} is abstract or has subclasses; name the concrete types")
        return False
    known = ctx.known_types
    if known is not None:
        prefix = name[:-1] if name.endswith("*") else None
        if not (name in known if prefix is None else any(t.startswith(prefix) for t in known)):
            ctx.unknown_types.add(name)
    return True


def _actions(node: YamlNode | None, path: str, ctx: _Context) -> list[str] | None:
    values = _texts(node, path, ctx, required=True)
    if values is None:
        return None
    bad = [v for v in values if not is_action_glob(v)]
    for value in bad:
        ctx.add(path, f"unknown action {value!r}")
    return None if bad or not values else sorted(set(values))


def _level(
    node: YamlNode | None, path: str, ctx: _Context, *, allowed: Sequence[str]
) -> str | None:
    value = _text(node, path, ctx, required=True)
    if value is not None and value not in allowed:
        ctx.add(path, f"must be one of {', '.join(allowed)}")
        return None
    return value


def _unknown_keys(node: YamlNode, keys: frozenset[str], path: str, ctx: _Context) -> None:
    if isinstance(node, YamlMap):
        for key in sorted(set(node.items) - keys):
            ctx.add(f"{path}.{key}" if path else key, "unknown key")


def _map(
    node: YamlNode | None, path: str, ctx: _Context, *, required: bool = False
) -> dict[str, YamlNode] | None:
    if node is None:
        if required:
            ctx.add(path, "is required")
        return None
    if not isinstance(node, YamlMap):
        ctx.add(path, "must be a mapping")
        return None
    return dict(node.items)


def _seq(
    node: YamlNode | None, path: str, ctx: _Context, *, required: bool = False
) -> tuple[YamlNode, ...] | None:
    if node is None:
        if required:
            ctx.add(path, "is required")
        return None
    if not isinstance(node, YamlSeq):
        ctx.add(path, "must be a list")
        return None
    return node.items


def _scalar(node: YamlNode | None) -> YamlScalar | None:
    return node if isinstance(node, YamlScalar) and not node.is_null() else None


def _text(node: YamlNode | None, path: str, ctx: _Context, *, required: bool = True) -> str | None:
    scalar = _scalar(node)
    if scalar is None:
        if required or node is not None:
            ctx.add(path, "must be a string")
        return None
    return scalar.text


def _texts(
    node: YamlNode | None, path: str, ctx: _Context, *, required: bool = False
) -> list[str] | None:
    """A string or a list of strings."""
    if node is None:
        if required:
            ctx.add(path, "is required")
        return None
    if isinstance(node, YamlSeq):
        values = [_scalar(item) for item in node.items]
        if not values or any(v is None for v in values):
            ctx.add(path, "must be a non-empty list of strings")
            return None
        return [v.text for v in values if v is not None]
    value = _text(node, path, ctx)
    return None if value is None else [value]


def _invalid(file: str, problems: Sequence[Problem]) -> GraphCliError:
    shown = "; ".join(f"{p.path or '.'}: {p.message}" for p in problems[:5])
    more = len(problems) - 5
    if more > 0:
        shown += f"; and {more} more"
    return GraphCliError(
        "policy.invalid",
        f"{file} is not a valid policy: {shown}",
        details={"file": file, "problems": [p.to_json() for p in problems]},
    )
