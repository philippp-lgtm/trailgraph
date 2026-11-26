"""Authored policies (YAML, C.7) compiled to ``graph_cli.policy/1`` documents (contracts B6.13).

Two authored forms, told apart by ``id``:

- ``installation``: optional ``roles`` (the declared role names, each with a
  description), ``grants`` (who may use which graphs at which level,
  through the app or, with ``entrypoint: server``, the server API; a grant
  may name a project, which only a token bound to it verifies, or a role,
  which only a server token holding it carries; a ``level: read`` grant on named
  graphs may carry ``writes``, see below), optional ``maintainers``,
  project ``scopes``, the required ``no_project`` scope, ``deny`` rules and
  ``obligations``;
- ``graph``: one graph's ``types`` limits, ``obligations`` and ``deny``
  rules (a rule with ``type`` may add ``attribute`` or ``relation``). It only narrows.

A role is a subject like a token or a project: its rights are the rules that
name it. Grants and deny rules of the installation layer may name only declared
roles (a typo is ``policy.invalid``); a graph layer is applied independently, so
a role the installed installation policy does not declare is reported, not
refused (``Compiled.unknown_roles``), like an unknown type. A policy without
roles compiles to the same document, hence the same hash, as before roles existed.

``reads`` is a grant's read allow list: type CURIEs (or ``prefix:*``) the grant's callers may see
(``read.get`` on the type is its visibility, B6.9). The grant's read rule ``grant.<n>`` then
names the exact ``graph:<id>`` instead of the glob ``graph:<id>*`` (which would cover every type
resource) and one more allow rule, ``grant.<n>.reads`` (action ``read.get``), names the type
resources of every named graph, sorted. It needs ``level: read`` and named graphs, like
``writes``; with both, every ``writes`` type must be covered by ``reads``. A grant without
``reads`` compiles as before, to the same hash.

``writes`` is a grant's write allow list: concrete type CURIEs, each mapping verbs
(``create``, ``update``, ``link``, ``unlink``, ``transition``, ``supersede``, ``retract``,
``alias``) to ``true``, ``false`` or, for the first five, a non-empty list of attribute names
(``create``, ``update``), relation CURIEs (``link``, ``unlink``) or lifecycle attribute names
(``transition``); an absent or ``false`` verb is not granted and a type grants at least one.
A list is sorted and deduplicated, so duplicates are no problem. A grant keeps its read rule
``grant.<n>`` and gains one allow rule per granted verb, ``grant.<n>.<verb>`` (action
``write.<verb>``), in the order create, update, link, edge, unlink, transition, supersede,
retract, alias; ``grant.<n>.edge`` carries ``write.create`` on the relation resources of the
``link`` entries only (an inline edge of a create needs the create right on its type and the
link right on its relation). Resources are ``graph:<id>/type:<T>`` plus ``/attr:*`` or
``/attr:<a>`` (create, update, transition) or ``/rel:*`` or ``/rel:<r>`` (link, unlink),
across every named graph and type that grants the verb, sorted. The compiled document never
depends on a model.

``markings`` is a grant's list of markings (CURIEs, ``ex:personal``): the grant's callers may
see the attributes that carry them (field visibility). It takes any level and needs named
graphs, like ``writes`` and ``reads``: markings belong to an ontology. It compiles to one more
allow rule, ``grant.<n>.markings`` (action ``read.get``), after the read rules, naming the
resource ``marking:<graph id>/<curie>`` of every named graph and marking, sorted. Such a
resource lies outside ``graph:`` so that no grant, scope or deny rule that names a graph covers
it: a marked attribute is hidden from everybody until a grant names its marking. A grant
without ``markings`` compiles as before, to the same hash.

``level: catalog`` limits a server grant to named catalog entries: a ``catalog`` key lists the
``functions``, ``actions`` and ``views`` (names of the entry pattern) the grant runs and nothing
else of the graph is granted. It needs ``entrypoint: server`` (locally an agent with a shell reads
the files, so the profile would only pretend), named graphs, a ``reads`` list (the types its
entries may read, so a later release's new types never widen it) and refuses ``writes`` (an
action writes through its own entry). Its rules: ``grant.<n>`` (``read.list`` and
``read.describe`` on the exact ``graph:<id>``), ``grant.<n>.reads`` as above, then one rule per
kind that names entries, ``grant.<n>.functions`` (``read.function`` on
``graph:<id>/function:<name>``), ``grant.<n>.views`` (``read.view``, ``.../view:<name>``) and
``grant.<n>.actions`` (``write.action``, ``.../action:<name>``), then ``grant.<n>.markings``;
every resource list is sorted. ``catalog.actions`` may also stand on a ``level: read`` grant
(a reader that may also write through these actions: the rule ``grant.<n>.actions`` joins
the read rules); ``catalog.functions`` and ``catalog.views`` are refused there as redundant, and
``catalog`` is refused on ``level: write``. ``read.view`` and ``write.action`` are in no level,
so a grant without ``catalog`` compiles as before, to the same hash.

``types: {T: none}`` of a graph layer hides ``T`` from the app and server entrypoints: the deny
rule ``type.<T>`` names ``read.get`` and ``write.*`` on the exact type and carries
``principal: {entrypoint: [app, server]}``, so a maintainer is not hit. A graph deny rule with
``type`` may name ``read.get`` (alone or with write actions); it compiles as written.

A graph deny rule with ``type`` and ``attribute`` (or ``relation``) denies on
``graph:<id>/type:<T>/attr:<a>`` (``/rel:<r>``) for every listed type instead of on the type;
its actions must be write actions that can decide such a term, and a ``relation`` rule on
``write.link`` also denies ``write.create``, which adds the inline edges of a create.

Levels expand to action globs here (``kernel.actions``), graph names to ids,
so the compiled document says exactly what it means and its hash names that
meaning. Scopes narrow: a scope becomes deny rules for the actions above its
level; the ``no_project`` scope applies unless the actor names a scoped
project, so an absent and an unknown project get it alike. A scope's
``maintain`` level is ``write`` for the project's callers plus one allow rule
that lets an admin token bound to the project run the graph-level
maintenance actions on the graphs it names, and read their data as check
findings do (``authorize`` keeps such a token from every other rule). The installation
document always carries the built-in reason obligation.

Validation is total: every problem is collected with its path and the
compile fails with ``policy.invalid`` listing all of them. Types and terms are
checked against the graph's pinned model when the caller passes it (``known_types``
and ``extended_types`` for type limits, ``models`` for ``reads``, ``writes`` and term deny rules);
an abstract or extended type is an error, an unknown type or term is not (it may come
with a later release), the caller warns.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from graph_cli.access_control.documents import (
    ASSERTED_KEYS,
    CURIE_RE,
    GRAPH_LAYER,
    INSTALLATION_LAYER,
    MARKING_RE,
    NAME_RE,
    POLICY_SCHEMA,
    TYPE_GLOB_RE,
    Problem,
    document_problems,
    is_action_glob,
    is_type_action_glob,
    is_write_glob,
)
from graph_cli.access_control.terms import (
    HiddenRangeCreate,
    TermModel,
    TypeTerms,
    UnknownMarking,
    UnknownReadType,
    UnknownTerm,
    UnknownWriteType,
    UnusableMarkedWrite,
    UnusableWrite,
    marking_resource,
)
from graph_cli.kernel.actions import (
    ACCESS_LEVELS,
    CATALOG_LEVEL,
    ENTRY_KIND_ACTIONS,
    ENTRY_NAME_RE,
    ENTRY_PLURALS,
    MAINTAIN_LEVEL,
    PROJECT_ADMIN_ACTIONS,
    SCOPE_LEVELS,
    actions_above,
    level_actions,
)
from graph_cli.kernel.actor import is_valid_asserted_value, is_valid_role_name
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
# The verbs of a ``writes`` entry, in the order their rules are compiled.
WRITE_VERBS: Final = (
    "create",
    "update",
    "link",
    "unlink",
    "transition",
    "supersede",
    "retract",
    "alias",
)
# Verbs that take a list of terms, and what the list names; the others are true or false.
_LIST_VERBS: Final = {
    "create": "attribute names",
    "update": "attribute names",
    "link": "relation CURIEs",
    "unlink": "relation CURIEs",
    "transition": "lifecycle attribute names",
}
_ATTRIBUTE_VERBS: Final = ("create", "update", "transition")
# The write actions that can decide an attribute or a relation of a type.
_ATTRIBUTE_ACTIONS: Final = frozenset({"write.create", "write.update", "write.transition"})
_RELATION_ACTIONS: Final = frozenset({"write.create", "write.link", "write.unlink"})
_KIND: Final = {"attribute": "attr", "relation": "rel"}
_INSTALLATION_KEYS: Final = frozenset(
    {
        "schema",
        "id",
        "roles",
        "grants",
        "maintainers",
        "scopes",
        "no_project",
        "deny",
        "obligations",
    }
)
_GRAPH_KEYS: Final = frozenset({"schema", "id", "types", "obligations", "deny"})
_PRINCIPAL_KEYS: Final = frozenset({"name", "id"})
_GRANT_PRINCIPAL_KEYS: Final = frozenset({*_PRINCIPAL_KEYS, "project", "role"})
_MATCHERS: Final = ("entrypoint", "principal_id", "principal_name", *ASSERTED_KEYS, "role")
_DENY_ID_RE: Final = re.compile(r"^[a-z][a-z0-9_]*$")
# The entrypoints a grant may name; the maintenance entrypoint is for maintainers only.
GRANT_ENTRYPOINTS: Final = ("app", "server")
# ``level: catalog``: the actions its base rule ``grant.<n>`` allows on the graph itself, the
# kinds of entry a ``catalog`` key lists in the order their rules are compiled, and the entry
# kind and the action each of them is decided with.
CATALOG_BASE_ACTIONS: Final = ("read.list", "read.describe")
CATALOG_KINDS: Final = ("functions", "views", "actions")
_CATALOG_ENTRIES: Final = {
    ENTRY_PLURALS[kind]: (kind, str(ENTRY_KIND_ACTIONS[kind]))
    for kind in ("function", "view", "action")
}
# One kind's names of a ``catalog`` key: sorted, without duplicates.
type _Catalog = dict[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class Compiled:
    document: dict[str, JsonValue]
    hash: str
    layer: str
    unknown_types: tuple[str, ...] = ()
    unknown_roles: tuple[str, ...] = ()
    # Found against the models of ``compile_policy(models=...)``; all sorted.
    unknown_write_types: tuple[UnknownWriteType, ...] = ()
    unknown_read_types: tuple[UnknownReadType, ...] = ()
    unknown_terms: tuple[UnknownTerm, ...] = ()
    unusable_writes: tuple[UnusableWrite, ...] = ()
    hidden_range_creates: tuple[HiddenRangeCreate, ...] = ()
    unknown_markings: tuple[UnknownMarking, ...] = ()
    unusable_marked_writes: tuple[UnusableMarkedWrite, ...] = ()


# One verb's terms of a type: sorted, without duplicates; ``()`` = every term (``true``).
type _Terms = tuple[str, ...]
# type CURIE -> verb -> terms, the verbs that are granted.
type _Writes = dict[str, dict[str, _Terms]]


@dataclass
class _Context:
    graphs: Mapping[str, GraphId]  # name -> id
    graph: GraphId | None
    known_types: frozenset[str] | None
    extended_types: frozenset[str] = frozenset()
    # Graph layer: the roles of the installed installation policy (``None``: not checked).
    # Installation layer: the roles this policy declares (set by ``_roles``).
    known_roles: frozenset[str] | None = None
    models: Callable[[GraphId], TermModel | None] | None = None
    problems: list[Problem] = field(default_factory=list)
    unknown_types: set[str] = field(default_factory=set)
    unknown_roles: set[str] = field(default_factory=set)
    unknown_write_types: set[UnknownWriteType] = field(default_factory=set)
    unknown_read_types: set[UnknownReadType] = field(default_factory=set)
    unknown_terms: set[UnknownTerm] = field(default_factory=set)
    unusable_writes: set[UnusableWrite] = field(default_factory=set)
    hidden_range_creates: set[HiddenRangeCreate] = field(default_factory=set)
    unknown_markings: set[UnknownMarking] = field(default_factory=set)
    unusable_marked_writes: set[UnusableMarkedWrite] = field(default_factory=set)
    _models: dict[GraphId, TermModel | None] = field(default_factory=dict)

    def add(self, path: str, message: str) -> None:
        self.problems.append(Problem(path, message))

    def model_of(self, graph: GraphId) -> TermModel | None:
        """The term model of ``graph``, asked for once; ``None``: not checked."""
        if self.models is None:
            return None
        if graph not in self._models:
            self._models[graph] = self.models(graph)
        return self._models[graph]

    def graph_name(self, graph: GraphId) -> str:
        return next((name for name, found in self.graphs.items() if found == graph), str(graph))


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
    roles: frozenset[str] | None = None,
    models: Callable[[GraphId], TermModel | None] | None = None,
) -> Compiled:
    """Compile one authored document. ``graphs`` maps every graph name to its id,
    ``graph`` is the graph a graph policy is compiled for, ``known_types`` the type
    CURIEs of that graph's pinned model (``None``: not checked), ``extended_types``
    those that are abstract or have subclasses: a limit matches the exact type only,
    so naming one of them would miss its subclasses' entities. ``roles`` are the roles
    the installed installation policy declares (``None``: not checked); a graph policy's
    deny rules naming others are collected in ``unknown_roles``. ``models`` is asked,
    lazily and once per graph, for the term model of each graph a ``reads``, ``writes`` or
    ``markings`` grant names and of the graph of a graph policy that has term deny rules
    (``None``, or no model for a graph: not checked); abstract or extended read and write types
    are problems, unknown types, markings and terms and unusable writes are collected."""
    ctx = _Context(graphs, graph, known_types, extended_types, roles, models)
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
    return Compiled(
        document,
        hash_value(document),
        layer_name,
        tuple(sorted(ctx.unknown_types)),
        tuple(sorted(ctx.unknown_roles)),
        tuple(sorted(ctx.unknown_write_types)),
        tuple(sorted(ctx.unknown_read_types)),
        tuple(sorted(ctx.unknown_terms)),
        tuple(sorted(ctx.unusable_writes)),
        tuple(sorted(ctx.hidden_range_creates)),
        tuple(sorted(ctx.unknown_markings)),
        tuple(sorted(ctx.unusable_marked_writes)),
    )


# ---- installation --------------------------------------------------------------


def _installation(root: YamlMap, ctx: _Context) -> dict[str, JsonValue]:
    _unknown_keys(root, _INSTALLATION_KEYS, "", ctx)
    roles = _roles(root.items.get("roles"), ctx)
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
        levels = _scope(node, f"scopes.{project}", ctx, allowed=SCOPE_LEVELS)
        rules += _scope_rules(levels, f"scope.{project}", {"project": [project]}, [], ctx)
        rules += _maintain_rule(project, levels, ctx)
    if "no_project" not in root.items:
        ctx.add("no_project", "is required: decide what an invocation without a project may do")
    else:
        levels = _scope(root.items["no_project"], "no_project", ctx, allowed=ACCESS_LEVELS)
        unless: list[JsonValue] = [{"principal": {"project": projects}}] if projects else []
        rules += _scope_rules(levels, "no_project", {}, unless, ctx)
    rules += _deny_rules(root.items.get("deny"), None, ctx)
    obligations = [dict(BASE_OBLIGATION), *_obligations(root.items.get("obligations"), ctx)]
    document: dict[str, JsonValue] = {
        "schema": POLICY_SCHEMA,
        "id": INSTALLATION_LAYER,
        "layer": INSTALLATION_LAYER,
        "projects": list(projects),
        "rules": rules,
        "obligations": obligations,
    }
    if roles:
        document["roles"] = roles
    return document


def _roles(node: YamlNode | None, ctx: _Context) -> dict[str, JsonValue]:
    """The declared roles, ``{name: {description}}``; sets ``ctx.known_roles``."""
    declared: dict[str, JsonValue] = {}
    for name, item in sorted((_map(node, "roles", ctx) or {}).items()):
        path = f"roles.{name}"
        if not is_valid_role_name(name):
            ctx.add(path, "is not a role name (lowercase letters, digits and _, up to 64)")
        entry = _map(item, path, ctx, required=True)
        if entry is None:
            continue
        _unknown_keys(item, frozenset({"description"}), path, ctx)
        description = _text(entry.get("description"), f"{path}.description", ctx)
        if description is not None and not description.strip():
            ctx.add(f"{path}.description", "must not be empty")
        elif description is not None and is_valid_role_name(name):
            declared[name] = {"description": description}
    ctx.known_roles = frozenset(declared)
    return declared


def _grant(node: YamlNode, path: str, number: int, ctx: _Context) -> list[JsonValue]:
    entry = _map(node, path, ctx, required=True)
    if entry is None:
        return []
    _unknown_keys(
        node,
        frozenset(
            {"principal", "entrypoint", "graphs", "level", "writes", "reads", "markings", "catalog"}
        ),
        path,
        ctx,
    )
    entrypoints = _entrypoints(entry, path, ctx)
    principal: dict[str, JsonValue] = {"entrypoint": entrypoints}
    if "principal" in entry:
        found = _principal(entry["principal"], f"{path}.principal", ctx, _GRANT_PRINCIPAL_KEYS)
        principal.update(found or {})
        served = "server" in (entrypoints if isinstance(entrypoints, list) else [entrypoints])
        if "project" in principal and not served:
            ctx.add(
                f"{path}.entrypoint",
                "a grant to a project needs entrypoint: server (only a token bound to the"
                " project verifies it)",
            )
        if "role" in principal and not served:
            ctx.add(
                f"{path}.entrypoint",
                "a grant to a role needs entrypoint: server (only a server token carries roles)",
            )
    resources = _graph_resources(entry.get("graphs"), f"{path}.graphs", ctx, required=True)
    level = _level(
        entry.get("level"), f"{path}.level", ctx, allowed=("read", "write", CATALOG_LEVEL)
    )
    catalog = _catalog(entry["catalog"], f"{path}.catalog", ctx) if "catalog" in entry else None
    if level == CATALOG_LEVEL or "catalog" in entry:
        _catalog_problems(entry, level, catalog, resources, entrypoints, path, ctx)
    writes = _writes(entry["writes"], f"{path}.writes", ctx) if "writes" in entry else None
    if "writes" in entry:
        if level == "write":
            ctx.add(f"{path}.writes", "needs level: read; level write already allows every write")
        if resources == ["graph:*"]:
            ctx.add(
                f"{path}.graphs",
                'must name graphs, not "*", when writes is given: terms belong to an ontology',
            )
    reads = _reads(entry["reads"], f"{path}.reads", ctx) if "reads" in entry else None
    if "reads" in entry:
        if level == "write":
            ctx.add(f"{path}.reads", "needs level: read; level write already reads every type")
        if resources == ["graph:*"]:
            ctx.add(
                f"{path}.graphs",
                'must name graphs, not "*", when reads is given: types belong to an ontology',
            )
    markings = (
        _markings(entry["markings"], f"{path}.markings", ctx) if "markings" in entry else None
    )
    if "markings" in entry and resources == ["graph:*"]:
        ctx.add(
            f"{path}.graphs",
            'must name graphs, not "*", when markings is given: markings belong to an ontology',
        )
    if reads is not None and writes:
        for name in sorted(writes):
            if not _covered(name, reads):
                ctx.add(
                    f"{path}.writes.{name}",
                    f"{name} is not in reads; a grant cannot write a type it hides",
                )
    if level is None or resources is None:
        return []
    named = resources != ["graph:*"]
    graphs = (
        sorted({ctx.graphs[n] for n in _texts(entry["graphs"], "", ctx) or ()}) if named else []
    )
    limited = bool(reads) and named
    rules: list[JsonValue] = [
        {
            "id": f"grant.{number}",
            "effect": "allow",
            "principal": principal,
            "action": list(
                CATALOG_BASE_ACTIONS if level == CATALOG_LEVEL else level_actions(level)
            ),
            # A glob would cover every type resource, whose visibility ``reads`` decides.
            "resource": [f"graph:{g}" for g in graphs] if limited else resources,
        }
    ]
    if reads and limited:
        _check_reads(reads, graphs, f"{path}.reads", ctx)
        rules.append(_reads_rule(number, principal, graphs, reads))
    if catalog and named:
        rules += _catalog_rules(number, principal, graphs, catalog)
    if markings and named:
        _check_markings(markings, graphs, ctx)
        rules.append(_markings_rule(number, principal, graphs, markings))
    if writes and named:
        _check_writes(writes, graphs, reads, frozenset(markings or ()), f"{path}.writes", ctx)
        rules += _write_rules(number, principal, graphs, writes)
    return rules


def _catalog(node: YamlNode, path: str, ctx: _Context) -> _Catalog | None:
    """The ``catalog`` of a grant: per kind (``functions``, ``views``, ``actions``) the entry
    names, sorted and without duplicates; a kind without names is left out."""
    entry = _map(node, path, ctx)
    if entry is None:
        return None
    _unknown_keys(node, frozenset(CATALOG_KINDS), path, ctx)
    found: _Catalog = {}
    valid = True
    for kind in CATALOG_KINDS:
        if kind not in entry:
            continue
        names = _texts(entry[kind], f"{path}.{kind}", ctx)
        if names is None:
            valid = False
            continue
        for index, name in enumerate(names):
            if not ENTRY_NAME_RE.match(name):
                ctx.add(
                    f"{path}.{kind}[{index}]",
                    f"{name!r} is not an entry name (lowercase letters, digits and _, 2 to 63"
                    " characters, starting with a letter)",
                )
                valid = False
        found[kind] = tuple(sorted(set(names)))
    if not entry:
        ctx.add(path, "must name at least one function, action or view")
        valid = False
    return found if valid else None


def _catalog_problems(
    entry: Mapping[str, YamlNode],
    level: str | None,
    catalog: _Catalog | None,
    resources: list[str] | None,
    entrypoints: JsonValue,
    path: str,
    ctx: _Context,
) -> None:
    """What a ``catalog`` key and ``level: catalog`` need and refuse: where each may stand
    (see the module documentation) and the keys they require."""
    served = entrypoints == "server"
    if level == CATALOG_LEVEL:
        if "catalog" not in entry:
            ctx.add(
                f"{path}.catalog",
                "is required for level: catalog: name the functions, actions and views the"
                " grant runs",
            )
        if "reads" not in entry:
            ctx.add(
                f"{path}.reads",
                "is required for level: catalog: name the types its entries may read, so that a"
                " later release's new types never widen the grant",
            )
        if "writes" in entry:
            ctx.add(
                f"{path}.writes",
                "level: catalog takes no writes; an action writes through its own entry"
                " (catalog.actions)",
            )
    if "catalog" in entry and not served:
        ctx.add(
            f"{path}.entrypoint",
            "a grant with catalog needs entrypoint: server (locally an agent with a shell reads"
            " the files, so the limit would only pretend)",
        )
    if "catalog" in entry and resources == ["graph:*"]:
        ctx.add(
            f"{path}.graphs",
            'must name graphs, not "*", when catalog is given: entries belong to a graph',
        )
    if level == "write" and "catalog" in entry:
        ctx.add(
            f"{path}.catalog",
            "needs level: catalog or read; level write already runs every function, view"
            " and action",
        )
    if level == "read" and catalog:
        for kind in ("functions", "views"):
            if kind in catalog:
                ctx.add(
                    f"{path}.catalog.{kind}",
                    f"is redundant with level: read, which already runs every {kind[:-1]}; a"
                    " read grant lists actions only",
                )


def _catalog_rules(
    number: int,
    principal: Mapping[str, JsonValue],
    graphs: Sequence[GraphId],
    catalog: _Catalog,
) -> list[JsonValue]:
    """``grant.<n>.functions``, ``.views`` and ``.actions`` (in this order, a kind without
    names has no rule): the kind's action on the entry resource of every name in every named
    graph, sorted."""
    rules: list[JsonValue] = []
    for kind in CATALOG_KINDS:
        names = catalog.get(kind)
        if not names:
            continue
        entry_kind, action = _CATALOG_ENTRIES[kind]
        rules.append(
            {
                "id": f"grant.{number}.{kind}",
                "effect": "allow",
                "principal": dict(principal),
                "action": [action],
                "resource": sorted(
                    f"graph:{g}/{entry_kind}:{name}" for g in graphs for name in names
                ),
            }
        )
    return rules


def _reads(node: YamlNode, path: str, ctx: _Context) -> tuple[str, ...] | None:
    """The ``reads`` of a grant: type CURIEs or ``prefix:*`` globs, in the order written."""
    names = _texts(node, path, ctx)
    if names is None:
        return None
    valid = True
    for index, name in enumerate(names):
        if not TYPE_GLOB_RE.match(name):
            ctx.add(f"{path}[{index}]", f"{name!r} is not a type CURIE or prefix:*")
            valid = False
    return tuple(names) if valid else None


def _covered(name: str, reads: Sequence[str]) -> bool:
    """Whether a read entry names type ``name``: itself or a ``prefix:*`` that matches it."""
    return any(r == name or (r.endswith("*") and name.startswith(r[:-1])) for r in reads)


def _check_reads(reads: Sequence[str], graphs: Sequence[GraphId], path: str, ctx: _Context) -> None:
    """The model checks of ``reads`` against every graph the grant names: an abstract or
    extended type is a problem (a rule on the exact type would hide the subclasses), a type
    or prefix the model lacks is collected."""
    for graph in graphs:
        model = ctx.model_of(graph)
        if model is None:
            continue
        for index, name in enumerate(reads):
            if name.endswith("*"):
                if not any(t.startswith(name[:-1]) for t in model.types):
                    ctx.unknown_read_types.add(UnknownReadType(graph, name))
                continue
            found = model.types.get(name)
            if found is None:
                ctx.unknown_read_types.add(UnknownReadType(graph, name))
            elif found.extended:
                ctx.add(
                    f"{path}[{index}]",
                    f"{name} is abstract or has subclasses in graph {ctx.graph_name(graph)}; "
                    "name the concrete types",
                )


def _reads_rule(
    number: int, principal: Mapping[str, JsonValue], graphs: Sequence[GraphId], reads: Sequence[str]
) -> dict[str, JsonValue]:
    """``grant.<n>.reads``: ``read.get`` on every named type of every named graph."""
    return {
        "id": f"grant.{number}.reads",
        "effect": "allow",
        "principal": dict(principal),
        "action": ["read.get"],
        "resource": sorted({f"graph:{g}/type:{name}" for g in graphs for name in reads}),
    }


def _markings(node: YamlNode, path: str, ctx: _Context) -> tuple[str, ...] | None:
    """The ``markings`` of a grant: marking CURIEs, sorted and without duplicates."""
    names = _texts(node, path, ctx)
    if names is None:
        return None
    valid = True
    for index, name in enumerate(names):
        if not MARKING_RE.match(name):
            ctx.add(f"{path}[{index}]", f"{name!r} is not a marking CURIE (prefix:name)")
            valid = False
    return tuple(sorted(set(names))) if valid else None


def _check_markings(markings: Sequence[str], graphs: Sequence[GraphId], ctx: _Context) -> None:
    """The model check of ``markings``: a marking the graph's model does not declare is
    collected (a later release may declare it)."""
    for graph in graphs:
        model = ctx.model_of(graph)
        if model is None:
            continue
        for name in markings:
            if name not in model.markings:
                ctx.unknown_markings.add(UnknownMarking(graph, name))


def _markings_rule(
    number: int,
    principal: Mapping[str, JsonValue],
    graphs: Sequence[GraphId],
    markings: Sequence[str],
) -> dict[str, JsonValue]:
    """``grant.<n>.markings``: ``read.get`` on the resource of every named marking of every
    named graph."""
    resources = {marking_resource(g, name) for g in graphs for name in markings}
    return {
        "id": f"grant.{number}.markings",
        "effect": "allow",
        "principal": dict(principal),
        "action": ["read.get"],
        "resource": sorted(r for r in resources if r is not None),
    }


def _writes(node: YamlNode, path: str, ctx: _Context) -> _Writes | None:
    """The ``writes`` of a grant: type -> the verbs it grants (``false`` verbs dropped)."""
    entry = _map(node, path, ctx)
    if entry is None:
        return None
    if not entry:
        ctx.add(path, "must name at least one type")
        return None
    found: _Writes = {}
    for name, item in sorted(entry.items()):
        where = f"{path}.{name}"
        if not TYPE_GLOB_RE.match(name):
            ctx.add(where, f"{name!r} is not a type CURIE")
        elif name.endswith("*"):
            ctx.add(where, f"{name!r} is a prefix; name concrete types")
        else:
            verbs = _type_writes(item, where, ctx)
            if verbs:
                found[name] = verbs
    return found


def _type_writes(node: YamlNode, path: str, ctx: _Context) -> dict[str, _Terms] | None:
    entry = _map(node, path, ctx, required=True)
    if entry is None:
        return None
    before = len(ctx.problems)
    _unknown_keys(node, frozenset(WRITE_VERBS), path, ctx)
    granted: dict[str, _Terms] = {}
    for verb in WRITE_VERBS:
        if verb in entry:
            terms = _verb_terms(verb, entry[verb], f"{path}.{verb}", ctx)
            if terms is not None:
                granted[verb] = terms
    if not granted and len(ctx.problems) == before:
        ctx.add(path, f"grants no verb; name at least one of {', '.join(WRITE_VERBS)}")
    return granted


def _verb_terms(verb: str, node: YamlNode, path: str, ctx: _Context) -> _Terms | None:
    """The terms a verb grants: ``()`` for ``true`` (every term), ``None`` when it is
    ``false`` or malformed."""
    names = _LIST_VERBS.get(verb)
    if isinstance(node, YamlScalar) and node.as_bool() is not None:
        return () if node.as_bool() else None
    if names is None:
        ctx.add(path, "must be true or false")
        return None
    if not isinstance(node, YamlSeq) or not node.items:
        ctx.add(path, f"must be true, false or a non-empty list of {names}")
        return None
    pattern = CURIE_RE if verb in ("link", "unlink") else NAME_RE
    terms: set[str] = set()
    valid = True
    for index, item in enumerate(node.items):
        text = _scalar(item)
        if text is None or not pattern.match(text.text):
            ctx.add(f"{path}[{index}]", f"must be one of the {names}")
            valid = False
        else:
            terms.add(text.text)
    return tuple(sorted(terms)) if valid else None


def _check_writes(
    writes: _Writes,
    graphs: Sequence[GraphId],
    reads: Sequence[str] | None,
    held: frozenset[str],
    path: str,
    ctx: _Context,
) -> None:
    """The model checks of ``writes`` (W8) against every graph the grant names; ``reads`` is
    the grant's own allow list (``None``: it reads every type), ``held`` its own markings."""
    for graph in graphs:
        model = ctx.model_of(graph)
        if model is None:
            continue
        for name, verbs in sorted(writes.items()):
            found = model.types.get(name)
            if found is None:
                ctx.unknown_write_types.add(UnknownWriteType(graph, name))
            elif found.extended:
                ctx.add(
                    f"{path}.{name}",
                    f"{name} is abstract or has subclasses in graph {ctx.graph_name(graph)}; "
                    "name the concrete types",
                )
            else:
                _check_terms(graph, name, found, verbs, held, ctx)
                if reads is not None and "create" in verbs:
                    _check_hidden_ranges(graph, name, found, reads, ctx)


