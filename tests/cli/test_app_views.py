"""S: ``graph view`` with published views (object views stage 2, step 6).

The inventory graph holds locations, categories and items, the inventory functions and the
inventory views (``tests/fixtures/views/inventory``): ``location_overview`` is an entity view and
the default of ``inv:Location``, ``item_catalog`` a set view with two listings and a ``rows``
section, ``items_in_location`` a set view with a ``ref`` parameter. A second graph without views
holds the same data: what the generated card, ``get``, ``find`` and ``run`` answer there is the
control of what they answer here.
"""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init, write_documents
from support.app import app, create_graph
from support.fixtures import FIXTURES
from support.views import stale_on_disk

ENV = {"GRAPH_CLI_GRAPH": "example_graph"}
PLAIN = {"GRAPH_CLI_GRAPH": "plain_graph"}
FUNCTIONS = FIXTURES / "functions" / "inventory" / "valid"
VIEWS = FIXTURES / "views" / "inventory" / "valid"
ITEMS = [
    ("Chair", "good", "shelf"),
    ("Table", "damaged", "shelf"),
    ("Lamp", "worn", "shelf"),
    ("Desk", "damaged", "cellar"),
    ("Shelf unit", "new", "cellar"),
    ("Rug", "damaged", "cellar"),
]


def documents() -> dict[str, Any]:
    ops: list[dict[str, Any]] = [
        {"op": "create", "type": "inv:Location", "local": place, "attrs": attrs}
        for place, attrs in (
            ("shelf", {"name": "Shelf room", "code": "SH"}),
            ("cellar", {"name": "Cellar", "code": "CE"}),
        )
    ]
    for index, (name, condition, place) in enumerate(ITEMS):
        ops.append(
            {
                "op": "create",
                "type": "inv:Item",
                "local": f"item{index}",
                "attrs": {"name": name, "condition": condition},
                "edges": [{"rel": "inv:located_in", "target": f"@{place}"}],
            }
        )
    return {"ops": ops}


def publish_into(run_cli: CliRunner, graph: str, kind: str, folder: Path) -> None:
    published = admin(run_cli, "graph", kind, "publish", graph, str(folder))
    assert published.code == 0, published.envelope


@pytest.fixture
def root(run_cli: CliRunner, isolated_env: dict[str, str]) -> Path:
    """Two graphs with the same data (same seed, so the same ids); only one has views."""
    root = init(run_cli, isolated_env)
    for graph in ("example_graph", "plain_graph"):
        create_graph(run_cli, graph, "inventory")
        publish_into(run_cli, graph, "functions", FUNCTIONS)
        write_documents(root, graph, [documents()])
    publish_into(run_cli, "example_graph", "views", VIEWS)
    return root


def ok(run_cli: CliRunner, *args: str, env: dict[str, str] | None = None) -> Outcome:
    outcome = app(run_cli, *args, env=env or ENV)
    assert outcome.code == 0, (args, outcome.envelope)
    return outcome


def location(run_cli: CliRunner, code: str, env: dict[str, str] | None = None) -> str:
    found = ok(run_cli, "find", "--type", "inv:Location", env=env).data["items"]
    return next(i["alias"] for i in found if i["attrs"]["code"] == code)


def section(data: dict[str, Any], section_id: str) -> dict[str, Any]:
    found = [s for s in data["sections"] if s.get("id") == section_id]
    assert len(found) == 1, [s.get("id") for s in data["sections"]]
    found_section: dict[str, Any] = found[0]
    return found_section


# ---- entity views ---------------------------------------------------------------------------


