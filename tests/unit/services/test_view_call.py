"""The storage-free parts of a ``graph view`` call: the default view of a class, the key a page
cursor is tied to, the share of the query budget a ``rows`` section gets, the lazy results."""

from decimal import Decimal

import pytest
from support import views
from support.views import company_model

from graph_cli.kinetic.view_page import cursor_key
from graph_cli.kinetic.view_rows import RowsResult
from graph_cli.query.cypher.execute import DEFAULT_BUDGET
from graph_cli.services.view_call import (
    ROWS_BUDGET,
    LazyRows,
    default_view,
    page_key,
    rows_share,
)

MODEL = company_model()


def test_the_rows_budget_is_the_engines_own() -> None:
    assert ROWS_BUDGET == DEFAULT_BUDGET


def test_a_rows_section_gets_an_even_share_of_the_budget() -> None:
    assert rows_share(0) == rows_share(1) == ROWS_BUDGET
    assert rows_share(2) == ROWS_BUDGET // 2
    assert rows_share(32) * 32 <= ROWS_BUDGET
    assert rows_share(10**9) == 1  # never zero: a section always may start


def entries() -> list[object]:
    return list(views.compiled_entries().values())


def test_the_default_view_is_that_of_the_nearest_class_on_the_chain() -> None:
    found = views.compiled_entries()
    unit = found["unit_overview"]  # takes the abstract org:OrgUnit, default
    candidates = [unit, found["position_overview"], found["legacy_unit_overview"]]
    assert default_view(candidates, MODEL, "org:Department") is unit  # a subclass of OrgUnit
    assert default_view(candidates, MODEL, "org:Team") is unit
    assert default_view(candidates, MODEL, "org:Position") is None  # no default of its own
    assert default_view([], MODEL, "org:Team") is None
    assert default_view(candidates, MODEL, "no:Such") is None
    deeper = views.compiled_entries(
        {
            "team_default.yaml": views.valid_sources()["unit_overview.yaml"]
            .replace("name: unit_overview", "name: team_default")
            .replace("takes: org:OrgUnit", "takes: org:Team")
        }
    )["team_default"]
    assert default_view([unit, deeper], MODEL, "org:Team") is deeper  # the most specific class
    assert default_view([unit, deeper], MODEL, "org:Department") is unit


def test_a_deprecated_view_is_never_a_default() -> None:
    found = views.compiled_entries()
    legacy = found["legacy_unit_overview"]
    assert legacy.deprecated and not legacy.defn.default
    assert default_view([legacy], MODEL, "org:Team") is None


def key(**changes: object) -> str:
    given: dict[str, object] = {
        "view_hash": "sha256:v",
        "functions_hash": "sha256:f",
        "pin_hash": "sha256:p",
        "reader": "r",
        "bound": {"a": "x", "b": 1},
        "names": ["a", "b"],
    }
    return page_key(**{**given, **changes})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "change",
    [
        {"view_hash": "sha256:other"},
        {"functions_hash": "sha256:other"},
        {"functions_hash": None},
        {"pin_hash": "sha256:other"},
        {"reader": "other"},
        {"reader": None},
        {"bound": {"a": "y", "b": 1}},
    ],
)
def test_a_cursor_key_changes_with_everything_that_changes_the_pages(
    change: dict[str, object],
) -> None:
    assert key(**change) != key()


def test_a_cursor_key_is_one_function_of_the_bound_values() -> None:
    assert key() == key()
    assert len(key()) == 8
    # a default given or left out is one bound value; a decimal is its text
    assert key(bound={"a": Decimal("1.50"), "b": 1}) == key(bound={"a": "1.50", "b": 1})
    # only the declared parameters count
    assert key(bound={"a": "x", "b": 1, "ignored": 9}) == key()
    assert key() == cursor_key("sha256:v|sha256:f|sha256:p|r", {"a": "x", "b": 1})


def test_lazy_rows_run_a_section_once_and_only_when_asked() -> None:
    calls: list[str] = []

    def run(section: str) -> RowsResult:
        calls.append(section)
        return RowsResult(section)

    rows = LazyRows(["a", "b"], run)
    assert calls == [] and len(rows) == 2 and list(rows) == ["a", "b"]
    assert rows["b"].function == "b" and rows["b"] is rows["b"] and rows.get("b") is rows["b"]
    assert calls == ["b"]
    assert rows.get("missing") is None
    with pytest.raises(KeyError):
        rows["missing"]
    assert calls == ["b"]
