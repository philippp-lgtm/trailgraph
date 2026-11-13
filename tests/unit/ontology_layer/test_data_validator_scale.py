"""``check_graph`` over a large graph or overlay stays linear where it used to be quadratic.

Counted, not timed: a timer on a loaded machine proves nothing, while a pass over every
overlay revision per entity, or a candidate list rebuilt per entity of a duplicate group, is a
count that does not depend on the machine. (Measured once with 6,300 entities and 15,000 edges:
about 6 s before, 0.15 s after.)
"""

from dataclasses import replace
from typing import Any

import pytest
from support.modules import compile_modules, header
from support.snapshots import DictSnapshot, edge, eid, revision

from graph_cli.kernel.ids import EntityId
from graph_cli.kernel.records import Revision
from graph_cli.ontology_layer import data_validator
from graph_cli.ontology_layer.data_validator import check_graph
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.temporal_knowledge.working import WorkingState

MODEL = """classes:
  Topic:
    summary: A topic.
    attributes:
      title: {summary: Title., type: string, role: label}
  Task:
    summary: A task.
    attributes:
      title: {summary: Title., type: string, role: label}
relations:
  in_topic:
    summary: The topic of a task.
    domain: Task
    range: Topic
    cardinality: {in: {max: 1000}}
"""
COUNT = 400


def model() -> CompiledModel:
    return compile_modules("example", {"example": header("example", "ex", "1.0.0") + MODEL})


class CountingRevisions(dict[EntityId, Revision]):
    """An overlay's ``revisions`` that counts the passes over its items."""

    passes = 0

    def items(self) -> Any:
        self.passes += 1
        return super().items()


def graph() -> list[Revision]:
    topics = [revision(f"topic{i}", "ex:Topic", {"title": f"Topic {i}"}) for i in range(20)]
    tasks = [
        revision(
            f"task{i}",
            "ex:Task",
            {"title": f"Task {i}"},
            [edge("ex:in_topic", f"topic{i % 20}", name=f"t{i}")],
        )
        for i in range(COUNT)
    ]
    return [*topics, *tasks]


def overlay_of(revisions: list[Revision]) -> tuple[WorkingState, CountingRevisions]:
    """Every entity migrated (rev 2) over a base that holds rev 1."""
    changed = CountingRevisions()
    for r in revisions:
        changed[r.id] = replace(r, rev=2, prev_rev=1)
    state = WorkingState(
        base=DictSnapshot(),
        revisions=changed,
        unchanged={},
        created=frozenset(),
        locals={},
        alias_allocations=(),
        resolution={},
        findings=(),
        effects=(),
    )
    return state, changed


def test_the_overlay_is_passed_over_once_not_once_per_entity() -> None:
    revisions = graph()
    state, changed = overlay_of(revisions)
    # the overlay is the whole graph: every in_topic edge is counted from the overlay
    assert check_graph(model(), state) == []
    assert changed.passes <= 2, changed.passes


def test_a_group_of_equal_names_is_computed_once() -> None:
    """Every task carries one name: each would have listed every other, ``COUNT`` squared."""
    revisions = [
        revision(f"task{i}", "ex:Task", {"title": "Same"}, alias=f"T-{i:04d}") for i in range(COUNT)
    ]
    snapshot = DictSnapshot(*revisions)
    calls = 0
    original = data_validator._matches

    def counting(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(data_validator, "_matches", counting)
        findings = check_graph(model(), snapshot)
    assert calls <= 2, calls
    [finding] = findings  # one warning, on the entity with the smallest id
    assert finding.code == "data.semantic.possible_duplicate"
    assert finding.target["id"] == str(min(r.id for r in revisions))
    assert isinstance(finding.actual, list) and len(finding.actual) == 5  # capped candidates


def test_the_duplicate_report_is_the_same_for_every_group_member_order() -> None:
    """Two groups, the leader is the smallest id of its own group; the others are quiet."""
    revisions = [revision(f"a{i}", "ex:Task", {"title": "Alpha"}) for i in range(3)] + [
        revision(f"b{i}", "ex:Task", {"title": "Beta"}) for i in range(3)
    ]
    findings = check_graph(model(), DictSnapshot(*revisions))
    leaders = {str(f.target["id"]) for f in findings}
    assert leaders == {
        str(min(eid(f"a{i}") for i in range(3))),
        str(min(eid(f"b{i}") for i in range(3))),
    }