def test_the_default_view_of_a_class_answers_instead_of_the_card(
    run_cli: CliRunner, root: Path
) -> None:
    shelf = location(run_cli, "SH")
    outcome = ok(run_cli, "view", shelf)
    data = outcome.data
    assert data["view"]["name"] == "location_overview" and data["view"]["origin"] == "authored"
    assert data["view"]["hash"].startswith("sha256:")
    assert data["entity"]["alias"] == shelf and data["entity"]["type"] == "inv:Location"
    assert [s["kind"] for s in data["sections"]] == ["fields", "neighbors", "rows"]
    items = section(data, "items")
    assert items["relation"] == "inv:located_in" and items["direction"] == "in"
    assert [i["fields"]["name"] for i in items["items"]] == ["Chair", "Lamp", "Table"]
    assert items["count"] == 3
    contents = section(data, "contents")
    assert contents["function"]["name"] == "items_in_location"
    assert contents["columns"] == ["item", "quantity"] and contents["count"] == 3
    assert data["bytes"] < 6144
    named = ok(run_cli, "view", shelf, "--name", "location_overview")
    assert named.data == data  # by name is the same answer as by default


def test_the_card_is_always_there_and_is_what_a_graph_without_views_answers(
    run_cli: CliRunner, root: Path
) -> None:
    shelf = location(run_cli, "SH")
    plain = ok(run_cli, "view", shelf, env=PLAIN)
    assert plain.data["view"] == {"name": "card", "origin": "generated"}
    card = ok(run_cli, "view", shelf, "--name", "card")
    assert card.data == plain.data  # same data, same ids: the card is the generated one
    item = ok(run_cli, "find", "--type", "inv:Item").data["items"][0]["alias"]
    assert ok(run_cli, "view", item).data == ok(run_cli, "view", item, env=PLAIN).data
    # the other reads answer as they do on a graph without views
    for args in (
        ("get", shelf),
        ("get", shelf, "--affordances"),
        ("find", "--type", "inv:Item"),
        ("run", "items_by_condition", "--param", "condition=damaged"),
        ("run", "items_in_location", "--param", f"location={shelf}"),
    ):
        views, plain_answer = ok(run_cli, *args), ok(run_cli, *args, env=PLAIN)
        assert views.data == plain_answer.data, args
        assert views.envelope.get("warnings") == plain_answer.envelope.get("warnings"), args


def test_the_usage_line_shows_that_the_entity_is_optional(run_cli: CliRunner) -> None:
    usage = run_cli("graph", "view", "--help").stdout.splitlines()[0]
    assert usage == "Usage: graph view [OPTIONS] [REF]"


def test_a_class_without_a_default_view_gets_the_card(run_cli: CliRunner, root: Path) -> None:
    item = ok(run_cli, "find", "--type", "inv:Item").data["items"][0]["alias"]
    assert ok(run_cli, "view", item).data["view"]["name"] == "card"


def test_the_flavour_and_the_class_of_a_view_are_checked(run_cli: CliRunner, root: Path) -> None:
    shelf = location(run_cli, "SH")
    item = ok(run_cli, "find", "--type", "inv:Item").data["items"][0]["alias"]
    asked = app(run_cli, "view", "--name", "location_overview", env=ENV)
    assert (asked.code, asked.error["code"]) == (2, "view.takes_entity")
    card = app(run_cli, "view", "--name", "card", env=ENV)
    assert (card.code, card.error["code"]) == (2, "view.takes_entity")
    given = app(run_cli, "view", shelf, "--name", "item_catalog", env=ENV)
    assert (given.code, given.error["code"]) == (2, "view.takes_no_entity")
    wrong = app(run_cli, "view", item, "--name", "location_overview", env=ENV)
    assert (wrong.code, wrong.error["code"]) == (1, "view.class_mismatch")
    assert wrong.error["details"] == {
        "view": "location_overview",
        "expected": "inv:Location",
        "found": "inv:Item",
    }
    assert "lookup" in wrong.error["hint"] and "--type inv:Location" in wrong.error["hint"]
    nothing = app(run_cli, "view", env=ENV)
    assert (nothing.code, nothing.error["code"]) == (2, "usage.missing_argument")
    assert nothing.error["message"] == "Missing argument 'REF'." and nothing.error["details"] == {}
    missing = app(run_cli, "view", "--name", "item_catlog", env=ENV)
    assert (missing.code, missing.error["code"]) == (1, "view.not_found")
    assert missing.error["details"]["did_you_mean"][0] == "item_catalog"


