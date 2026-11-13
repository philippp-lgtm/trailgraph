"""Which terms a policy decides, and the any-of rule for an action's needs (write allow
lists plan W7, W9).

``FineTypes`` caches ``fine_types`` per (policy hash, graph): the types some rule names terms
of, the only ones whose attribute and relation decisions are made and recorded.

``need_decisions`` joins one need of ``kinetic.action_needs`` with the policy. A need is met
when one of its options is: the write action allowed on ``graph:<id>/type:<T>`` and, for a
type in ``fine_types``, every fine term of the option allowed on its resource. For a policy
without term rules and a need on a concrete type without subclasses that is the decision the
declared type got before. The listing of actions (``ReadService.visible_actions``) and the
authorization of an action whose expansion is empty (``WriteService``) both use it:

- listed: every need met;
- empty expansion: the decisions of the first option (sorted by type) that is met; when none
  is, those of the first option, so a denial names a real resource. A need whose declared
  class has no concrete subtype is decided on the declared class, as before.
"""

from __future__ import annotations

from dataclasses import dataclass

from graph_cli.access_control.authorize import authorize
from graph_cli.access_control.documents import fine_types
from graph_cli.access_control.terms import term_resource
from graph_cli.kernel.actor import ActorContext
from graph_cli.kernel.authz import AuthzContext, Decision
from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.kinetic.action_needs import FineNeed, NeedOptions


class FineTypes:
    """``fine_types`` of a policy set for a graph, computed once per policy hash."""

    def __init__(self) -> None:
        self._found: dict[tuple[str, GraphId], frozenset[str]] = {}

    def of(self, policy: PolicySet, graph: GraphId) -> frozenset[str]:
        key = (policy.hash, graph)
        found = self._found.get(key)
        if found is None:
            found = self._found[key] = fine_types(policy.documents, graph)
        return found


@dataclass(frozen=True, slots=True)
class NeedResult:
    decisions: tuple[Decision, ...]
    met: bool
    type: str | None  # the type the decisions were made on, and the fine terms of that option
    terms: tuple[FineNeed, ...] = ()


def _option_decisions(
    actor: ActorContext,
    graph: GraphId,
    action: str,
    type_curie: str,
    terms: tuple[FineNeed, ...],
    ctx: AuthzContext,
    policy: PolicySet,
    fine: frozenset[str],
) -> tuple[Decision, ...]:
    """The decision on the type and, when ``type_curie`` is in ``fine``, on each fine term."""
    decisions = [authorize(actor, action, f"graph:{graph}/type:{type_curie}", ctx, policy)]
    if type_curie in fine:
        for term_action, kind, term in terms:
            resource = term_resource(graph, type_curie, kind, term)
            if resource is not None:
                decisions.append(authorize(actor, term_action, resource, ctx, policy))
    return tuple(decisions)


def need_decisions(
    actor: ActorContext,
    graph: GraphId,
    need: NeedOptions,
    ctx: AuthzContext,
    policy: PolicySet,
    fine: frozenset[str],
) -> NeedResult:
    """The decisions that decide ``need`` and whether it is met (see the module docstring)."""
    if not need.options:
        if need.type is None:
            return NeedResult((), True, None)
        decision = authorize(actor, need.action, f"graph:{graph}/type:{need.type}", ctx, policy)
        return NeedResult((decision,), decision.effect == "allow", need.type)
    first: NeedResult | None = None
    for type_curie, terms in need.options:
        decisions = _option_decisions(
            actor, graph, need.action, type_curie, terms, ctx, policy, fine
        )
        met = all(d.effect == "allow" for d in decisions)
        found = NeedResult(decisions, met, type_curie, terms)
        if met:
            return found
        first = first or found
    assert first is not None
    return first