def _check_terms(
    graph: GraphId,
    name: str,
    found: TypeTerms,
    verbs: Mapping[str, _Terms],
    held: frozenset[str],
    ctx: _Context,
) -> None:
    """Terms the type does not have, and verbs no write can satisfy."""

    def unknown(term: str, kind: str) -> None:
        ctx.unknown_terms.add(UnknownTerm(graph, name, term, kind))

    for verb, terms in sorted(verbs.items()):
        for term in terms:
            if verb == "transition":
                if term not in found.attributes or not found.attributes[term].lifecycle:
                    unknown(term, "lifecycle")
            elif verb in ("link", "unlink"):
                if term not in found.relations:
                    unknown(term, "relation")
            elif term not in found.attributes:
                unknown(term, "attribute")
    # An update never touches a lifecycle attribute; a create without ``true`` must name
    # every attribute it has to give (those it names are never missing).
    locked = tuple(
        t
        for t in verbs.get("update", ())
        if t in found.attributes and found.attributes[t].lifecycle
    )
    if locked:
        ctx.unusable_writes.add(UnusableWrite(graph, name, "update", locked, found.file))
    listed = verbs.get("create", ())
    missing = tuple(
        sorted(a for a, t in found.attributes.items() if t.must_give and a not in listed)
    )
    if listed and missing:
        ctx.unusable_writes.add(UnusableWrite(graph, name, "create", missing, found.file))
    _check_marked(graph, name, found, verbs, held, ctx)