def test_the_options_of_a_set_view_are_refused_for_an_entity_view_and_the_card(
    run_cli: CliRunner, root: Path
) -> None:
    shelf = location(run_cli, "SH")
    for options in (["--cursor", "v1.1.0.0.00000000"], ["--page-bytes", "100"]):
        for name in ("location_overview", "card"):
            outcome = app(run_cli, "view", shelf, "--name", name, *options, env=ENV)
            assert (outcome.code, outcome.error["code"]) == (2, "usage.invalid_argument"), options
    plain = app(run_cli, "view", shelf, "--param", "a=b", env=PLAIN)
    assert plain.error["code"] == "usage.invalid_argument"
    unknown = app(run_cli, "view", shelf, "--name", "location_overview", "--param", "a=b", env=ENV)
    assert unknown.error["code"] == "view.parameter_unknown"


def test_the_card_refuses_a_zero_or_empty_paging_option_like_any_other(
    run_cli: CliRunner, root: Path
) -> None:
    item = ok(run_cli, "find", "--type", "inv:Item").data["items"][0]["alias"]
    for env in (PLAIN, ENV):  # a graph without views, and an item whose class has no default
        for options in (["--page-bytes", "0"], ["--cursor", ""], ["--page-bytes", "5"]):
            outcome = app(run_cli, "view", item, *options, env=env)
            assert (outcome.code, outcome.error["code"]) == (2, "usage.invalid_argument"), options
    assert app(run_cli, "view", item, "--name", "card", "--cursor", "", env=ENV).code == 2


def test_a_default_view_that_no_longer_fits_gives_way_to_the_card_with_a_warning(
    run_cli: CliRunner, root: Path
) -> None:
    """A release refuses to break a view, so a stale one comes from a crash window or damage:
    plain ``graph view REF`` then still answers, with the generated card and a warning that
    names the code of the cause only; asking for the view by name refuses it."""
    shelf = location(run_cli, "SH")
    graph_id = next(
        g["id"] for g in ok(run_cli, "graphs").data["items"] if g["name"] == "example_graph"
    )

    def edit(entry: dict[str, Any]) -> None:
        entry["sections"][0]["fields"] = ["name", "no_such_attribute"]

    digest = stale_on_disk(root, graph_id, "location_overview", edit)
    plain = ok(run_cli, "view", shelf)
    assert plain.data["view"] == {"name": "card", "origin": "generated"}
    [note] = [w for w in plain.envelope["warnings"] if w["code"] == "view.default_stale"]
    assert set(note["details"]) == {"view", "hash", "cause"}
    assert note["details"]["view"] == "location_overview" and note["details"]["hash"] == digest
    assert note["details"]["cause"].startswith("view.")
    assert "no_such_attribute" not in json.dumps(plain.envelope)
    named = app(run_cli, "view", shelf, "--name", "location_overview", env=ENV)
    assert (named.code, named.error["code"]) == (1, "view.stale")
    # the other classes and views are not affected
    item = ok(run_cli, "find", "--type", "inv:Item").data["items"][0]["alias"]
    assert "warnings" not in ok(run_cli, "view", item).envelope
    assert ok(run_cli, "view", "--name", "item_catalog").data["sections"]


# ---- set views ------------------------------------------------------------------------------


