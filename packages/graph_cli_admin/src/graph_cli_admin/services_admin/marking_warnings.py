"""What the installed policy and the model of a release say about markings (field visibility
plan F2): the policy-side warnings of ``policies_affected``, which runs for ``ontology release``,
``graph push`` and ``ontology diff --graph``.

A marking is declared by the ontology and granted by the installation policy (``markings`` of a
grant, compiled to ``marking:<graph>/<curie>`` rules). A marked attribute is hidden from every
caller whose grants do not name its markings, so a release and the policy can disagree in four
ways, each a warning (none refuses: a policy may name a marking a later release declares, and a
maintainer may mean to show an attribute to nobody):

- ``admin.policy_unknown_marking``: an installed grant names a marking the release does not
  declare;
- ``admin.marking_ungranted``: the release uses a marking no installed grant names, so only
  maintainer surfaces show those attributes (with the built-in policy, which names no marking,
  no app caller reads or writes them);
- ``admin.marking_blocks_create``: a required attribute without a default is marked, so every
  caller without the marking loses the create of its type;
- ``admin.marking_in_expression``: a rule, a lifecycle guard or a precondition of a published
  action names a marked attribute (an ``attr`` node or the attribute of a ``sum``). Rules are
  judged on the whole graph, so their refusals are a covert channel for callers without the
  marking, which repeated dry runs can amplify.

Pure over the installed policy, the release's model and, for preconditions, the published
action catalog (only its stored form is read; the actions a ``graph push`` is about to replace
are not looked at).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Final

from graph_cli.access_control.policy import BUILTIN_POLICY
from graph_cli.access_control.terms import named_markings
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.json_access import JsonShapeError
from graph_cli.kernel.ports.catalog import GraphEntry, KineticSlot
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.kinetic.action_entries import load_action_entries
from graph_cli.ontology_layer.expr_ast import walk
from graph_cli.ontology_layer.model import CompiledModel

MAX_LISTED: Final = 20


def warnings(
    policy: PolicySet,
    entry: GraphEntry,
    model: CompiledModel,
    actions: KineticSlot | None = None,
) -> tuple[WarningRecord, ...]:
    """The marking warnings of releasing ``model`` for ``entry`` under ``policy``: unknown
    markings, ungranted ones, required marked attributes, marked attributes in expressions;
    each group sorted. ``actions`` is the published action catalog (``None``: not checked)."""
    granted = named_markings(policy.documents, entry.id)
    declared = _declared(model)
    found: list[WarningRecord] = []
    found += _unknown(granted, model, entry)
    found += _ungranted(declared, granted, policy, entry)
    found += _blocks_create(model, entry)
    found += _in_expression(model, actions, entry)
    return tuple(found)


def _declared(model: CompiledModel) -> dict[str, list[str]]:
    """The markings the model uses, each with the attributes that carry it as
    ``<declaring class>.<attribute>`` (one per declaration, not per subclass), sorted."""
    used: dict[str, list[str]] = {}
    for curie, cls in model.classes.items():
        for name, attribute in cls.attributes.items():
            if attribute.declared_in == curie:
                for marking in attribute.markings:
                    used.setdefault(marking, []).append(f"{curie}.{name}")
    return {m: sorted(a) for m, a in sorted(used.items())}


def _unknown(
    granted: Iterable[str], model: CompiledModel, entry: GraphEntry
) -> list[WarningRecord]:
    version = model.bundle_version
    return [
        WarningRecord(
            "admin.policy_unknown_marking",
            f"release {version} does not declare marking {marking} in graph {entry.name}, which"
            " an installed grant names; the grant shows nobody anything until a release declares"
            " it",
            {"graph": entry.name, "marking": marking},
        )
        for marking in granted
        if marking not in model.markings
    ]


def _ungranted(
    declared: Mapping[str, list[str]],
    granted: Iterable[str],
    policy: PolicySet,
    entry: GraphEntry,
) -> list[WarningRecord]:
    named = set(granted)
    builtin = len(policy.documents) > 0 and policy.documents[0] == BUILTIN_POLICY
    found: list[WarningRecord] = []
    for marking, attributes in declared.items():
        if marking in named:
            continue
        effect = (
            "no installation policy is installed, so no app caller can read or write them (the"
            " built-in policy grants no marking); only maintainer surfaces show them"
            if builtin
            else "only maintainer surfaces show them until a grant of the installation policy"
            " names the marking"
        )
        shown = ", ".join(attributes[:5]) + (", ..." if len(attributes) > 5 else "")
        found.append(
            WarningRecord(
                "admin.marking_ungranted",
                f"marking {marking} is used in graph {entry.name} ({shown}) and granted by no"
                f" installed grant: {effect}",
                {
                    "graph": entry.name,
                    "marking": marking,
                    "count": len(attributes),
                    "attributes": list[JsonValue](attributes[:MAX_LISTED]),
                },
            )
        )
    return found


def _blocks_create(model: CompiledModel, entry: GraphEntry) -> list[WarningRecord]:
    found: list[WarningRecord] = []
    for curie, cls in sorted(model.classes.items()):
        for name, attribute in sorted(cls.attributes.items()):
            if (
                attribute.declared_in == curie
                and attribute.markings
                and attribute.required
                and attribute.default is None
                and attribute.kind != "lifecycle"
            ):
                found.append(
                    WarningRecord(
                        "admin.marking_blocks_create",
                        f"{curie}.{name} of graph {entry.name} is required, has no default and is"
                        f" marked {', '.join(attribute.markings)}: a caller without the marking"
                        f" cannot see it and so cannot create {curie}; give it a default or leave"
                        " it unmarked",
                        {
                            "graph": entry.name,
                            "type": curie,
                            "attribute": name,
                            "markings": list(attribute.markings),
                        },
                    )
                )
    return found


@dataclass(frozen=True, slots=True)
class _Site:
    """An expression and the classes whose entities it is evaluated on."""

    kind: str  # rule | guard | precondition
    name: str
    classes: tuple[str, ...]
    expr: JsonValue


def _sites(model: CompiledModel, actions: KineticSlot | None) -> Iterator[_Site]:
    for rule in model.rules:
        yield _Site("rule", rule.id, model.subtypes(rule.target), rule.expr)
    for lifecycle, definition in model.lifecycles.items():
        # the classes that have an attribute moved by this lifecycle
        classes = tuple(
            sorted(
                curie
                for curie, cls in model.classes.items()
                if any(
                    a.kind == "lifecycle" and a.type == lifecycle for a in cls.attributes.values()
                )
            )
        )
        for transition in definition.transitions:
            for guard in transition.guards:
                yield _Site("guard", f"{lifecycle}.{guard.id}", classes, guard.expr)
    if actions is None:
        return
    try:
        entries = load_action_entries(actions.compiled)
    except (JsonShapeError, GraphCliError):
        return  # a damaged catalog is reported where it is published
    for action in entries:
        for pre in action.preconditions:
            param = next((p for p in action.defn.params if p.name == pre.on), None)
            of_class = None if param is None else param.of_class
            classes = tuple(
                sorted(
                    {
                        sub
                        for curie in model.classes
                        if curie == of_class or curie.partition(":")[2] == of_class
                        for sub in model.subtypes(curie)
                    }
                )
            )
            yield _Site("precondition", f"{action.name}.{pre.id}", classes, pre.expr)


def _sum_classes(model: CompiledModel, spec: Mapping[str, JsonValue]) -> tuple[str, ...]:
    """The classes whose entities a ``sum`` reads the attribute of: the far end of its
    relation, narrowed by ``where.type`` when that names classes of the model; every class when
    the relation's end is open or unknown."""
    relation = model.relations.get(str(spec.get("rel")))
    end = (
        ()
        if relation is None
        else (relation.domain if spec.get("direction") == "in" else relation.range)
    )
    reachable = {c for curie in end for c in model.subtypes(curie)} or set(model.classes)
    where = spec.get("where")
    types = where.get("type") if isinstance(where, Mapping) else None
    if isinstance(types, list):
        named = {c for t in types for c in model.subtypes(str(t))}
        reachable = (reachable & named) or reachable
    return tuple(sorted(reachable))


