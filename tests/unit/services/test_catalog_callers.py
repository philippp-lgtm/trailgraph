"""A caller limited to the catalog sees exactly the entries its grants name (catalog callers plan
CA2, step 1): the catalog filter of the classification, its place after the read-view layer and
apart from its cache, the listings, counts, suggestions, search and skill that go through it, and
what stays as it was for a reader (also one with ``catalog.actions``).

Over the company example with its published functions, actions and views. The grants are server
grants of roles: ``support`` is catalog-only, ``scoped`` is a reader with the very read view
``support`` has (so a cache that mixed the two up would show), ``closer`` a reader with
``catalog.actions``, ``reader`` a plain reader of everything.
"""

import dataclasses
from pathlib import Path

import pytest
from support.catalog_leaks import assert_hidden_absent
from support.catalog_policies import Fixed, company_env
from support.services import ServiceEnv
from support.txbuilder import actor

from graph_cli.access_control.compiler import compile_policy
from graph_cli.access_control.policy import policy_set
from graph_cli.app_cli.tool_manifest import heads
from graph_cli.kernel.actor import ActorContext, Assurance, ClientInfo, Entrypoint, Facet
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.records import RequestMeta
from graph_cli.kinetic.entries import find_entry
from graph_cli.services import describe
from graph_cli.services.catalog_access import Denied, allowed_entries, not_allowed
from graph_cli.services.read import ReadService
from graph_cli.services.schema_search import search_model

READS = '["org:*", "ppl:*", "ops:*"]'  # no leg:*: functions that touch it are hidden to these

POLICY = f"""\
schema: graph_cli.policy/1
id: installation
roles:
  support: {{description: "Catalog only"}}
  scoped: {{description: "A reader with the read view of support"}}
  closer: {{description: "A reader that also closes through an action"}}
  reader: {{description: "A reader of everything"}}
  wide: {{description: "Catalog only, naming an entry a type limit hides"}}
  viewer: {{description: "Catalog only, a view and nothing else"}}
grants:
  - entrypoint: server
    principal: {{role: support}}
    graphs: [example_graph]
    level: catalog
    catalog: {{functions: [members_of_unit], actions: [create_task], views: [unit_overview]}}
    reads: {READS}
  - {{entrypoint: server, principal: {{role: scoped}}, graphs: [example_graph], level: read,
     reads: {READS}}}
  - entrypoint: server
    principal: {{role: closer}}
    graphs: [example_graph]
    level: read
    catalog: {{actions: [create_task]}}
  - {{entrypoint: server, principal: {{role: reader}}, graphs: [example_graph], level: read}}
  - entrypoint: server
    principal: {{role: wide}}
    graphs: [example_graph]
    level: catalog
    catalog: {{functions: [members_of_unit, employer_of], views: [members_report]}}
    reads: {READS}
  - entrypoint: server
    principal: {{role: viewer}}
    graphs: [example_graph]
    level: catalog
    catalog: {{views: [org_catalog]}}
    reads: {READS}
no_project: {{"*": write}}
"""

ALL_FUNCTIONS = 9
ALL_ACTIONS = 4
ALL_VIEWS = 6


def token(*roles: str) -> ActorContext:
    base = actor()
    return dataclasses.replace(
        base,
        principal=Facet(Assurance.VERIFIED, id="token:t", name="t", source="token"),
        client=ClientInfo("graph-server", "0.1.0", Entrypoint.SERVER),
        roles=roles,
    )


@pytest.fixture
def env(tmp_path: Path) -> ServiceEnv:
    return company_env(tmp_path / "root")


@pytest.fixture
def read(env: ServiceEnv) -> ReadService:
    document = compile_policy(
        POLICY.encode(), file="i.yaml", graphs={"example_graph": env.entry.id}
    ).document
    return env.make_reader(Fixed(policy_set(document)))


SUPPORT = token("support")


def names(found: object) -> list[str]:
    return sorted(c.entry.name for c in found)  # type: ignore[attr-defined]


def classified(
    env: ServiceEnv, read: ReadService, who: ActorContext
) -> dict[str, tuple[object, ...]]:
    published = read.published_actions(env.entry)
    assert published is not None
    return {
        "functions": read.classified_functions(who, env.entry),
        "actions": read.classified_actions(who, env.entry, published),
        "views": read.classified_views(who, env.entry),
    }