def _check_marked(
    graph: GraphId,
    name: str,
    found: TypeTerms,
    verbs: Mapping[str, _Terms],
    held: frozenset[str],
    ctx: _Context,
) -> None:
    """Write rights over marked attributes the grant does not hold the markings of (field
    visibility plan F3). A caller must see an attribute to write it, so such an entry is dead
    for a caller with only this grant. ``create`` and ``update`` name attributes (``true``
    covers every one of the type; an update leaves the lifecycle ones out, and those carry no
    marking); a create of a required marked attribute without a default can never succeed."""
    for verb in ("create", "update"):
        if verb not in verbs:
            continue
        covered = verbs[verb] or tuple(found.attributes)
        lacking = {
            a: tuple(m for m in found.attributes[a].markings if m not in held)
            for a in covered
            if a in found.attributes
        }
        lacking = {a: m for a, m in lacking.items() if m}
        blocking = {a for a in lacking if verb == "create" and found.attributes[a].must_give}
        for names, blocks in ((blocking, True), (set(lacking) - blocking, False)):
            if names:
                missing = tuple(sorted({m for a in names for m in lacking[a]}))
                ctx.unusable_marked_writes.add(
                    UnusableMarkedWrite(graph, name, verb, tuple(sorted(names)), missing, blocks)
                )