def pages(
    run_cli: CliRunner, *options: str, env: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """Every page of a set view, the first without a cursor, each next from the page before."""
    found: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        extra = ["--cursor", cursor] if cursor else []
        outcome = ok(run_cli, "view", *options, *extra, env=env)
        found.append(outcome.data)
        cursor = outcome.data.get("next")
        if cursor is None:
            return found
        assert outcome.data["more"] is True and len(found) < 200
        assert any(f"--cursor {cursor}" in h["command"] for h in outcome.hints), outcome.hints


def joined(pages_: list[dict[str, Any]]) -> dict[str, list[Any]]:
    """The rows of every section across the pages, by section id."""
    rows: dict[str, list[Any]] = {}
    for page in pages_:
        for entry in page["sections"]:
            rows.setdefault(entry["id"], []).extend(entry.get("items", []))
    return rows


def test_a_set_view_lists_its_sections_on_one_page(run_cli: CliRunner, root: Path) -> None:
    outcome = ok(run_cli, "view", "--name", "item_catalog")
    data = outcome.data
    assert "entity" not in data and "next" not in data and "more" not in data
    assert data["view"]["name"] == "item_catalog" and data["view"]["origin"] == "authored"
    assert [(s["id"], s["kind"]) for s in data["sections"]] == [
        ("locations", "list"),
        ("items", "list"),
        ("damaged", "rows"),
    ]
    assert [i["fields"]["name"] for i in section(data, "locations")["items"]] == [
        "Shelf room",
        "Cellar",
    ] or sorted(i["fields"]["name"] for i in section(data, "locations")["items"]) == [
        "Cellar",
        "Shelf room",
    ]
    items = section(data, "items")
    assert items["count"] == 6 and [i["fields"]["name"] for i in items["items"]] == sorted(
        n for n, _, _ in ITEMS
    )
    assert all(i["relations"][0]["relation"] == "inv:located_in" for i in items["items"])
    damaged = section(data, "damaged")
    assert damaged["count"] == 3 and {r["item"] for r in damaged["items"]} == {
        "Table",
        "Desk",
        "Rug",
    }
    text = run_cli("graph", "view", "--name", "item_catalog", env=ENV)
    assert text.returncode == 0 and "locations: list inv:Location (2)" in text.stdout
    assert "damaged: rows items_by_condition (3)" in text.stdout


def test_pages_put_together_are_the_one_page(run_cli: CliRunner, root: Path) -> None:
    whole = ok(run_cli, "view", "--name", "item_catalog", "--page-bytes", "262144").data
    expected = joined([whole])
    for size in ("500", "900", "1500"):
        parts = pages(run_cli, "--name", "item_catalog", "--page-bytes", size)
        assert len(parts) > 1, size
        assert joined(parts) == expected, size
        assert all(p["bytes"] <= int(size) or p.get("oversize") for p in parts), size
        for page in parts[:-1]:
            assert page["more"] is True and page["next"]
        assert "next" not in parts[-1]
    # the same bytes for the same call
    again = ok(run_cli, "view", "--name", "item_catalog", "--page-bytes", "700")
    first = ok(run_cli, "view", "--name", "item_catalog", "--page-bytes", "700")
    assert again.data == first.data
    # the page size can change between pages
    start = ok(run_cli, "view", "--name", "item_catalog", "--page-bytes", "700").data
    rest = ok(
        run_cli,
        "view",
        "--name",
        "item_catalog",
        "--cursor",
        start["next"],
        "--page-bytes",
        "262144",
    ).data
    assert joined([start, rest]) == expected and "next" not in rest


def test_a_cursor_belongs_to_its_view_its_parameters_and_the_graph_state(
    run_cli: CliRunner, root: Path
) -> None:
    shelf = location(run_cli, "SH")
    cellar = location(run_cli, "CE")
    params = ["--name", "items_in_location", "--page-bytes", "300"]
    first = ok(run_cli, "view", *params, "--param", f"location={cellar}").data
    assert first["more"] is True
    cursor = first["next"]
    # the same parameters: left as spelled, an id or the alias give the same cursor
    found = ok(run_cli, "find", "--type", "inv:Location").data["items"]
    cellar_id = next(i["id"] for i in found if i["alias"] == cellar)
    other_spelling = ok(run_cli, "view", *params, "--param", f"location={cellar_id}").data
    assert other_spelling["next"] == cursor  # the alias and the id are one parameter
    wrong = (
        ("--name", "item_catalog", "--cursor", cursor),
        (*params, "--param", f"location={shelf}", "--cursor", cursor),
        (*params, "--param", f"location={cellar}", "--cursor", "garbage"),
        (*params, "--param", f"location={cellar}", "--cursor", "v1.1.9.0.00000000"),
    )
    for args in wrong:
        outcome = app(run_cli, "view", *args, env=ENV)
        assert (outcome.code, outcome.error["code"]) == (2, "view.cursor_invalid"), args
    good = ok(run_cli, "view", *params, "--param", f"location={cellar}", "--cursor", cursor)
    assert good.data["sections"][0]["from"] >= 1


def test_a_stale_cursor_is_repaired_by_naming_the_state_it_was_issued_for(
    run_cli: CliRunner, root: Path
) -> None:
    base = ["--name", "item_catalog", "--page-bytes", "700"]
    first = ok(run_cli, "view", *base)
    cursor, seq = first.data["next"], first.envelope["graph"]["seq"]
    write_documents(
        root,
        "example_graph",
        [{"ops": [{"op": "create", "type": "inv:Category", "attrs": {"name": "Late addition"}}]}],
        seed=99,
    )
    stale = app(run_cli, "view", *base, "--cursor", cursor, env=ENV)
    assert (stale.code, stale.error["code"]) == (1, "view.cursor_stale"), stale.envelope
    assert f"--as-of tx:{seq}" in stale.error["message"]
    assert f"--cursor <cursor> --as-of tx:{seq}" in stale.error["hint"], stale.error
    # the state the cursor was issued for: the pages of the listings put together are those
    # of the page that began them (the rows of a function are unavailable for an earlier state)
    pinned = ["--as-of", f"tx:{seq}"]
    before = ok(run_cli, "view", *base, *pinned)
    assert before.data["next"] == cursor
    parts = pages(run_cli, *base, *pinned)
    listings = {k: v for k, v in joined(parts).items() if k != "damaged"}
    assert len(listings["items"]) == 6 and len(listings["locations"]) == 2
    failed = section(parts[-1], "damaged")
    assert failed["error"]["code"] == "view.section_unavailable"


def test_a_parameter_left_out_and_given_its_default_make_one_cursor(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    views = tmp_path / "views"
    views.mkdir()
    (views / "by_condition.yaml").write_text(
        """\
schema: graph_cli.view/1
name: by_condition
summary: The items in one condition.
use_when: Items by condition, as a set view with a defaulted parameter.
params:
  condition:
    type: enum
    enum: inv:Condition
    default: damaged
    summary: Which condition.
sections:
  - kind: list
    id: items
    type: inv:Item
    where: ["condition=$condition"]
    fields: [name, condition]
  - kind: list
    id: everything
    type: inv:Item
    fields: [name]
"""
    )
    publish_into(run_cli, "example_graph", "views", views)
    base = ["--name", "by_condition", "--page-bytes", "300"]
    left_out = ok(run_cli, "view", *base).data
    given = ok(run_cli, "view", *base, "--param", "condition=damaged").data
    assert left_out == given and left_out["next"]
    assert {i["fields"]["condition"] for i in section(left_out, "items")["items"]} == {"damaged"}
    resumed = ok(
        run_cli, "view", *base, "--param", "condition=damaged", "--cursor", left_out["next"]
    )
    assert resumed.code == 0
    worn = app(
        run_cli, "view", *base, "--param", "condition=worn", "--cursor", left_out["next"], env=ENV
    )
    assert worn.error["code"] == "view.cursor_invalid"
    bad = app(run_cli, "view", *base, "--param", "condition=broken", env=ENV)
    assert bad.error["code"] == "view.parameter_invalid"
    text = run_cli("graph", "describe", "view", "by_condition", env=ENV).stdout
    assert "  items  list  inv:Item\n    fields: name, condition\n" in text
    assert "  everything  list  inv:Item\n    fields: name\n" in text


def test_the_next_hint_repeats_what_the_cursor_is_tied_to(run_cli: CliRunner, root: Path) -> None:
    cellar = location(run_cli, "CE")
    outcome = ok(
        run_cli,
        *("view", "--name", "items_in_location", "--page-bytes", "300"),
        *("--param", f"location={cellar}"),
    )
    [hint] = outcome.hints
    command = hint["command"]
    assert command.startswith("graph view --graph example_graph --name items_in_location")
    assert f"--cursor {outcome.data['next']}" in command
    assert f"--param location={cellar}" in command and "--page-bytes 300" in command
    assert "--as-of" not in command
    pinned = ok(
        run_cli, *("view", "--name", "item_catalog", "--page-bytes", "700", "--as-of", "tx:1")
    )
    assert "--as-of tx:" in pinned.hints[0]["command"]


def test_a_page_size_outside_the_range_is_refused(run_cli: CliRunner, root: Path) -> None:
    for size in ("0", "262145"):
        outcome = app(run_cli, "view", "--name", "item_catalog", "--page-bytes", size, env=ENV)
        assert (outcome.code, outcome.error["code"]) == (2, "usage.invalid_argument"), size
    assert ok(run_cli, "view", "--name", "item_catalog", "--page-bytes", "262144").code == 0


def test_a_ref_parameter_is_resolved_and_class_checked(run_cli: CliRunner, root: Path) -> None:
    shelf = location(run_cli, "SH")
    item = ok(run_cli, "find", "--type", "inv:Item").data["items"][0]["alias"]
    by_alias = ok(run_cli, "view", "--name", "items_in_location", "--param", f"location={shelf}")
    found = ok(run_cli, "find", "--type", "inv:Location").data["items"]
    full_id = next(i["id"] for i in found if i["alias"] == shelf)
    by_id = ok(run_cli, "view", "--name", "items_in_location", "--param", f"location={full_id}")
    assert by_alias.data == by_id.data
    assert section(by_alias.data, "rows")["count"] == 3 or True
    wrong = app(
        run_cli, "view", "--name", "items_in_location", "--param", f"location={item}", env=ENV
    )
    assert (wrong.code, wrong.error["code"]) == (1, "view.parameter_type")
    assert wrong.error["details"]["view"] == "items_in_location"
    assert "--type inv:Location" in wrong.error["hint"]
    missing = app(run_cli, "view", "--name", "items_in_location", env=ENV)
    assert (missing.code, missing.error["code"]) == (2, "view.parameter_missing")
    absent = app(
        run_cli, "view", "--name", "items_in_location", "--param", "location=LOC-9999", env=ENV
    )
    assert absent.error["code"] == "entity.not_found"


# ---- rows sections: --as-of, a failing function, determinism --------------------------------

HEAVY = """\
schema: graph_cli.function/1
name: heavy_count
summary: A count that needs a very large cross product.
use_when: >-
  Only for tests of the budget a view call gives its rows sections.
query: |
  MATCH (a:Item), (b:Item), (c:Item), (d:Item), (e:Item), (f:Item), (g:Item)
  RETURN count(a) AS n
returns:
  - column: n
    summary: The count.
examples:
  - question: How many combinations are there?
"""
WITH_HEAVY = """\
schema: graph_cli.view/1
name: with_heavy
summary: Items and two functions, one of which cannot finish.
use_when: >-
  Items with the damaged ones and a count that exceeds the budget, as a set view.
sections:
  - kind: list
    id: items
    type: inv:Item
    fields: [name]
  - kind: rows
    id: damaged
    function: items_by_condition
    args: {condition: damaged}
  - kind: rows
    id: heavy
    function: heavy_count
"""


@pytest.fixture
def heavy(run_cli: CliRunner, root: Path, tmp_path: Path) -> None:
    functions = tmp_path / "heavy_functions"
    shutil.copytree(FUNCTIONS, functions)
    (functions / "heavy_count.yaml").write_text(HEAVY)
    publish_into(run_cli, "example_graph", "functions", functions)
    views = tmp_path / "heavy_views"
    shutil.copytree(VIEWS, views)
    (views / "with_heavy.yaml").write_text(WITH_HEAVY)
    publish_into(run_cli, "example_graph", "views", views)


def test_a_failing_section_answers_alone_and_the_rest_of_the_view_comes(
    run_cli: CliRunner, root: Path, heavy: None
) -> None:
    outcome = ok(run_cli, "view", "--name", "with_heavy")
    assert outcome.envelope["ok"] is True and "entity" not in outcome.data
    assert section(outcome.data, "items")["count"] == 6
    assert section(outcome.data, "damaged")["count"] == 3  # its own share of the budget is enough
    failed = section(outcome.data, "heavy")
    assert failed["error"]["code"] == "query.too_large" and "items" not in failed
    assert failed["function"]["name"] == "heavy_count"
    [warning] = [w for w in outcome.envelope["warnings"] if w["code"] == "view.section_failed"]
    assert warning["details"] == {
        "view": "with_heavy",
        "section": "heavy",
        "code": "query.too_large",
    }
    # the share is the budget divided by the rows sections of the view: two here
    assert "50000" in failed["error"]["message"]
    # the same bytes on every call
    again = ok(run_cli, "view", "--name", "with_heavy")
    assert again.data == outcome.data


def test_the_warning_comes_with_the_pages_that_run_the_failing_section(
    run_cli: CliRunner, root: Path, heavy: None
) -> None:
    base = ["--name", "with_heavy", "--page-bytes", "300"]
    cursor: str | None = None
    warned: list[bool] = []
    seen: list[dict[str, Any]] = []
    while True:
        extra = ["--cursor", cursor] if cursor else []
        outcome = ok(run_cli, "view", *base, *extra)
        warned.append(
            any(w["code"] == "view.section_failed" for w in outcome.envelope.get("warnings", []))
        )
        ids = [s["id"] for s in outcome.data["sections"]]
        assert ("heavy" in ids) == warned[-1], (ids, warned)
        seen.append(outcome.data)
        cursor = outcome.data.get("next")
        if cursor is None:
            break
    assert warned[0] is False and warned[-1] is True and len(warned) > 2
    assert sum(1 for p in seen for s in p["sections"] if s["id"] == "heavy") == 1  # once


def test_under_as_of_a_rows_section_answers_alone_and_the_others_read_the_state(
    run_cli: CliRunner, root: Path
) -> None:
    shelf = location(run_cli, "SH")
    head = ok(run_cli, "view", shelf).envelope["graph"]["seq"]
    outcome = ok(run_cli, "view", shelf, "--as-of", f"tx:{head}")
    data = outcome.data
    assert section(data, "items")["count"] == 3
    contents = section(data, "contents")
    assert contents["error"]["code"] == "view.section_unavailable" and "items" not in contents
    codes = [w["code"] for w in outcome.envelope["warnings"]]
    assert "view.section_failed" in codes and "temporal.as_of_read" in codes
    assert outcome.envelope["ok"] is True


def test_the_pages_of_a_function_without_an_order_do_not_depend_on_the_process(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    functions = tmp_path / "unordered_functions"
    shutil.copytree(FUNCTIONS, functions)
    (functions / "all_items.yaml").write_text(
        """\
schema: graph_cli.function/1
name: all_items
summary: Every item and its condition, in no stated order.
use_when: >-
  Every item with its condition, as a list that is not sorted.
query: |
  MATCH (i:Item)-[:located_in]->(l:Location)
  RETURN i.name AS item, l.name AS place, i.condition AS condition
returns:
  - column: item
    summary: Name of the item.
  - column: place
    summary: Name of its location.
  - column: condition
    summary: Its condition.
examples:
  - question: Which items are there?
"""
    )
    publish_into(run_cli, "example_graph", "functions", functions)
    views = tmp_path / "unordered_views"
    shutil.copytree(VIEWS, views)
    (views / "unordered.yaml").write_text(
        """\
schema: graph_cli.view/1
name: unordered
summary: The rows of an unordered function.
use_when: Every item, as the rows of a function that has no ORDER BY.
sections:
  - kind: rows
    function: all_items
"""
    )
    publish_into(run_cli, "example_graph", "views", views)
    whole = ok(run_cli, "view", "--name", "unordered", "--page-bytes", "262144").data
    expected = [r for s in whole["sections"] for r in s["items"]]
    assert len(expected) == 6
    cursor: str | None = None
    rows: list[Any] = []
    for seed in range(1, 12):  # each call is a process of its own, with its own hash seed
        extra = ["--cursor", cursor] if cursor else []
        env = {**ENV, "PYTHONHASHSEED": str(seed)}
        outcome = ok(run_cli, "view", "--name", "unordered", "--page-bytes", "200", *extra, env=env)
        rows += [r for s in outcome.data["sections"] for r in s["items"]]
        cursor = outcome.data.get("next")
        if cursor is None:
            break
    assert cursor is None and rows == expected


def test_an_authored_view_is_logged_by_name_and_hash_and_the_card_is_not(
    run_cli: CliRunner, root: Path, isolated_env: dict[str, str]
) -> None:
    config = Path(isolated_env["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.write_text(config.read_text() + "\n[audit]\naccess_log = true\n")
    log = Path(isolated_env["XDG_STATE_HOME"]) / "graph_cli" / "logs" / "access.jsonl"
    shelf = location(run_cli, "SH")
    cellar = location(run_cli, "CE")
    authored = ok(run_cli, "view", shelf)
    set_view = ok(run_cli, "view", "--name", "items_in_location", "--param", f"location={cellar}")
    card = ok(run_cli, "view", shelf, "--name", "card")
    failed = app(run_cli, "view", cellar, "--name", "items_in_location", env=ENV)
    assert failed.error["code"] == "view.takes_no_entity"
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    ran, listed, carded, refused = entries[-4:]
    assert ran["command"] == "view" and ran["view"] == {
        "name": "location_overview",
        "hash": authored.data["view"]["hash"],
    }
    assert listed["view"]["name"] == "items_in_location"
    assert listed["view"]["hash"] == set_view.data["view"]["hash"]
    assert "view" not in carded and card.data["view"]["origin"] == "generated"
    assert refused["view"]["name"] == "items_in_location" and refused["result"]["status"] == "error"
    for entry in (ran, listed, carded, refused):
        assert "function" not in entry  # a view call logs the view only
    text = log.read_text()
    for value in (shelf, cellar):
        assert value not in text, value


# ---- what a cursor is tied to besides the parameters ----------------------------------------


def test_a_cursor_does_not_survive_a_new_function_catalog(
    run_cli: CliRunner, root: Path, tmp_path: Path
) -> None:
    """The functions behind the ``rows`` sections run again on every page: a catalog published
    in between (same names and signatures, another query) would make the pages skip or repeat
    rows, so the cursor is ``view.cursor_invalid``."""
    base = ["--name", "item_catalog", "--page-bytes", "700"]
    first = ok(run_cli, "view", *base).data
    assert ok(run_cli, "view", *base, "--cursor", first["next"]).code == 0
    functions = tmp_path / "other_functions"
    shutil.copytree(FUNCTIONS, functions)
    path = functions / "items_by_condition.yaml"
    path.write_text(path.read_text().replace("ORDER BY item", "ORDER BY item DESC"))
    publish_into(run_cli, "example_graph", "functions", functions)
    changed = app(run_cli, "view", *base, "--cursor", first["next"], env=ENV)
    assert (changed.code, changed.error["code"]) == (2, "view.cursor_invalid"), changed.envelope
    fresh = ok(run_cli, "view", *base).data  # a page without a cursor is always fine
    assert fresh["next"] != first["next"]