def verdicts(found: object) -> dict[str, tuple[str, ...]]:
    return {c.entry.name: tuple(str(m) for m in c.missing) for c in found}  # type: ignore[attr-defined]


# ---- the filter ------------------------------------------------------------------------


def test_a_catalog_caller_sees_exactly_the_entries_its_grants_name(
    env: ServiceEnv, read: ReadService
) -> None:
    found = classified(env, read, SUPPORT)
    # named entries are classified as before: not allowed until a later step lets them run
    assert verdicts(found["functions"]) == {"members_of_unit": ("read.function on graph",)}
    assert verdicts(found["actions"]) == {"create_task": ("write.create on type:ops:Task",)}
    assert verdicts(found["views"]) == {
        "unit_overview": ("read.get on graph", "read.function on graph")
    }
    for kind in ("functions", "actions", "views"):
        assert allowed_entries(found[kind]) == ()  # type: ignore[arg-type]


def test_a_reader_and_a_reader_with_catalog_actions_see_every_entry(
    env: ServiceEnv, read: ReadService
) -> None:
    for role in ("reader", "scoped", "closer"):
        found = classified(env, read, token(role))
        assert len(found["functions"]) >= (ALL_FUNCTIONS if role != "scoped" else 1)
        assert len(found["views"]) >= (ALL_VIEWS if role != "scoped" else 1)
    everything = classified(env, read, token("reader"))
    assert len(everything["functions"]) == ALL_FUNCTIONS
    assert len(everything["actions"]) == ALL_ACTIONS
    assert len(everything["views"]) == ALL_VIEWS
    # `catalog.actions` on a read grant names an action and filters none: the unnamed ones are
    # classified as before
    closer = classified(env, read, token("closer"))
    assert names(closer["actions"]) == names(everything["actions"])
    assert names(closer["functions"]) == names(everything["functions"])
    assert verdicts(closer["actions"]) == verdicts(everything["actions"])


def test_the_listings_that_return_allowed_entries_return_none_of_the_others(
    env: ServiceEnv, read: ReadService
) -> None:
    assert read.functions(SUPPORT, env.entry) is None  # no read.function on the graph
    assert read.actions(SUPPORT, env.entry) is None
    assert read.views(SUPPORT, env.entry) is None
    # a catalog caller whose grant also lets it read functions sees the named ones only
    callable_ = read.callable_functions(SUPPORT, env.entry, read.read_view(SUPPORT, env.entry))
    assert [f.name for f in callable_ or ()] == ["members_of_unit"]
    everyone = read.callable_functions(
        token("reader"), env.entry, read.read_view(token("reader"), env.entry)
    )
    assert len(everyone or ()) == ALL_FUNCTIONS


def test_a_name_outside_the_catalog_is_unknown_like_a_name_that_does_not_exist(
    env: ServiceEnv, read: ReadService
) -> None:
    view = read.read_view(SUPPORT, env.entry)
    visible = read.callable_functions(SUPPORT, env.entry, view)
    with pytest.raises(GraphCliError) as unnamed:
        find_entry(visible, "vacant_positions")  # published, not granted
    with pytest.raises(GraphCliError) as missing:
        find_entry(visible, "vacant_positionz")  # never published
    assert unnamed.value.code == missing.value.code == "function.not_found"
    for error in (unnamed.value, missing.value):
        assert error.details["did_you_mean"] in ([], ["members_of_unit"])
    assert str(unnamed.value.message).replace("vacant_positions", "x") == str(
        missing.value.message
    ).replace("vacant_positionz", "x")
    # and the name never appears as a suggestion
    with pytest.raises(GraphCliError) as near:
        find_entry(visible, "vacant_positions_of_unit")
    assert_hidden_absent(near.value.details, set(ALL_NAMES) - {"members_of_unit"})


ALL_NAMES = {
    "employer_of",
    "members_of_unit",
    "missing_required_trainings",
    "policies_for_unit",
    "reporting_chain",
    "required_trainings",
    "shareholders_of",
    "training_completions",
    "vacant_positions",
    "assign_position",
    "create_task",
    "hand_over_position",
    "offboard_employee",
    "legacy_unit_overview",
    "members_report",
    "org_catalog",
    "position_overview",
    "tasks_by_state",
    "unit_overview",
}
GRANTED = {"members_of_unit", "create_task", "unit_overview"}


# ---- two layers: the read view first, the catalog after --------------------------------


