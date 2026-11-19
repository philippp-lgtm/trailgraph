"""The byte guard of the generated card, pinned at many limits (T17 stage 2: the guard works by
section index and must not move a byte of the card).

Each synthetic scenario is rendered without a limit; the guard then runs at limits from the
untrimmed size (capped) down in 10 % steps, and the golden holds the size and a digest of the
document at each. ``GRAPH_CLI_UPDATE_GOLDENS=1`` rewrites it (only for an intended card change).
"""

import hashlib
import json

from support.card_scenarios import Scenario, scenarios, stages_seen
from support.fixtures import GOLDENS, assert_golden

from graph_cli.kernel.canonical import pretty_bytes
from graph_cli.kinetic import card
from graph_cli.kinetic.card import dumps, generated_spec, render

CAP = 12_000
FLOOR = 150


def limits(untrimmed: int) -> list[int]:
    found = [untrimmed]
    limit = min(untrimmed, CAP)
    while limit > FLOOR:
        found.append(limit)
        limit = int(limit * 0.9)
    return found


def pinned(scenario: Scenario) -> dict[str, object]:
    spec = generated_spec(scenario.model, scenario.class_curie)
    spec = spec.__class__(spec.name, spec.origin, spec.sections, 10**9)
    untrimmed = render(
        spec,
        scenario.entity,
        scenario.snapshot,
        scenario.model,
        affordances=scenario.affordances or None,
    )
    assert not untrimmed.cut
    steps: list[list[object]] = []
    for limit in limits(untrimmed.size()):
        guarded = card._guard(untrimmed, limit)
        document = dumps(guarded.to_json())
        steps.append([limit, len(document), hashlib.sha256(document).hexdigest()[:16]])
    return {"untrimmed": untrimmed.size(), "steps": steps}


def test_the_guard_gives_the_same_bytes_at_every_limit() -> None:
    found = {s.name: pinned(s) for s in scenarios()}
    assert_golden(
        GOLDENS / "kinetic" / "card_guard.json", pretty_bytes(json.loads(json.dumps(found)))
    )


def test_the_scenarios_reach_every_stage_of_the_guard() -> None:
    seen: set[str] = set()
    for scenario in scenarios():
        spec = generated_spec(scenario.model, scenario.class_curie)
        spec = spec.__class__(spec.name, spec.origin, spec.sections, 10**9)
        full = render(
            spec,
            scenario.entity,
            scenario.snapshot,
            scenario.model,
            affordances=scenario.affordances or None,
        )
        for limit in limits(full.size()):
            seen |= stages_seen(card._guard(full, limit).to_json())
    assert seen == {
        "key fields kept",
        "key fields dropped",
        "items to one",
        "groups moved",
        "values shortened",
        "fields moved",
        "files cut",
        "citations cut",
    }