def _check_hidden_ranges(
    graph: GraphId, name: str, found: TypeTerms, reads: Sequence[str], ctx: _Context
) -> None:
    """A create of ``name`` needs an edge for each required relation; when the grant's own
    ``reads`` hide every concrete type of a relation's range, a caller with only this grant
    sees no target and every create is refused as conflicting with data it cannot see."""
    for relation, targets in sorted(found.required_ranges.items()):
        if not any(_covered(target, reads) for target in targets):
            ctx.hidden_range_creates.add(HiddenRangeCreate(graph, name, relation, targets))


def _write_rules(
    number: int, principal: Mapping[str, JsonValue], graphs: Sequence[GraphId], writes: _Writes
) -> list[JsonValue]:
    """``grant.<n>.<verb>`` per granted verb, in the order of ``WRITE_VERBS`` (``edge`` right
    after ``link``). A resource is the type, plus the terms of the verb below it."""
    rules: list[JsonValue] = []
    for verb in WRITE_VERBS:
        on: set[str] = set()
        edges: set[str] = set()
        for graph in graphs:
            for name, verbs in writes.items():
                if verb not in verbs:
                    continue
                base = f"graph:{graph}/type:{name}"
                kind = "attr" if verb in _ATTRIBUTE_VERBS else "rel"
                if verb not in _LIST_VERBS:
                    below: list[str] = []
                elif verbs[verb]:
                    below = [f"{base}/{kind}:{term}" for term in verbs[verb]]
                else:
                    below = [f"{base}/{kind}:*"]
                on |= {base, *below}
                if verb == "link":
                    edges |= set(below)
        allowed = [(f"grant.{number}.{verb}", f"write.{verb}", on)]
        if verb == "link":
            allowed.append((f"grant.{number}.edge", "write.create", edges))
        for rule_id, action, resources in allowed:
            if resources:
                rules.append(
                    {
                        "id": rule_id,
                        "effect": "allow",
                        "principal": dict(principal),
                        "action": [action],
                        "resource": sorted(resources),
                    }
                )
    return rules


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