def test_a_granted_name_the_read_view_hides_stays_hidden(
    env: ServiceEnv, read: ReadService
) -> None:
    """``employer_of`` touches a type outside ``reads``: the hidden layer takes it away
    before the catalog layer could show it, so granting it exposes nothing."""
    wide = token("wide")
    found = classified(env, read, wide)
    assert names(found["functions"]) == ["members_of_unit"]
    assert "employer_of" not in {c.entry.name for c in found["functions"]}  # type: ignore[attr-defined]
    full = classified(env, read, token("scoped"))
    assert "employer_of" not in {c.entry.name for c in full["functions"]}  # type: ignore[attr-defined]
    # whoami shows the names it sees, not the names the policy says
    graph = read.whoami(wide)["permissions"]["graphs"][0]  # type: ignore[index]
    assert graph["catalog"]["functions"] == ["members_of_unit"]  # type: ignore[index]


def test_an_allowed_view_keeps_its_row_functions_without_granting_them(
    env: ServiceEnv, read: ReadService
) -> None:
    """``members_report`` runs ``members_of_unit``, which ``wide`` is granted; ``support``'s
    ``unit_overview`` runs it too. A view whose function the catalog does not name is still
    classified: the read-view-filtered list feeds the view layer, not the catalog layer."""
    only_view = token("wide")
    found = classified(env, read, only_view)
    assert names(found["views"]) == ["members_report"]
    # the function list the views are judged by is not narrowed to the catalog
    rows = {f.name for f in read.view_row_functions(only_view, env.entry)}
    assert {"members_of_unit", "training_completions", "vacant_positions"} <= rows
    assert rows == {f.name for f in read.view_row_functions(token("scoped"), env.entry)}
    # the support caller's function list is the narrowed one
    assert names(classified(env, read, SUPPORT)["functions"]) == ["members_of_unit"]


def test_the_manifest_types_the_rows_of_a_view_without_a_tool_for_the_function(
    env: ServiceEnv, read: ReadService
) -> None:
    owner = env.make_reader()  # the built-in policy: an app caller with every right
    view = next(v for v in owner.views(env.actor, env.entry) or () if v.name == "unit_overview")
    model = owner.visible_model(env.actor, env.entry)
    rows = read.view_row_functions(SUPPORT, env.entry)
    typed = heads(model, (), (), (view,), frozenset(), rows=rows)
    (head,) = typed
    assert head.name == "view_unit_overview"
    columns = [s.columns for s in head.sections if s.function is not None]
    assert columns and all(c is not None for c in columns)
    # without the row functions the sections stay untyped, and no function tool is made for them
    untyped = heads(model, (), (), (view,), frozenset())
    assert [s.columns for s in untyped[0].sections if s.function is not None] == [None]
    assert all(h.kind != "function" for h in typed)


# ---- the cache -------------------------------------------------------------------------


def test_a_catalog_caller_and_a_reader_with_the_same_read_view_share_no_filtered_result(
    env: ServiceEnv, read: ReadService
) -> None:
    scoped, support = token("scoped"), token("support")
    # the premise: one read view, so the hidden layer's cache entry is shared
    assert read.read_view(scoped, env.entry) == read.read_view(support, env.entry)
    assert read.catalog_scope(scoped, env.entry) is None
    assert read.catalog_scope(support, env.entry) is not None
    for order in ((support, scoped), (scoped, support)):
        fresh = env.make_reader(read._policy)
        sizes = {}
        for who in order:
            sizes[who.roles] = len(fresh.classified_functions(who, env.entry))
            assert fresh.catalog_scope(who, env.entry) is (
                None if who.roles == ("scoped",) else fresh.catalog_scope(who, env.entry)
            )
        assert sizes[("support",)] == 1
        assert sizes[("scoped",)] > 1
    # asked again, over a warm cache, in the other order
    assert len(read.classified_functions(support, env.entry)) == 1
    assert len(read.classified_functions(scoped, env.entry)) > 1
    assert len(read.classified_functions(support, env.entry)) == 1


def test_the_scope_is_cached_per_policy_and_actor_not_across_them(
    env: ServiceEnv, read: ReadService
) -> None:
    first = read.catalog_scope(SUPPORT, env.entry)
    assert first is not None and first.functions == ("members_of_unit",)
    assert read.catalog_scope(SUPPORT, env.entry) is first  # cached
    other = read.catalog_scope(token("wide"), env.entry)
    assert other is not None and other.functions == ("employer_of", "members_of_unit")
    assert read.catalog_scope(token("reader"), env.entry) is None
    # two roles at once: the grants of both name entries (rights add up)
    both = read.catalog_scope(token("support", "wide"), env.entry)
    assert both is not None and both.views == ("members_report", "unit_overview")
    # a reader's role makes the caller no catalog caller whatever else it holds
    assert read.catalog_scope(token("support", "reader"), env.entry) is None


