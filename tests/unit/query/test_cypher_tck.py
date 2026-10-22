"""openCypher TCK scenarios against the Cypher subset (plan cypher_query.md 6).

Every scenario of the vendored feature files runs. Three kinds are skipped,
each with its reason in the test report:

- the runner cannot set it up (``support.tck.Skip``: setups beyond plain
  ``CREATE``, properties named like system fields);
- the query uses a construct the subset refuses on purpose (``REFUSED``);
- a documented difference of graph_cli (``DIFFERENCES``). These run as
  strict xfail, so a difference that disappears fails the test.
"""

import pytest
from harness import REPO_ROOT
from support.tck import Mismatch, Scenario, Skip, run, scenarios

FEATURES = REPO_ROOT / "tests" / "fixtures" / "opencypher_tck" / "features"

# query.unsupported constructs refused by design (plan 3.2): a scenario that
# expects rows and gets one of them is skipped with the construct as reason.
REFUSED = (
    "named paths",
    "variable length without an upper bound",
    "(a write clause)",
    "the function ",
    "list comprehensions",
    "pattern comprehensions",
    "list indexing and slicing",
    "the power operator ^",
    "map projections",
    "subqueries",
    "a value next to an aggregate",
)

# Scenario id prefix -> the documented difference that makes it fail.
DIFFERENCES = {
    "Match2: [3]": "type() returns the relation CURIE",
    "Match2: [4]": "type() returns the relation CURIE",
    "MatchWhere1: [7]": "type() returns the relation CURIE",
    "MatchWhere1: [11]": "type() returns the relation CURIE",
    "Comparison4: [1]": "labels() returns class CURIEs",
    "Comparison1: [8]": "no NaN: a decimal division by zero is an error",
    "Comparison2: [5]": "no NaN: a decimal division by zero is an error",
    "WithWhere1: [2]": "WHERE after WITH DISTINCT sees the columns only, like ORDER BY",
}

SCENARIOS = [s for path in sorted(FEATURES.rglob("*.feature")) for s in scenarios(path)]


def _difference(scenario: Scenario) -> str | None:
    return next((why for key, why in DIFFERENCES.items() if scenario.id.startswith(key)), None)


def _refused(mismatch: Mismatch) -> str | None:
    error = mismatch.error
    if error is None or error.code != "query.unsupported":
        return None
    construct = str(error.details["construct"])
    return construct if any(part in construct for part in REFUSED) else None


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.id for s in SCENARIOS])
def test_scenario(scenario: Scenario) -> None:
    difference = _difference(scenario)
    try:
        run(scenario)
    except Skip as exc:
        pytest.skip(str(exc))
    except Mismatch as exc:
        refused = _refused(exc)
        if refused is not None:
            pytest.skip(f"refused by the subset: {refused}")
        if difference is not None:
            pytest.xfail(difference)
        raise
    assert difference is None, f"listed as a difference but passes: {difference}"


def test_the_vendored_files_are_complete() -> None:
    assert len(list(FEATURES.rglob("*.feature"))) == 76
    assert len(SCENARIOS) > 1000


def test_differences_name_existing_scenarios() -> None:
    for key in DIFFERENCES:
        assert any(s.id.startswith(key) for s in SCENARIOS), key