def _principal(
    node: YamlNode, path: str, ctx: _Context, keys: frozenset[str] = _PRINCIPAL_KEYS
) -> dict[str, JsonValue] | None:
    entry = _map(node, path, ctx, required=True)
    if entry is None:
        return None
    _unknown_keys(node, keys, path, ctx)
    found: dict[str, JsonValue] = {}
    for key, facet in (
        ("name", "principal_name"),
        ("id", "principal_id"),
        ("project", "project"),
        ("role", "role"),
    ):
        if key in entry and key in keys:
            value = _text(entry[key], f"{path}.{key}", ctx)
            if value is not None and key == "project" and not is_valid_asserted_value(value):
                ctx.add(f"{path}.project", "is not a valid project name")
            elif value is not None and key == "role":
                if _role_named(value, f"{path}.role", ctx):
                    found[facet] = value
            elif value is not None:
                found[facet] = value
    if not entry:
        ctx.add(path, "names a principal by " + " or ".join(sorted(keys)))
    return found


def _scope(node: YamlNode, path: str, ctx: _Context, *, allowed: tuple[str, ...]) -> dict[str, str]:
    entry = _map(node, path, ctx, required=True) or {}
    levels: dict[str, str] = {}
    for name, value in sorted(entry.items()):
        if name != ALL and name not in ctx.graphs:
            ctx.add(f"{path}.{name}", "is not a graph of this installation")
            continue
        level = _level(value, f"{path}.{name}", ctx, allowed=allowed)
        if level == MAINTAIN_LEVEL and name == ALL:
            ctx.add(f"{path}.{name}", "maintain names graphs one by one, never every graph")
        elif level is not None:
            levels[name] = level
    return levels