# ---- counts, listings, suggestions, search, skill, whoami ------------------------------


def test_start_counts_cover_the_catalog_only(env: ServiceEnv, read: ReadService) -> None:
    item = read.start_graphs(SUPPORT, [env.entry])[0]
    assert item["level"] == "catalog"
    assert item["withheld"] == []
    assert item["counts"]["functions"] == 0 and item["counts"]["actions"] == 0  # type: ignore[index]
    assert item["not_allowed"] == {"functions": 1, "actions": 1, "views": 1}
    modules = read.start_modules(SUPPORT, env.entry)
    counted = sum(
        sum(m.get("not_allowed", {}).get(k, 0) for k in ("functions", "actions", "views"))  # type: ignore[attr-defined]
        for m in modules
    )
    assert counted == 3
    reader = read.start_graphs(token("reader"), [env.entry])[0]
    assert reader["level"] == "read"
    assert reader["counts"]["functions"] == ALL_FUNCTIONS  # type: ignore[index]
    assert "functions" not in reader.get("not_allowed", {})  # type: ignore[operator]


def test_describe_lists_search_and_modules_name_only_the_catalog(
    env: ServiceEnv, read: ReadService
) -> None:
    found = classified(env, read, SUPPORT)
    functions = found["functions"]
    actions = found["actions"]
    views = found["views"]
    denied = Denied.of(functions, actions, views)  # type: ignore[arg-type]
    model = read.visible_model(SUPPORT, env.entry)
    hidden = ALL_NAMES - GRANTED
    listed = describe.describe_functions(
        env.entry, model, None, denied=denied.functions, include_not_allowed=True
    )
    assert [i["name"] for i in listed["functions"]] == ["members_of_unit"]  # type: ignore[index, union-attr]
    answers: list[object] = [listed]
    answers.append(
        describe.describe_actions(
            env.entry, model, None, denied=denied.actions, include_not_allowed=True
        )
    )
    answers.append(
        describe.describe_views(
            env.entry, model, None, denied=denied.views, include_not_allowed=True
        )
    )
    answers.append(describe.describe_modules(env.entry, model, None, None, None, denied))
    for text in ("members", "unit", "task", "overview", "position", "employer", "training"):
        data, _ = search_model(
            env.entry, model, text, kinds=("function", "action", "view"), denied=denied
        )
        answers.append(data)
    for answer in answers:
        assert_hidden_absent(answer, hidden)


def test_describing_a_name_outside_the_catalog_suggests_only_the_catalog(
    env: ServiceEnv, read: ReadService
) -> None:
    found = classified(env, read, SUPPORT)
    denied = Denied.of(found["functions"], found["actions"], found["views"])  # type: ignore[arg-type]
    model = read.visible_model(SUPPORT, env.entry)
    shown = describe.describe_function(env.entry, model, None, "members_of_unit", denied.functions)
    assert shown["function"]["name"] == "members_of_unit"  # type: ignore[index]
    with pytest.raises(GraphCliError) as unnamed:
        describe.describe_function(env.entry, model, None, "members_of_units", denied.functions)
    with pytest.raises(GraphCliError) as error:
        describe.describe_function(env.entry, model, None, "vacant_positions", denied.functions)
    assert unnamed.value.code == error.value.code == "function.not_found"
    # the name asked is echoed (it is the caller's own text); no other name is, in the
    # suggestions or the message
    assert_hidden_absent(error.value.details["did_you_mean"], ALL_NAMES - GRANTED)
    assert_hidden_absent(error.value.message, ALL_NAMES - GRANTED - {"vacant_positions"})


