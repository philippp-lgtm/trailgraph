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

For a graph with type read limits a caller can run an action only through types it sees
(type read limits plan V7): ``need_decisions`` takes the caller's visible set (``None``: no
limit, nothing changes) and skips every option outside it. The set is the V1 decision
``read.get`` on ``graph:<id>/type:<T>`` taken for each concrete type (``services.visibility``),
so membership is that decision without a second ``authorize`` per option and without new
decision records: the decisions of a need stay exactly those of the write rights. A need
without an option the caller sees is not met and carries no decisions.

The same holds for the attributes of an option (field visibility plan F6, defence in depth):
``keeps`` gives the attribute names a type keeps visible for the caller (``ReadView.keeps``;
``None``: no attribute is hidden), and an option whose fine terms name an attribute the
caller does not see is skipped like a type it does not see. The check is made for every
option, whether or not its type has term rules (``fine``), and needs no decision of its own:
the compile check over the caller's model view hides an action that sets or reads a hidden
attribute, this keeps the any-of rule from ever meeting a need through one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from graph_cli.access_control.authorize import authorize, authorize_entry
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
    through: Decision | None = None,
) -> tuple[Decision, ...]:
    """The decision on the type and, when ``type_curie`` is in ``fine``, on each fine term;
    deny-only when ``through`` is the entry decision of an action the caller's grants name."""

    def decide(a: str, resource: str) -> Decision:
        if through is None:
            return authorize(actor, a, resource, ctx, policy)
        return authorize_entry(actor, through, a, resource, ctx, policy)

    decisions = [decide(action, f"graph:{graph}/type:{type_curie}")]
    if type_curie in fine:
        for term_action, kind, term in terms:
            resource = term_resource(graph, type_curie, kind, term)
            if resource is not None:
                decisions.append(decide(term_action, resource))
    return tuple(decisions)


def attribute_hidden(
    keeps: Callable[[str], frozenset[str] | None] | None,
    type_curie: str,
    terms: tuple[FineNeed, ...],
) -> bool:
    """Whether an option names an attribute its type does not keep visible (``keeps``)."""
    if keeps is None:
        return False
    kept = keeps(type_curie)
    return kept is not None and any(kind == "attr" and name not in kept for _, kind, name in terms)


def need_decisions(
    actor: ActorContext,
    graph: GraphId,
    need: NeedOptions,
    ctx: AuthzContext,
    policy: PolicySet,
    fine: frozenset[str],
    visible: frozenset[str] | None = None,
    *,
    keeps: Callable[[str], frozenset[str] | None] | None = None,
    through: Decision | None = None,
) -> NeedResult:
    """The decisions that decide ``need`` and whether it is met (see the module docstring);
    ``through``: deny-only, for an action the caller's grants name."""
    if not need.options:
        if need.type is None:
            return NeedResult((), True, None)
        if visible is not None and need.type not in visible:
            return NeedResult((), False, need.type)
        resource = f"graph:{graph}/type:{need.type}"
        decision = (
            authorize(actor, need.action, resource, ctx, policy)
            if through is None
            else authorize_entry(actor, through, need.action, resource, ctx, policy)
        )
        return NeedResult((decision,), decision.effect == "allow", need.type)
    first: NeedResult | None = None
    for type_curie, terms in need.options:
        if visible is not None and type_curie not in visible:
            continue
        if attribute_hidden(keeps, type_curie, terms):
            continue
        decisions = _option_decisions(
            actor, graph, need.action, type_curie, terms, ctx, policy, fine, through
        )
        met = all(d.effect == "allow" for d in decisions)
        found = NeedResult(decisions, met, type_curie, terms)
        if met:
            return found
        first = first or found
    return first if first is not None else NeedResult((), False, None)