def _names(
    model: CompiledModel, classes: Iterable[str], attribute: str
) -> dict[str, tuple[str, ...]]:
    """``<declaring class>.<attribute>`` -> markings, for the marked attributes called
    ``attribute`` on ``classes``."""
    found: dict[str, tuple[str, ...]] = {}
    for curie in classes:
        cls = model.classes.get(curie)
        definition = None if cls is None else cls.attributes.get(attribute)
        if definition is not None and definition.markings:
            found[f"{definition.declared_in}.{attribute}"] = definition.markings
    return found


def _in_expression(
    model: CompiledModel, actions: KineticSlot | None, entry: GraphEntry
) -> list[WarningRecord]:
    found: list[WarningRecord] = []
    for site in _sites(model, actions):
        marked: dict[str, tuple[str, ...]] = {}
        for kind, arg, _ in walk(site.expr):
            if kind == "attr" and isinstance(arg, str):
                marked.update(_names(model, site.classes, arg))
            elif kind == "sum" and isinstance(arg, Mapping) and isinstance(arg.get("attr"), str):
                marked.update(_names(model, _sum_classes(model, arg), str(arg["attr"])))
        if not marked:
            continue
        attributes = sorted(marked)
        markings = sorted({m for found_markings in marked.values() for m in found_markings})
        found.append(
            WarningRecord(
                "admin.marking_in_expression",
                f"{site.kind} {site.name} of graph {entry.name} names marked attribute(s)"
                f" {', '.join(attributes)}: it is judged on the whole graph, so what it refuses"
                " or reports tells callers without the marking about a value they cannot see,"
                " and repeated dry runs can narrow the value down; keep marked attributes out of"
                " rules, guards and preconditions",
                {
                    "graph": entry.name,
                    "kind": site.kind,
                    "name": site.name,
                    "attributes": list[JsonValue](attributes),
                    "markings": list[JsonValue](markings),
                },
            )
        )
    return found