def test_the_skill_documents_name_nothing_outside_the_catalog(
    env: ServiceEnv, read: ReadService
) -> None:
    request = RequestMeta.of("skill", {"graph": env.entry.name})
    for role in ("support", "wide"):
        who = token(role)
        documents, _ = read.skill_model(who, env.entry, request)
        granted = set(read.catalog_scope(who, env.entry).functions)  # type: ignore[union-attr]
        # a catalog caller has no read.function on the graph: functions are withheld, by name or
        # not at all; actions and views it may not run are not listed either
        assert_hidden_absent(documents, ALL_NAMES - granted - GRANTED - {"employer_of"})
        assert "vacant_positions" not in str(documents)
        assert "employer_of" not in str(documents)
    # a reader's skill lists the whole catalog, as before
    reader_docs, _ = read.skill_model(token("reader"), env.entry, request)
    for name in ("vacant_positions", "employer_of"):
        assert name in str(reader_docs)


def test_whoami_shows_the_level_and_the_names_the_caller_sees(
    env: ServiceEnv, read: ReadService
) -> None:
    def graph(*roles: str) -> dict[str, object]:
        found = read.whoami(token(*roles))["permissions"]["graphs"]  # type: ignore[index]
        (item,) = found  # type: ignore[misc]
        return item  # type: ignore[no-any-return]

    support = graph("support")
    assert support["level"] == "catalog"
    assert support["withheld"] == []
    assert support["catalog"] == {
        "functions": ["members_of_unit"],
        "actions": ["create_task"],
        "views": ["unit_overview"],
        "catalog_only": True,
        "reader_rules": [],
    }
    assert "writes" not in support
    assert support["reads"]  # the types its grant lets it see
    closer = graph("closer")
    assert closer["level"] == "read"
    assert closer["catalog"] == {
        "functions": [],
        "actions": ["create_task"],
        "views": [],
        "catalog_only": False,
        "reader_rules": ["grant.3"],
    }
    assert "catalog" not in graph("reader")
    both = graph("support", "reader")
    assert both["level"] == "read"
    assert both["catalog"]["catalog_only"] is False  # type: ignore[index]
    assert both["catalog"]["reader_rules"] == ["grant.4"]  # type: ignore[index]


def test_whoami_actions_leave_out_the_entry_actions(read: ReadService) -> None:
    """``read.view`` and ``write.action`` are decided on entries, never on a graph that no
    policy names: ``permissions.allowed`` does not list them, for anyone."""
    for who in (SUPPORT, token("reader"), token("closer")):
        allowed = read.whoami(who)["permissions"]["allowed"]  # type: ignore[index]
        assert "write.action" not in allowed and "read.view" not in allowed  # type: ignore[operator]


def test_not_allowed_helper_is_empty_for_a_full_reader(env: ServiceEnv, read: ReadService) -> None:
    found = classified(env, read, token("reader"))
    assert not_allowed(found["functions"]) == ()  # type: ignore[arg-type]


def test_the_skill_says_functions_are_withheld_only_to_a_caller_whose_catalog_names_one(
    env: ServiceEnv, read: ReadService
) -> None:
    """The function document of a catalog caller is narrowed to its catalog before the question
    whether it sees an active function: a caller that is granted no function is not told that
    functions exist but are withheld from it."""
    request = RequestMeta.of("skill", {"graph": env.entry.name})
    named, _ = read.skill_model(SUPPORT, env.entry, request)
    none, _ = read.skill_model(token("viewer"), env.entry, request)
    assert "'withheld': True" in str(named)
    assert "withheld" not in str(none)
    assert "functions" not in {k for k in none if none[k]}  # type: ignore[union-attr]


def test_the_scope_follows_the_policy_in_force(env: ServiceEnv) -> None:
    """The cache key holds the policy hash: the same actor under another policy is asked again."""
    document = compile_policy(
        POLICY.encode(), file="i.yaml", graphs={"example_graph": env.entry.id}
    ).document
    source = Fixed(policy_set(document))
    changing = env.make_reader(source)
    assert changing.catalog_scope(SUPPORT, env.entry) is not None
    wider = POLICY.replace(
        "principal: {role: support}\n    graphs: [example_graph]\n    level: catalog",
        "principal: {role: support}\n    graphs: [example_graph]\n    level: read",
    ).replace(
        "    catalog: {functions: [members_of_unit], actions: [create_task],"
        " views: [unit_overview]}\n    reads: " + READS,
        "    catalog: {actions: [create_task]}",
    )
    after = compile_policy(
        wider.encode(), file="i.yaml", graphs={"example_graph": env.entry.id}
    ).document
    source.policy = policy_set(after)
    assert changing.catalog_scope(SUPPORT, env.entry) is None
    assert len(changing.classified_functions(SUPPORT, env.entry)) == ALL_FUNCTIONS
