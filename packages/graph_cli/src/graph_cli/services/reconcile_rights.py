"""What a reconcile document declares it may do, as needs of write actions (reconcile plan R-K11).

A scope names its type, the attributes it sets (``fields`` and the key attribute) and the
relations it sets (``edges``). Before the lock, and before anything is planned, the write
service authorizes these declared needs, so a token without the retract right fails on its first
run and not on the day something disappears:

- ``write.create`` and ``write.update`` on the type, with the attributes (a create also with the
  relations: an inline edge is decided as ``write.create``);
- ``write.link`` and ``write.unlink`` with the relations, for a scope that sets any;
- for a complete scope ``write.retract`` (a retract retire) or ``write.transition`` with the
  lifecycle attribute (a transition retire), and ``write.transition`` for a scope that revives.

The needs have the shape of an action's (``kinetic.action_needs``) and are decided by the same
any-of rule: the options of a need are the non-abstract types of the scope's type, and it is met
when one option is allowed. There is no ``write.reconcile`` action: a reconcile can do nothing
its caller could not do with plain ops. Pure: no storage, no policy.
"""

from __future__ import annotations

from collections.abc import Sequence

from graph_cli.kernel.actions import Action
from graph_cli.kernel.changeset import OP_ACTIONS, ReconcileScope
from graph_cli.kinetic.action_needs import FineNeed, NeedOptions
from graph_cli.ontology_layer.model import CompiledModel

_CREATE = str(OP_ACTIONS["create"])
_UPDATE = str(OP_ACTIONS["update"])
_LINK = str(OP_ACTIONS["link"])
_UNLINK = str(OP_ACTIONS["unlink"])
_RETRACT = str(OP_ACTIONS["retract"])
_TRANSITION = str(OP_ACTIONS["transition"])


def reconcile_needs(
    scopes: Sequence[ReconcileScope], model: CompiledModel
) -> tuple[NeedOptions, ...]:
    """The declared needs of ``scopes`` against ``model``, sorted by (action, type); a scope
    whose type the model lacks has none (the planner refuses it as an unknown term)."""
    groups: dict[tuple[str, str], dict[str, set[FineNeed]]] = {}

    def need(
        action: Action | str,
        type_curie: str,
        terms: Sequence[FineNeed] = (),
        *,
        lifecycle: bool = False,
        attr: str | None = None,
    ) -> None:
        by_type = groups.setdefault((str(action), type_curie), {})
        for name in model.subtypes(type_curie):
            if model.classes[name].abstract:
                continue
            found = by_type.setdefault(name, set())
            found.update(terms)
            if lifecycle:
                moved = attr or model.lifecycle_attribute(name)
                if moved is not None:
                    found.add((_TRANSITION, "attr", moved))

    for scope in scopes:
        if scope.type not in model.classes:
            continue
        names = sorted({scope.key, *scope.fields})
        rels = list(scope.edges)
        need(
            _CREATE,
            scope.type,
            [(_CREATE, "attr", n) for n in names] + [(_CREATE, "rel", r) for r in rels],
        )
        need(_UPDATE, scope.type, [(_UPDATE, "attr", n) for n in names])
        if rels:
            need(_LINK, scope.type, [(_LINK, "rel", r) for r in rels])
            need(_UNLINK, scope.type, [(_UNLINK, "rel", r) for r in rels])
        retire = scope.retire
        if scope.complete and retire.transition is None:
            need(_RETRACT, scope.type)
        if (scope.complete and retire.transition is not None) or retire.revive is not None:
            need(_TRANSITION, scope.type, lifecycle=True, attr=retire.attr)
    return tuple(
        NeedOptions(
            action,
            type_curie,
            tuple((name, tuple(sorted(terms))) for name, terms in sorted(by_type.items())),
        )
        for (action, type_curie), by_type in sorted(groups.items())
    )
