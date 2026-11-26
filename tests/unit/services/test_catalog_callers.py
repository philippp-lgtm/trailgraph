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
from graph_cli.services.catalog_access import (
    Denied,
    EntryPath,
    EntryRef,
    allowed_entries,
    not_allowed,
)
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
  overview: {{description: "Catalog only, a view whose row function it is not granted"}}
  legacy: {{description: "Catalog only, a deprecated function"}}
  blind: {{description: "Catalog only, a default view that names a type it cannot see"}}
  ghost: {{description: "Catalog only, a view the graph does not publish"}}
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
  - entrypoint: server
    principal: {{role: overview}}
    graphs: [example_graph]
    level: catalog
    catalog: {{views: [unit_overview]}}
    reads: {READS}
  - entrypoint: server
    principal: {{role: legacy}}
    graphs: [example_graph]
    level: catalog
    catalog: {{functions: [unit_members]}}
    reads: {READS}
  - entrypoint: server
    principal: {{role: blind}}
    graphs: [example_graph]
    level: catalog
    catalog: {{views: [unit_overview]}}
    reads: ["ppl:*"]
  - entrypoint: server
    principal: {{role: ghost}}
    graphs: [example_graph]
    level: catalog
    catalog: {{views: [ghost_view]}}
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
    # a named function or view the policy lets it run is allowed through its entry grant (step 2);
    # an action is classified as before until step 3 lets it write
    assert verdicts(found["functions"]) == {"members_of_unit": ()}
    assert verdicts(found["actions"]) == {"create_task": ("write.create on type:ops:Task",)}
    assert verdicts(found["views"]) == {"unit_overview": ()}
    assert [e.name for e in allowed_entries(found["functions"])] == ["members_of_unit"]  # type: ignore[arg-type]
    assert [e.name for e in allowed_entries(found["views"])] == ["unit_overview"]  # type: ignore[arg-type]
    assert allowed_entries(found["actions"]) == ()  # type: ignore[arg-type]


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
    # no read.function on the graph: the allowed functions are those its grants name
    assert [f.name for f in read.functions(SUPPORT, env.entry) or ()] == ["members_of_unit"]
    assert read.actions(SUPPORT, env.entry) is None  # step 3
    assert [v.name for v in read.views(SUPPORT, env.entry) or ()] == ["unit_overview"]
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
    # the function and the view it is granted are allowed; the action waits for step 3
    assert item["counts"]["functions"] == 1 and item["counts"]["actions"] == 0  # type: ignore[index]
    assert item["counts"]["views"] == 1  # type: ignore[index]
    assert item["not_allowed"] == {"actions": 1}
    modules = read.start_modules(SUPPORT, env.entry)
    counted = sum(
        sum(m.get("not_allowed", {}).get(k, 0) for k in ("functions", "actions", "views"))  # type: ignore[attr-defined]
        for m in modules
    )
    assert counted == 1
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
    allowed = read.functions(SUPPORT, env.entry)
    listed = describe.describe_functions(
        env.entry, model, allowed, denied=denied.functions, include_not_allowed=True
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
            env.entry,
            model,
            read.views(SUPPORT, env.entry),
            denied=denied.views,
            include_not_allowed=True,
        )
    )
    allowed_views = read.views(SUPPORT, env.entry)
    answers.append(
        describe.describe_modules(env.entry, model, allowed, None, allowed_views, denied)
    )
    for text in ("members", "unit", "task", "overview", "position", "employer", "training"):
        data, _ = search_model(
            env.entry,
            model,
            text,
            kinds=("function", "action", "view"),
            functions=allowed,
            views=allowed_views,
            denied=denied,
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
    allowed = read.functions(SUPPORT, env.entry)
    shown = describe.describe_function(
        env.entry, model, allowed, "members_of_unit", denied.functions
    )
    assert shown["function"]["name"] == "members_of_unit"  # type: ignore[index]
    with pytest.raises(GraphCliError) as unnamed:
        describe.describe_function(env.entry, model, allowed, "members_of_units", denied.functions)
    with pytest.raises(GraphCliError) as error:
        describe.describe_function(env.entry, model, allowed, "vacant_positions", denied.functions)
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
        scope = read.catalog_scope(who, env.entry)
        assert scope is not None
        # the functions and views it may run are listed, the action it may not write is not, and
        # nothing else of the catalogs is named (a granted name its read view hides stays out)
        runnable = {*scope.functions, *scope.views} - {"employer_of"}
        assert_hidden_absent(documents, ALL_NAMES - runnable)
        assert "vacant_positions" not in str(documents)
        assert "employer_of" not in str(documents)
        assert documents["catalog_only"] is True
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


def test_the_skill_lists_the_functions_a_catalog_caller_may_run_and_withholds_none(
    env: ServiceEnv, read: ReadService
) -> None:
    """The function document of a catalog caller is narrowed to its catalog; it may run those
    functions now, so the skill lists them instead of saying functions are withheld. A caller
    that is granted no function is told nothing about functions."""
    request = RequestMeta.of("skill", {"graph": env.entry.name})
    named, _ = read.skill_model(SUPPORT, env.entry, request)
    none, _ = read.skill_model(token("viewer"), env.entry, request)
    assert "withheld" not in str(named)
    assert [f["name"] for f in named["functions"]["functions"]] == ["members_of_unit"]  # type: ignore[index]
    assert "withheld" not in str(none)
    assert "functions" not in {k for k in none if none[k]}  # type: ignore[union-attr]
    # a reader's documents carry no marker of the catalog
    assert "catalog_only" not in read.skill_model(token("reader"), env.entry, request)[0]


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


# ---- step 2: reads through entries -----------------------------------------------------

OVERVIEW = token("overview")


def policy_reader(
    env: ServiceEnv, *, deny: tuple[str, ...] = (), exact: tuple[str, ...] = ()
) -> ReadService:
    """The reader of ``POLICY`` with deny rules: ``deny`` as a policy author writes them
    (``action`` lists with no resource, each covering the graph, its types and its entries) and
    ``exact`` on the graph resource alone, so that the types stay visible and a granted entry's
    own need is the only thing taken away."""
    text = POLICY
    if deny:
        rules = "".join(f"  - {{id: no_{a.replace('.', '_')}, action: [{a}]}}\n" for a in deny)
        text += f"deny:\n{rules}"
    document = compile_policy(
        text.encode(), file="i.yaml", graphs={"example_graph": env.entry.id}
    ).document
    rules_list = document["rules"]
    assert isinstance(rules_list, list)
    for action in exact:
        rules_list.append(
            {
                "id": f"deny.exact_{action.replace('.', '_')}",
                "effect": "deny",
                "action": [action],
                "resource": [f"graph:{env.entry.id}"],
            }
        )
    return env.make_reader(Fixed(policy_set(document)))


def open_through(
    read: ReadService,
    env: ServiceEnv,
    who: ActorContext,
    kind: str,
    name: str,
    *,
    as_of: None = None,
):  # type: ignore[no-untyped-def]
    """The graph opened for ``run`` (a function) or ``view --name`` (a view) of ``name``."""
    command, action = ("run", "read.function") if kind == "function" else ("view", "read.get")
    request = RequestMeta.of(command, {"graph": env.entry.name, "name": name})
    return read.open(who, env.entry, action, request, through=EntryPath(kind, (name,)))


def department(env: ServiceEnv, local: str = "d") -> str:
    created = env.create("org:Department", local)
    return str(next(e for e in created.entities if e.local == local).id)


def denials(env: ServiceEnv, since: int) -> list[dict[str, object]]:
    return [dict(e) for e in env.installation_events()[since:]]


@pytest.mark.doc_claim("security_model.type_limits_complete")
def test_a_granted_function_runs_without_read_function_on_the_graph(
    env: ServiceEnv, read: ReadService
) -> None:
    unit = department(env)
    events = len(env.installation_events())
    opened = open_through(read, env, SUPPORT, "function", "members_of_unit")
    assert opened.through == EntryRef("function", "members_of_unit")
    data, page, _ = read.run(opened, "members_of_unit", [("unit", unit)], limit=10, offset=0)
    assert data["function"]["name"] == "members_of_unit"  # type: ignore[index]
    assert page.total == 0  # nobody holds a position in the new department
    assert len(env.installation_events()) == events  # an allow records nothing
    # a reader needs no entry: the graph's own right allowed it
    request = RequestMeta.of("run", {"graph": env.entry.name, "name": "members_of_unit"})
    own = read.open(
        token("reader"), env.entry, "read.function", request,
        through=EntryPath("function", ("members_of_unit",)),
    )  # fmt: skip
    assert own.through is None
    # the limits of the grant still bound what the function reads: the model it runs over has no
    # type outside ``reads``
    assert opened.read_view.limited and "leg:LegalEntity" not in opened.model.classes
    assert "leg:LegalEntity" in own.model.classes


def test_an_entry_outside_the_catalog_is_refused_like_one_that_does_not_exist(
    env: ServiceEnv, read: ReadService
) -> None:
    hidden = ALL_NAMES - GRANTED
    for kind, unnamed, missing, granted in (
        ("function", "vacant_positions", "vacant_positionz", "members_of_unit"),
        ("view", "members_report", "members_reportz", "unit_overview"),
    ):  # fmt: skip
        answers = []
        for name in (unnamed, missing):
            events = len(env.installation_events())
            with pytest.raises(GraphCliError) as caught:
                open_through(read, env, SUPPORT, kind, name)
            error = caught.value
            assert error.code == f"{kind}.not_found"
            assert error.details["did_you_mean"] in ([], [granted])
            assert_hidden_absent(error.details["did_you_mean"], hidden)
            # audited as a refusal, the decision being the graph's: no entry is named
            (event,) = denials(env, events)
            assert event["result"]["status"] == "denied"  # type: ignore[index]
            (decision,) = event["authz"]["decisions"]  # type: ignore[index]
            assert decision["rule"] == "default.deny" and "/" not in decision["resource"]
            assert unnamed not in str(event) and missing not in str(event)
            answers.append((error.code, str(error.message).replace(name, "X")))
        assert answers[0] == answers[1]


def decided(event: dict[str, object]) -> dict[str, dict[str, str]]:
    """The decisions of an audit event by effect (an event holds at most one of each here)."""
    found = event["authz"]["decisions"]  # type: ignore[index]
    by_effect = {d["effect"]: d for d in found}
    assert len(by_effect) == len(found)
    return by_effect


def test_a_granted_entry_a_deny_rule_takes_is_denied_naming_the_grant(
    env: ServiceEnv,
) -> None:
    for exact, kind, name, action in (
        ((), "function", "members_of_unit", "read.function"),  # the entry decision
        (("read.function",), "function", "members_of_unit", "read.function"),  # its need
        (("read.get",), "view", "unit_overview", "read.get"),
    ):
        read = policy_reader(env, deny=(action,) if not exact else (), exact=exact)
        events = len(env.installation_events())
        with pytest.raises(GraphCliError) as caught:
            open_through(read, env, SUPPORT, kind, name)
        assert caught.value.code == "access.denied"
        assert caught.value.details["action"] == action
        (event,) = denials(env, events)
        found = decided(event)
        assert found["deny"]["action"] == action and found["deny"]["code"] == "access.denied"
        if exact or kind == "view":
            # the entry decision that got the request so far stands next to the denial
            assert found["allow"]["rule"] == f"grant.1.{kind}s"
            assert found["allow"]["resource"].endswith(f"/{kind}:{name}")
        else:  # the entry decision itself was the one the rule denied
            assert set(found) == {"deny"}
            assert found["deny"]["resource"].endswith(f"/function:{name}")


# every need of an entry, each taken away by a deny rule alone: (how, deny, role, kind, entry,
# missing). "rule": an installation rule (covers the graph, its types and entries), "graph": a
# rule on the graph resource alone (types stay visible)
NEEDS = (
    ("rule", "read.function", "support", "functions", "members_of_unit", "read.function on func"),
    ("rule", "read.view", "support", "views", "unit_overview", "read.view on view:unit_over"),
    ("graph", "read.function", "support", "functions", "members_of_unit", "read.function on gr"),
    ("graph", "read.get", "support", "views", "unit_overview", "read.get on graph"),
    ("graph", "read.function", "support", "views", "unit_overview", "read.function on graph"),
    ("graph", "read.get", "viewer", "views", "org_catalog", "read.get on graph"),
    ("graph", "read.find", "viewer", "views", "org_catalog", "read.find on graph"),
    ("graph", "read.get", "wide", "views", "members_report", "read.get on graph"),
    ("graph", "read.find", "wide", "views", "members_report", "read.find on graph"),
    ("graph", "read.function", "wide", "views", "members_report", "read.function on graph"),
    ("rule", "read.find", "wide", "views", "members_report", "read.find on graph"),
    ("rule", "read.function", "wide", "views", "members_report", "read.function on graph"),
)  # fmt: skip


@pytest.mark.doc_claim("security_model.catalog_grants")
@pytest.mark.parametrize(("how", "deny", "role", "kind", "name", "missing"), NEEDS)
def test_a_deny_rule_wins_over_every_need_of_a_granted_entry(
    env: ServiceEnv, how: str, deny: str, role: str, kind: str, name: str, missing: str
) -> None:
    """Classification: the entry is seen, not allowed, and names the right a rule takes; with no
    rule it is allowed. Listings and the manifest follow (allowed entries only)."""
    plain = classified(env, policy_reader(env), token(role))[kind]
    assert verdicts(plain)[name] == ()
    rules = policy_reader(env, deny=(deny,)) if how == "rule" else policy_reader(env, exact=(deny,))
    taken = classified(env, rules, token(role))[kind]
    found = verdicts(taken)[name]
    assert len(found) >= 1 and any(m.startswith(missing) for m in found), found
    assert name not in [c.entry.name for c in taken if c.allowed]  # type: ignore[attr-defined]


def test_a_deny_rule_takes_only_the_entries_that_need_the_right(env: ServiceEnv) -> None:
    """``read.find`` is a need of a set view, not of an entity view or a function; ``read.view``
    only of views."""
    find = policy_reader(env, exact=("read.find",))
    assert verdicts(classified(env, find, SUPPORT)["views"]) == {"unit_overview": ()}
    assert verdicts(classified(env, find, SUPPORT)["functions"]) == {"members_of_unit": ()}
    assert verdicts(classified(env, find, token("viewer"))["views"])["org_catalog"] != ()
    view = policy_reader(env, deny=("read.view",))
    assert verdicts(classified(env, view, SUPPORT)["functions"]) == {"members_of_unit": ()}
    assert verdicts(classified(env, view, SUPPORT)["views"])["unit_overview"] != ()


def test_the_view_needs_of_a_granted_view_are_refused_by_name_and_recorded(
    env: ServiceEnv,
) -> None:
    read = policy_reader(env, exact=("read.find",))
    viewer = token("viewer")
    opened = open_through(read, env, viewer, "view", "org_catalog")  # read.get is not denied
    events = len(env.installation_events())
    with pytest.raises(GraphCliError) as caught:
        read.view(opened, None, name="org_catalog")
    assert caught.value.code == "access.denied" and caught.value.details["action"] == "read.find"
    (event,) = denials(env, events)
    found = decided(event)
    assert (found["allow"]["action"], found["allow"]["rule"]) == ("read.view", "grant.6.views")
    assert found["deny"]["action"] == "read.find"


def test_a_granted_view_runs_its_row_functions_without_them_being_granted(
    env: ServiceEnv, read: ReadService
) -> None:
    unit = department(env)
    assert [c.entry.name for c in read.classified_functions(OVERVIEW, env.entry)] == []
    opened = open_through(read, env, OVERVIEW, "view", "unit_overview")
    data, warnings = read.view(opened, unit, name="unit_overview")
    rows = next(s for s in data["sections"] if s["kind"] == "rows")  # type: ignore[index, attr-defined]
    assert rows["function"]["name"] == "members_of_unit" and "error" not in rows
    assert "view.section_failed" not in {w.code for w in warnings}
    # the row function is not a tool of the caller: running it by name is an unknown name
    with pytest.raises(GraphCliError) as caught:
        open_through(read, env, OVERVIEW, "function", "members_of_unit")
    assert caught.value.code == "function.not_found"


@pytest.mark.doc_claim("security_model.view_is_read_get")
def test_the_card_and_unseen_classes_are_refused_alike_and_a_granted_default_view_answers(
    env: ServiceEnv, read: ReadService
) -> None:
    unit = department(env)
    task = str(next(e for e in env.create("ops:Task", "t").entities if e.local == "t").id)
    path = read.default_views_path(SUPPORT, env.entry)
    assert path == EntryPath("view", ("unit_overview",), uniform=True)
    assert read.default_views_path(token("viewer"), env.entry) is None  # a view without a default
    assert read.default_views_path(token("reader"), env.entry) is None

    def default(ref: str) -> tuple[dict[str, object], list[object]]:
        request = RequestMeta.of("view", {"graph": env.entry.name, "ref": ref})
        opened = read.open(SUPPORT, env.entry, "read.get", request, through=path)
        return read.view(opened, ref)  # type: ignore[return-value]

    data, _ = default(unit)  # the class has a default view in the catalog
    assert data["view"]["name"] == "unit_overview"  # type: ignore[index]
    refused = []
    for ref in (task, "0192f0aa-0000-7000-8000-000000000000", "ffffffff", unit[:8] + "zz"):
        events = len(env.installation_events())
        with pytest.raises(GraphCliError) as caught:
            default(ref)  # a class with no default in the catalog, an id that is not there, ...
        error = caught.value
        assert error.code == "access.denied" and error.details["action"] == "read.get"
        assert "ref" not in error.message and error.details["resource"].count("/") == 0
        refused.append((error.code, error.message, error.details))
        assert len(denials(env, events)) == 1
    assert all(r == refused[0] for r in refused)
    # the card itself is refused when the graph is opened
    request = RequestMeta.of("view", {"graph": env.entry.name, "ref": unit, "name": "card"})
    with pytest.raises(GraphCliError) as card:
        read.open(SUPPORT, env.entry, "read.get", request)
    assert card.value.code == "access.denied" and card.value.details["action"] == "read.get"


def test_a_stale_cursor_of_a_catalog_caller_says_restart_without_the_as_of_hint(
    env: ServiceEnv, read: ReadService
) -> None:
    for local in ("a", "b", "c"):
        department(env, local)
    viewer = token("viewer")
    opened = open_through(read, env, viewer, "view", "org_catalog")
    first, _ = read.view(opened, None, name="org_catalog", page_bytes=1)
    cursor = first["next"]
    assert isinstance(cursor, str)
    department(env, "later")  # the graph moves on
    stale = open_through(read, env, viewer, "view", "org_catalog")
    with pytest.raises(GraphCliError) as caught:
        read.view(stale, None, name="org_catalog", cursor=cursor, page_bytes=1)
    assert caught.value.code == "view.cursor_stale"
    assert "--as-of" not in caught.value.message and "as-of" not in str(caught.value.details)
    assert "cursor_seq" not in caught.value.details
    # a reader may read the earlier state, and is told how
    request = RequestMeta.of("view", {"graph": env.entry.name, "name": "org_catalog"})
    reading = read.open(token("reader"), env.entry, "read.get", request)
    page, _ = read.view(reading, None, name="org_catalog", page_bytes=1)
    department(env, "later_again")
    reading = read.open(token("reader"), env.entry, "read.get", request)
    with pytest.raises(GraphCliError) as reader_error:
        read.view(reading, None, name="org_catalog", cursor=str(page["next"]), page_bytes=1)
    assert "--as-of tx:" in reader_error.value.message
    assert "cursor_seq" in reader_error.value.details


def test_the_skill_digest_covers_the_entry_list(env: ServiceEnv, read: ReadService) -> None:
    support, wide, scoped, reader = SUPPORT, token("wide"), token("scoped"), token("reader")
    # one read view, three callers: the catalog (or none) tells them apart
    assert read.read_view(support, env.entry) == read.read_view(wide, env.entry)
    digests = {
        who.roles: read.skill_digest(who, env.entry) for who in (support, wide, scoped, reader)
    }
    assert digests[("support",)] != digests[("wide",)]
    assert digests[("support",)] != digests[("scoped",)]
    # every other caller's digest is the read view's, as it was
    assert digests[("scoped",)] == read.read_view(scoped, env.entry).digest
    assert digests[("reader",)] is None
    # the entry list is in it: another catalog under the same read view is another digest
    changed = POLICY.replace(
        "catalog: {functions: [members_of_unit], actions: [create_task], views: [unit_overview]}",
        "catalog: {functions: [members_of_unit, vacant_positions], actions: [create_task],"
        " views: [unit_overview]}",
    )
    assert changed != POLICY
    document = compile_policy(
        changed.encode(), file="i.yaml", graphs={"example_graph": env.entry.id}
    ).document
    other = env.make_reader(Fixed(policy_set(document)))
    assert other.skill_digest(support, env.entry) != digests[("support",)]


def test_the_skill_of_a_catalog_reader_holds_a_deprecated_function_only_when_granted(
    env: ServiceEnv, read: ReadService
) -> None:
    from support.catalog_policies import publish_functions
    from support.functions import VALID
    from support.views import function_sources

    deprecated = (VALID / "unit_members.yaml").read_text()
    publish_functions(env, {**function_sources(), "unit_members.yaml": deprecated})
    request = RequestMeta.of("skill", {"graph": env.entry.name})
    legacy_docs, _ = read.skill_model(token("legacy"), env.entry, request)
    listed = legacy_docs["functions"]["functions"]  # type: ignore[index, call-overload]
    kept = {f["name"]: f.get("status") for f in listed}  # type: ignore[union-attr]
    assert kept == {"unit_members": "deprecated"}
    support_docs, _ = read.skill_model(SUPPORT, env.entry, request)
    assert "unit_members" not in str(support_docs)
    reader_docs, _ = read.skill_model(token("reader"), env.entry, request)
    assert "unit_members" in str(reader_docs)  # a reader's document is the catalog as it is


@pytest.mark.doc_claim("security_model.view_names_row_functions")
def test_the_row_function_of_a_granted_view_is_named_by_the_surfaces_of_that_view_only(
    env: ServiceEnv, read: ReadService
) -> None:
    """Accepted residual leak (Q7): a view names the function its ``rows`` section runs, so the
    caller of a granted view learns that name from the view's own surfaces. Pinned here: those
    surfaces name it, and no surface of functions does (the caller is not granted it)."""
    from support.catalog_leaks import mentions

    who, row = OVERVIEW, "members_of_unit"
    unit = department(env)
    model = read.visible_model(who, env.entry)
    allowed_views = read.views(who, env.entry)
    assert allowed_views is not None
    # the surfaces of the view
    detail = describe.describe_view(env.entry, model, allowed_views, "unit_overview")
    manifest = heads(
        model, (), (), allowed_views, frozenset(), rows=read.view_row_functions(who, env.entry)
    )
    opened = open_through(read, env, who, "view", "unit_overview")
    answer, _ = read.view(opened, unit, name="unit_overview")
    request = RequestMeta.of("skill", {"graph": env.entry.name})
    documents, _ = read.skill_model(who, env.entry, request)
    for surface in (detail, manifest, answer):
        assert mentions(surface, row)
    # the surfaces of functions name nothing the grants do not
    denied = Denied.of(*(classified(env, read, who)[k] for k in ("functions", "actions", "views")))  # type: ignore[arg-type]
    for surface in (
        describe.describe_functions(
            env.entry,
            model,
            read.functions(who, env.entry),
            denied=denied.functions,
            include_not_allowed=True,
        ),
        search_model(env.entry, model, "members", kinds=("function",), denied=denied)[0],
        documents.get("functions"),
        documents["views"],  # the skill's view document is reduced: no section names it
        read.whoami(who)["permissions"]["graphs"],  # type: ignore[index]
        [t for t in manifest if t.kind == "function"],
    ):
        assert not mentions(surface, row)


@pytest.mark.doc_claim("security_model.view_names_row_functions")
def test_the_deprecation_of_a_row_function_is_the_warning_of_its_granted_view(
    env: ServiceEnv, read: ReadService
) -> None:
    from support.catalog_policies import publish_functions
    from support.views import function_sources

    sources = function_sources()
    sources["members_of_unit.yaml"] += "status: deprecated\n"
    publish_functions(env, sources)
    unit = department(env)
    opened = open_through(read, env, OVERVIEW, "view", "unit_overview")
    _, warnings = read.view(opened, unit, name="unit_overview")
    (deprecated,) = [w for w in warnings if w.code == "view.function_deprecated"]
    assert deprecated.details["function"] == "members_of_unit"
    assert (
        deprecated.details["replaced_by"] is None
    )  # a replacement outside the catalog is not named


def test_a_granted_name_the_graph_does_not_publish_is_unknown_like_any_other(
    env: ServiceEnv, read: ReadService
) -> None:
    """``legacy`` is granted ``unit_members``, which this graph has not published: the entry
    decision allows the name, the lookup among what the caller sees finds nothing, and the answer
    is that of a name that never existed (no suggestion of an unnamed entry)."""
    legacy = token("legacy")
    answers = []
    for name in ("unit_members", "unit_memberz"):
        with pytest.raises(GraphCliError) as caught:
            # the granted name opens the graph and fails at the lookup, the other at the door
            read.run(
                open_through(read, env, legacy, "function", name), name, [], limit=10, offset=0
            )
        error = caught.value
        assert error.code == "function.not_found"
        assert error.details["did_you_mean"] == []
        answers.append(str(error.message).replace(name, "X"))
    assert answers[0] == answers[1]


# ---- step 2, batch 2b: references and the narrow lookup --------------------------------------

READER = token("reader")


@dataclasses.dataclass
class Names:
    """The entities of ``named_units``: ids by local name, their aliases and the hidden one."""

    ids: dict[str, str]
    aliases: dict[str, str]

    def __getitem__(self, local: str) -> str:
        return self.ids[local]


def entity_ops(
    env: ServiceEnv, cls: str, local: str, attr: str, name: str, **edges: str
) -> list[dict[str, object]]:
    """Ops creating ``cls`` named ``name`` (its label attribute ``attr``) with edges to earlier
    locals (``relation=local``)."""
    ops = env.factory.create(cls, local)
    main = next(o for o in ops if o.get("local") == local)
    main["attrs"][attr] = name  # type: ignore[index]
    if cls == "leg:LegalEntity":
        main["attrs"]["country"] = "DE"  # type: ignore[index]
    if edges:
        found = [{"rel": r, "target": f"@{t}"} for r, t in edges.items()]
        main["edges"] = [*main.get("edges", []), *found]  # type: ignore[call-overload]
    return ops


ENG_A_EDGES = {"org:part_of": "corp", "leg:operated_by": "hidden_le"}


def named_units(env: ServiceEnv) -> Names:
    """Three homonym units named Engineering (one under Corporate and operated by a legal entity the
    callers cannot see, one under Subsidiary, one without a parent), a position of the same name
    (another class) and a sales department."""
    ops = [
        *entity_ops(env, "leg:LegalEntity", "hidden_le", "name", "Hidden Holding"),
        *entity_ops(env, "org:Department", "corp", "name", "Corporate"),
        *entity_ops(env, "org:Department", "sub", "name", "Subsidiary"),
        *entity_ops(env, "org:Department", "eng_a", "name", "Engineering", **ENG_A_EDGES),
        *entity_ops(
            env, "org:Department", "eng_b", "name", "Engineering", **{"org:part_of": "sub"}
        ),
        *entity_ops(env, "org:Department", "eng_c", "name", "Engineering"),
        *entity_ops(env, "org:Department", "sales", "name", "Sales"),
        *entity_ops(env, "org:Position", "pos", "title", "Engineering"),
    ]
    created = env.apply(*ops)
    ids = {e.local: str(e.id) for e in created.entities if e.local}
    revisions = {str(r.id): r for r in env.all_revisions()}
    return Names(ids, {k: str(revisions[v].alias) for k, v in ids.items()})


def lookup_view(
    read: ReadService,
    env: ServiceEnv,
    who: ActorContext,
    use: str | None,
    *,
    for_source: str | None = None,
    **spec: object,
):  # type: ignore[no-untyped-def]
    """The graph opened for ``lookup TEXT --for use`` the way the command opens it: through the
    entry the request is for when it has the narrow shape, else as a plain ``read.find``."""
    from graph_cli.query.lookup import LookupSpec

    lookup = LookupSpec(text="Engineering", **spec)  # type: ignore[arg-type]
    path = read.lookup_path(who, env.entry, lookup, for_use=use, for_source=for_source)
    request = RequestMeta.of("lookup", {"graph": env.entry.name, "for": use})
    opened = read.open(who, env.entry, "read.find", request, through=path)
    return opened, lookup


def candidates(
    read: ReadService, env: ServiceEnv, who: ActorContext, use: str, *texts: str, **spec: object
) -> dict[str, object]:
    opened, lookup = lookup_view(read, env, who, use, **spec)
    data, page = read.lookup(opened, lookup, texts or ("Engineering",), for_use=use)
    return {**data, "page": page.to_json()}


SLOT = "members_of_unit.unit"


@pytest.mark.doc_claim("security_model.catalog_no_enumeration")
@pytest.mark.doc_claim("security_model.catalog_lookup_residual")
def test_a_catalog_callers_reference_takes_an_id_or_an_alias_and_no_trailing_hex(
    env: ServiceEnv, read: ReadService
) -> None:
    units = named_units(env)
    suffix = units["eng_a"][-8:]
    answers = {}
    for who in (SUPPORT, READER):
        opened = open_through(read, env, who, "function", "members_of_unit")
        for ref in (units["eng_a"], f"urn:uuid:{units['eng_a']}", units.aliases["eng_a"]):
            data, _, _ = read.run(opened, "members_of_unit", [("unit", ref)], limit=5, offset=0)
            assert data["function"]["name"] == "members_of_unit"  # type: ignore[index]
        try:
            read.run(opened, "members_of_unit", [("unit", suffix)], limit=5, offset=0)
        except GraphCliError as exc:
            answers[who.roles] = exc
    # a reader may still write an id by its trailing hex digits
    assert ("reader",) not in answers
    refused = answers[("support",)]
    assert refused.code == "function.parameter_invalid"
    assert "trailing hex" not in refused.message and refused.code != "entity.ambiguous_ref"
    assert refused.details["parameter"] == "unit" and "candidates" not in refused.details


@pytest.mark.doc_claim("security_model.catalog_no_enumeration")
def test_a_suffix_is_never_matched_against_the_ids_of_a_catalog_caller(
    env: ServiceEnv, read: ReadService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No answer may tell how many ids end in a suffix (the ambiguous-reference list of up to ten
    full ids is an enumeration): for a catalog caller a suffix is not looked at, whether it ends
    ids or not, while a reader's suffix is matched as before."""
    from graph_cli.services import refs

    units = named_units(env)
    matched: list[str] = []
    real = refs.parse_entity_ref

    def spy(text: str, **kw: bool) -> object:
        found = real(text, **kw)
        if isinstance(found, refs.AbbrevRef):
            matched.append(text)
        return found

    monkeypatch.setattr(refs, "parse_entity_ref", spy)
    nothing = "0" * 8  # ends no id
    refusals = []
    for ref in (units["eng_a"][-8:], units["eng_a"][-12:], nothing, units["eng_a"][-8:].upper()):
        opened = open_through(read, env, SUPPORT, "function", "members_of_unit")
        with pytest.raises(GraphCliError) as caught:
            read.run(opened, "members_of_unit", [("unit", ref)], limit=5, offset=0)
        error = caught.value
        assert error.code == "function.parameter_invalid"
        refusals.append((error.message.replace(ref, "X"), set(error.details)))
        assert "candidates" not in error.details and units["eng_a"] not in str(error.details)
    assert len(set(map(str, refusals))) == 1  # the same answer for a suffix that ends ids or not
    assert matched == []  # nothing was matched against the ids
    opened = open_through(read, env, READER, "function", "members_of_unit")
    read.run(opened, "members_of_unit", [("unit", units["eng_a"][-8:])], limit=5, offset=0)
    assert matched == [units["eng_a"][-8:]]  # a reader's suffix is matched as before


@pytest.mark.doc_claim("security_model.catalog_no_enumeration")
def test_an_id_of_another_class_or_a_hidden_one_is_not_found_without_naming_its_class(
    env: ServiceEnv, read: ReadService
) -> None:
    units = named_units(env)
    nowhere = "0192f0aa-0000-7000-8000-000000000000"
    outcomes: dict[str, GraphCliError] = {}
    opened = open_through(read, env, SUPPORT, "function", "members_of_unit")
    for label, ref in (
        ("position", units["pos"]),  # a class it sees, not the class of the parameter
        ("hidden", units["hidden_le"]),  # a class outside its reads
        ("nowhere", nowhere),
        ("position_alias", units.aliases["pos"]),  # the same by alias, and an alias nobody has
        ("hidden_alias", units.aliases["hidden_le"]),
        ("nowhere_alias", "DEPT-9999"),
    ):
        with pytest.raises(GraphCliError) as caught:
            read.run(opened, "members_of_unit", [("unit", ref)], limit=5, offset=0)
        outcomes[label] = caught.value
    for found in outcomes.values():
        assert found.code == "entity.not_found"
    shapes = {
        label: (
            e.code,
            e.message.replace(ref, "X"),
            {k: v for k, v in e.details.items() if k not in ("ref", "id")},
        )
        for (label, e), ref in zip(
            outcomes.items(),
            (
                units["pos"],
                units["hidden_le"],
                nowhere,
                units.aliases["pos"],
                units.aliases["hidden_le"],
                "DEPT-9999",
            ),
            strict=True,
        )
    }
    assert len({str(v) for v in shapes.values()}) == 1  # one answer, however the id is no unit
    assert_hidden_absent(str(outcomes["position"].details), {"org:Position", "ops:Task"})
    # everyone else is told the class, as before
    opened = open_through(read, env, READER, "function", "members_of_unit")
    with pytest.raises(GraphCliError) as plain:
        read.run(opened, "members_of_unit", [("unit", units["pos"])], limit=5, offset=0)
    assert plain.value.code == "function.parameter_type"
    assert plain.value.details["found"] == "org:Position"


@pytest.mark.doc_claim("security_model.catalog_no_enumeration")
@pytest.mark.doc_claim("security_model.view_is_read_get")
def test_the_entity_of_an_entity_view_follows_the_same_rule(
    env: ServiceEnv, read: ReadService
) -> None:
    units = named_units(env)
    nowhere = "0192f0aa-0000-7000-8000-000000000000"
    outcomes = []
    for ref in (units["pos"], units["hidden_le"], nowhere):  # another class, hidden, unknown
        opened = open_through(read, env, SUPPORT, "view", "unit_overview")
        with pytest.raises(GraphCliError) as caught:
            read.view(opened, ref, name="unit_overview")
        error = caught.value
        assert error.code == "entity.not_found"
        outcomes.append((error.code, error.message.replace(ref, "X"), set(error.details)))
    assert outcomes[0] == outcomes[1] == outcomes[2]
    opened = open_through(read, env, SUPPORT, "view", "unit_overview")
    with pytest.raises(GraphCliError) as suffix:
        read.view(opened, units["eng_a"][-8:], name="unit_overview")
    assert suffix.value.code == "usage.invalid_argument"
    assert "trailing hex" not in suffix.value.message
    assert suffix.value.details["for"] == "view:unit_overview._entity"  # the lookup that serves it
    data, _ = read.view(opened, units.aliases["eng_a"], name="unit_overview")
    assert data["view"]["name"] == "unit_overview"  # type: ignore[index]
    # a reader keeps the class mismatch and the suffix
    opened = open_through(read, env, READER, "view", "unit_overview")
    with pytest.raises(GraphCliError) as mismatch:
        read.view(opened, units["pos"], name="unit_overview")
    assert mismatch.value.code == "view.class_mismatch"
    read.view(opened, units["eng_a"][-8:], name="unit_overview")


@pytest.mark.doc_claim("security_model.catalog_no_enumeration")
@pytest.mark.doc_claim("security_model.catalog_lookup_residual")
def test_the_narrow_lookup_lists_the_exact_active_candidates_of_the_slots_class(
    env: ServiceEnv, read: ReadService
) -> None:
    units = named_units(env)
    found = candidates(read, env, SUPPORT, SLOT)
    items = found["items"]
    assert [i["id"] for i in items] == sorted(  # type: ignore[index, attr-defined]
        (units[k] for k in ("eng_a", "eng_b", "eng_c")), key=lambda i: i
    ) or {i["id"] for i in items} == {units[k] for k in ("eng_a", "eng_b", "eng_c")}  # type: ignore[attr-defined]
    for item in items:  # type: ignore[attr-defined]
        assert set(item) == {
            "text",
            "id",
            "alias",
            "type",
            "record_status",
            "label",
            "match",
            "facts",
        }
        assert item["type"] == "org:Department" and item["record_status"] == "active"
    # the position of the same name is another class: not listed, not counted
    (count,) = found["texts"]  # type: ignore[misc]
    assert count == {"text": "Engineering", "total": 3, "exact": 3, "excluded": 0}
    assert found["fit"]["types"] == ["org:Department", "org:OrgUnit", "org:Team"]  # type: ignore[index]
    assert units["pos"] not in str(found)
    # the facts tell homonyms apart: the parent, never what the caller cannot see
    by_id = {i["id"]: i for i in items}  # type: ignore[attr-defined]
    parents = {
        k: [(f["rel"], f["label"]) for f in by_id[units[k]]["facts"]]
        for k in ("eng_a", "eng_b", "eng_c")
    }
    assert parents == {
        "eng_a": [("org:part_of", "Corporate")],
        "eng_b": [("org:part_of", "Subsidiary")],
        "eng_c": [],
    }
    assert_hidden_absent(found, {"Hidden Holding", "leg:operated_by", "leg:LegalEntity"})
    assert units["hidden_le"] not in str(found)
    # one candidate has no facts to tell it apart from
    sales = candidates(read, env, SUPPORT, SLOT, "Sales")
    assert [i["facts"] for i in sales["items"]] == [None]  # type: ignore[attr-defined]


@pytest.mark.doc_claim("security_model.catalog_no_enumeration")
def test_the_narrow_lookup_answers_the_first_page_with_no_way_to_the_next(
    env: ServiceEnv, read: ReadService
) -> None:
    named_units(env)
    found = candidates(read, env, SUPPORT, SLOT, limit=2)
    assert len(found["items"]) == 2  # type: ignore[arg-type]
    page = found["page"]
    assert page["truncated"] is True and page["next_offset"] is None  # type: ignore[index]
    assert found["texts"][0]["total"] == 3  # type: ignore[index]  # the count of the name itself


@pytest.mark.doc_claim("security_model.catalog_no_enumeration")
def test_the_narrow_lookup_leaves_out_inactive_records(env: ServiceEnv, read: ReadService) -> None:
    units = named_units(env)
    env.apply({"op": "retract", "id": units["eng_c"]}, reason="closed")
    found = candidates(read, env, SUPPORT, SLOT)
    assert {i["id"] for i in found["items"]} == {units["eng_a"], units["eng_b"]}  # type: ignore[attr-defined]
    assert found["texts"][0]["total"] == 2  # type: ignore[index]
    assert all(i["record_status"] == "active" for i in found["items"])  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("use", "for_source", "spec"),
    [
        (None, None, {}),  # free lookup
        ("org:part_of", None, {}),  # for a relation
        (None, "org:part_of", {}),  # for-source
        (SLOT, "org:part_of", {}),
        (SLOT, None, {"match": "prefix"}),
        (SLOT, None, {"offset": 1}),
        (SLOT, None, {"include_inactive": True}),
        (SLOT, None, {"types": ("org:Team",)}),
        (SLOT, None, {"lang": "en"}),
        (SLOT, None, {"ignore_accents": True}),
        ("function:no_such_function.unit", None, {}),
    ],
)
@pytest.mark.doc_claim("security_model.catalog_no_enumeration")
def test_every_other_shape_of_lookup_stays_refused_for_a_catalog_caller(
    env: ServiceEnv,
    read: ReadService,
    use: str | None,
    for_source: str | None,
    spec: dict[str, object],
) -> None:
    named_units(env)
    events = len(env.installation_events())
    shape = "no_such_function" in str(use)
    with pytest.raises(GraphCliError) as caught:
        lookup_view(read, env, SUPPORT, use, for_source=for_source, **spec)
    error = caught.value
    if shape:  # an entry that does not exist for it
        assert error.code == "function.not_found"
    else:  # the plain question: read.find on the graph, which it does not hold
        assert error.code == "access.denied" and error.details["action"] == "read.find"
        assert error.details["resource"].count("/") == 0
        assert str(error) and "Engineering" not in str(error.details)
    assert len(denials(env, events)) == 1  # audited as a refusal
    # the same request is allowed for a reader (the narrow shapes need no entry)
    if not shape:
        opened, _ = lookup_view(read, env, READER, use, for_source=for_source, **spec)
        assert opened.through is None


def test_a_lookup_for_an_entry_outside_the_catalog_is_not_found_like_one_that_does_not_exist(
    env: ServiceEnv, read: ReadService
) -> None:
    named_units(env)
    answers = []
    for use in ("function:vacant_positions.unit", "function:vacant_positionz.unit"):
        with pytest.raises(GraphCliError) as caught:
            lookup_view(read, env, SUPPORT, use)
        error = caught.value
        assert error.code == "function.not_found"
        assert error.details["did_you_mean"] in ([], ["members_of_unit"])
        answers.append(error.message.replace(use.split(":")[1].split(".")[0], "X"))
    assert answers[0] == answers[1]
    # an unqualified name is asked the same way
    with pytest.raises(GraphCliError) as bare:
        lookup_view(read, env, SUPPORT, "vacant_positions.unit")
    assert bare.value.code == "function.not_found"
    # a name the grants name but the graph does not publish is answered alike
    legacy = token("legacy")
    with pytest.raises(GraphCliError) as unpublished:
        opened, lookup = lookup_view(read, env, legacy, "function:unit_members.unit")
        read.lookup(opened, lookup, ("Engineering",), for_use="function:unit_members.unit")
    assert unpublished.value.code == "function.not_found"
    assert unpublished.value.message == bare.value.message.replace(
        "vacant_positions", "unit_members"
    )


def test_the_narrow_lookup_serves_the_entity_of_a_granted_entity_view(
    env: ServiceEnv, read: ReadService
) -> None:
    units = named_units(env)
    found = candidates(read, env, SUPPORT, "view:unit_overview._entity", "Sales")
    assert [i["id"] for i in found["items"]] == [units["sales"]]  # type: ignore[attr-defined]
    # a view the grants do not name has no slot to look up for
    with pytest.raises(GraphCliError) as caught:
        lookup_view(read, env, SUPPORT, "view:position_overview._entity")
    assert caught.value.code == "view.not_found"


@pytest.mark.doc_claim("security_model.catalog_grants")
def test_a_deny_rule_on_read_find_takes_the_narrow_lookup_away(env: ServiceEnv) -> None:
    named_units(env)
    plain = policy_reader(env)
    assert plain.lookup_served(SUPPORT, env.entry)
    taken = policy_reader(env, exact=("read.find",))
    assert not taken.lookup_served(SUPPORT, env.entry)
    events = len(env.installation_events())
    with pytest.raises(GraphCliError) as caught:
        lookup_view(taken, env, SUPPORT, SLOT)
    assert caught.value.code == "access.denied" and caught.value.details["action"] == "read.find"
    (event,) = denials(env, events)
    found = decided(event)
    assert found["deny"]["action"] == "read.find"
    assert found["allow"]["rule"] == "grant.1.functions"  # the entry decision stands next to it
    # a rule that denies read.find everywhere (also on the entry) is the same refusal
    wide = policy_reader(env, deny=("read.find",))
    assert not wide.lookup_served(SUPPORT, env.entry)
    with pytest.raises(GraphCliError):
        lookup_view(wide, env, SUPPORT, SLOT)


def test_the_lookup_is_served_to_whoever_may_find_or_works_through_entries(
    env: ServiceEnv, read: ReadService
) -> None:
    assert read.lookup_served(SUPPORT, env.entry)
    assert read.lookup_served(token("viewer"), env.entry)
    assert read.lookup_served(READER, env.entry)
    assert not read.lookup_served(token("nobody"), env.entry)  # a role no grant names
    assert not policy_reader(env, exact=("read.find",)).lookup_served(READER, env.entry)


def test_a_lookup_opened_through_an_entry_cannot_ask_for_more(
    env: ServiceEnv, read: ReadService
) -> None:
    """The command layer decides the shape before it opens; a service that is handed a wider
    request on such a graph refuses it (an internal error, never a wider answer)."""
    from graph_cli.query.lookup import LookupSpec

    named_units(env)
    opened, _ = lookup_view(read, env, SUPPORT, SLOT)
    assert opened.through == EntryRef("function", "members_of_unit")
    for wide, use, source in (
        (LookupSpec(text="Eng", match="prefix"), SLOT, None),
        (LookupSpec(text="Eng", offset=1), SLOT, None),
        (LookupSpec(text="Eng", include_inactive=True), SLOT, None),
        (LookupSpec(text="Eng"), None, None),
        (LookupSpec(text="Eng"), "org:part_of", None),
        (LookupSpec(text="Eng"), SLOT, "org:part_of"),
    ):
        with pytest.raises(GraphCliError) as caught:
            read.lookup(opened, wide, ("Eng",), for_use=use, for_source=source)
        assert caught.value.code == "internal.error"
    # an opened graph is for its own entry's slot only (another entry the caller may use too)
    with pytest.raises(GraphCliError) as other:
        read.lookup(opened, LookupSpec(text="Eng"), ("Eng",), for_use="view:unit_overview._entity")
    assert other.value.code == "internal.error"
    # the same wide requests are fine for a graph the caller's own right opened
    own = read.open(READER, env.entry, "read.find", RequestMeta.of("lookup", {"graph": "g"}))
    data, _ = read.lookup(own, LookupSpec(text="Eng", match="prefix"), ("Eng",))
    assert data["items"]


# ---- review round 1: the nameless form, a changing policy -----------------------------------


def nameless(
    read: ReadService, env: ServiceEnv, who: ActorContext, ref: str
) -> GraphCliError | tuple[dict[str, object], list[object]]:
    """``view REF`` the way the command opens it: through the default views its grants name."""
    path = read.default_views_path(who, env.entry)
    request = RequestMeta.of("view", {"graph": env.entry.name, "ref": ref})
    try:
        opened = read.open(who, env.entry, "read.get", request, through=path)
        return read.view(opened, ref)  # type: ignore[return-value]
    except GraphCliError as exc:
        return exc


@pytest.mark.doc_claim("security_model.view_is_read_get")
def test_the_nameless_form_answers_the_one_refusal_of_the_card_when_an_entry_is_denied(
    env: ServiceEnv,
) -> None:
    """A rule that takes ``read.view`` away makes the granted default view refused: the answer is
    the refusal of the card (``read.get`` on the graph), that of an entity that has no default view,
    and it names no view; the audit holds the graph's decision alone."""
    unit = department(env)
    task = str(next(e for e in env.create("ops:Task", "t").entities if e.local == "t").id)
    denying = policy_reader(env, deny=("read.view",))
    answers = []
    for ref in (unit, task):  # a class with a granted default view, and one without
        events = len(env.installation_events())
        error = nameless(denying, env, SUPPORT, ref)
        assert isinstance(error, GraphCliError)
        assert error.code == "access.denied" and error.details["action"] == "read.get"
        assert error.details["resource"].count("/") == 0 and "unit_overview" not in str(error)
        (event,) = denials(env, events)
        assert set(decided(event)) == {"deny"}
        assert "unit_overview" not in str(event)
        answers.append((error.code, error.message, error.details))
    assert answers[0] == answers[1]
    # a rule on the entry's own need is refused the same way
    for exact in (("read.get",), ("read.function",)):
        error = nameless(policy_reader(env, exact=exact), env, SUPPORT, unit)
        assert isinstance(error, GraphCliError) and error.details["resource"].count("/") == 0
        assert "unit_overview" not in str(error)


def test_a_granted_default_view_that_names_a_hidden_type_is_never_named(
    env: ServiceEnv, read: ReadService
) -> None:
    person = str(next(e for e in env.create("ppl:Employee", "p").entities if e.local == "p").id)
    blind = token("blind")
    assert read.default_views_path(blind, env.entry) is None  # it does not exist for this caller
    events = len(env.installation_events())
    error = nameless(read, env, blind, person)
    assert isinstance(error, GraphCliError) and error.details["action"] == "read.get"
    assert error.details["resource"].count("/") == 0
    assert "unit_overview" not in str(error) and "unit_overview" not in str(denials(env, events))
    # the same caller asking for it by name: an unknown view, with no suggestion
    with pytest.raises(GraphCliError) as caught:
        opened = open_through(read, env, blind, "view", "unit_overview")
        read.view(opened, None, name="unit_overview")
    assert caught.value.code == "view.not_found" and caught.value.details["did_you_mean"] == []


def test_the_default_views_of_a_graph_the_caller_cannot_see_are_none(env: ServiceEnv) -> None:
    """The path is built from what the caller sees: for a graph it may not list there is none, so
    the refusal is the graph's ``graph.not_found`` and no catalog of the graph is read first."""
    hidden = policy_reader(env, exact=("read.list",))
    assert hidden.default_views_path(SUPPORT, env.entry) is None
    request = RequestMeta.of("view", {"graph": env.entry.name, "ref": "x"})
    with pytest.raises(GraphCliError) as caught:
        hidden.open(SUPPORT, env.entry, "read.get", request, through=None)
    assert caught.value.code == "graph.not_found"


def test_a_policy_that_changes_while_a_through_open_decides_does_not_crash_it(
    env: ServiceEnv,
) -> None:
    """``authorize_through`` asks the policy several times; when a refresh lands after the first
    answers (a deny) and before the last (an allow), the open follows the later one."""

    class Flipping:
        def __init__(self, first: object, then: object, after: int) -> None:
            self.first, self.then, self.after, self.calls = first, then, after, 0

        def current(self, graph: object) -> object:
            self.calls += 1
            return self.first if self.calls <= self.after else self.then

        def refresh(self) -> None:
            pass

    def compiled(text: str) -> object:
        return policy_set(
            compile_policy(
                text.encode(), file="i.yaml", graphs={"example_graph": env.entry.id}
            ).document
        )

    denying = compiled(POLICY + "deny:\n  - {id: no_fn, action: [read.function]}\n")
    allowing = compiled(POLICY)
    for after in range(1, 6):
        source = Flipping(denying, allowing, after)
        read = env.make_reader(source)
        request = RequestMeta.of("run", {"graph": env.entry.name, "name": "members_of_unit"})
        try:
            read.open(
                token("scoped"), env.entry, "read.function", request,
                through=EntryPath("function", ("members_of_unit",)),
            )  # fmt: skip
        except GraphCliError as exc:  # the earlier policy denied it: a refusal, never a crash
            assert exc.code in ("access.denied", "function.not_found"), exc
        except AssertionError as exc:  # pragma: no cover - what the bug was
            raise AssertionError(f"after {after} reads of the policy") from exc


def test_a_granted_view_the_graph_does_not_publish_is_unknown_like_any_other(
    env: ServiceEnv, read: ReadService
) -> None:
    ghost = token("ghost")
    answers = []
    for name in ("ghost_view", "ghost_viewz"):
        with pytest.raises(GraphCliError) as caught:
            opened = open_through(read, env, ghost, "view", name)  # the granted name opens it...
            read.view(opened, None, name=name)  # ...and fails at the lookup
        error = caught.value
        assert error.code == "view.not_found" and error.details["did_you_mean"] == []
        answers.append(str(error.message).replace(name, "X"))
    assert answers[0] == answers[1]