def _maintain_rule(project: str, levels: Mapping[str, str], ctx: _Context) -> list[JsonValue]:
    """The allow rule of a scope's ``maintain`` graphs: the graph-level maintenance
    actions for the maintenance entrypoint acting for the (verified) project."""
    graphs = sorted(_graph_glob(ctx.graphs[n]) for n, lv in levels.items() if lv == MAINTAIN_LEVEL)
    if not graphs:
        return []
    return [
        {
            "id": f"scope.{project}:maintain",  # ":" is in no graph name
            "effect": "allow",
            "principal": {"entrypoint": "maint", "project": project},
            "action": list(PROJECT_ADMIN_ACTIONS),
            "resource": graphs,
        }
    ]


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
        if level is not None:
            types[name] = level
            if level == "read":
                rules.append(_deny(f"type.{name}", ("write.*",), [_type_glob_of(graph, name)], {}))
            elif level == "none":
                # Hidden from the callers of the app and the server; a maintainer is not hit.
                rules.append(
                    _deny(
                        f"type.{name}",
                        ("read.get", "write.*"),
                        [_type_glob_of(graph, name)],
                        {"entrypoint": list(GRANT_ENTRYPOINTS)},
                    )
                )
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
    if graph is not None:
        allowed |= {"attribute", "relation"}
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
                if values is not None and key == "role":
                    values = [
                        v
                        for v in values
                        if _role_named(v, f"{path}.role", ctx, graph_layer=graph is not None)
                    ]
                if values:
                    principal[key] = sorted(set(values))
        if graph is not None:
            types = _texts(entry["type"], f"{path}.type", ctx) if "type" in entry else None
            if (
                types is not None
                and actions is not None
                and not all(map(is_type_action_glob, actions))
            ):
                ctx.add(
                    f"{path}.action",
                    "a deny rule on types names write actions (not write.action), read.get or"
                    " read.*",
                )
                actions = None
            names = (
                None
                if types is None
                else sorted({t for t in types if _type_glob(t, f"{path}.type", ctx)})
            )
            terms = [key for key in ("attribute", "relation") if key in entry]
            if terms:
                resources, actions = _term_resources(entry, terms, names, actions, path, graph, ctx)
            else:
                resources = (
                    [_graph_glob(graph)]
                    if names is None
                    else [_type_glob_of(graph, t) for t in names]
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


def _term_resources(
    entry: Mapping[str, YamlNode],
    keys: Sequence[str],
    types: list[str] | None,
    actions: list[str] | None,
    path: str,
    graph: GraphId,
    ctx: _Context,
) -> tuple[list[str], list[str] | None]:
    """The resources and actions of a deny rule with ``attribute`` or ``relation`` (W6):
    the terms of every listed type, for the write actions that can decide them. A
    ``write.link`` on relations also denies ``write.create``, which adds inline edges."""
    if len(keys) > 1:
        ctx.add(path, "names attribute or relation, not both")
        return [], actions
    key = keys[0]
    where = f"{path}.{key}"
    if types is None:
        ctx.add(where, "needs type: an attribute or relation belongs to a type")
    for name in types or ():
        if name.endswith("*"):
            ctx.add(f"{path}.type", f"{name!r} is a prefix; name concrete types with {key}")
    pattern, label = (
        (NAME_RE, "an attribute name") if key == "attribute" else (CURIE_RE, "a relation CURIE")
    )
    terms: list[str] = []
    for term in _texts(entry[key], where, ctx) or ():
        if pattern.match(term):
            terms.append(term)
        else:
            ctx.add(where, f"{term!r} is not {label}")
    allowed = _ATTRIBUTE_ACTIONS if key == "attribute" else _RELATION_ACTIONS
    refused = [a for a in actions or () if a != "write.*" and a not in allowed]
    if refused:
        ctx.add(
            f"{path}.action",
            f"{refused[0]!r} never decides {'an' if key == 'attribute' else 'a'} {key}; "
            f"name {', '.join(sorted(allowed))} or write.*",
        )
        actions = None
    elif actions is not None and key == "relation" and "write.link" in actions:
        actions = sorted({*actions, "write.create"})
    concrete = [t for t in types or () if not t.endswith("*")]
    model = ctx.model_of(graph)
    if model is not None:
        for name in concrete:
            found = model.types.get(name)  # an unknown type is reported with the limits
            if found is not None:
                known = found.attributes if key == "attribute" else found.relations
                for term in terms:
                    if term not in known:
                        ctx.unknown_terms.add(UnknownTerm(graph, name, term, key))
    resources = [f"graph:{graph}/type:{t}/{_KIND[key]}:{term}" for t in concrete for term in terms]
    return sorted(set(resources)), actions


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


def _role_named(name: str, path: str, ctx: _Context, *, graph_layer: bool = False) -> bool:
    """Whether a rule may name role ``name``. The installation layer names declared roles
    only; a graph layer's unknown role is collected for a warning, not refused."""
    if not is_valid_role_name(name):
        ctx.add(path, f"{name!r} is not a role name (lowercase letters, digits and _)")
        return False
    known = ctx.known_roles
    if not graph_layer:
        if known is None or name not in known:
            ctx.add(path, f"role {name!r} is not declared under roles")
            return False
    elif known is not None and name not in known:
        ctx.unknown_roles.add(name)
    return True


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
