"""The describe services with entries the caller sees but may not use (graph start plan S4,
step 2): lists count them and list them only on request, module views count them per kind and
module, a description and a search mark them (``allowed: false`` and ``missing``), a search
ranks them after the allowed hits of the same tier, and the repeated answers never name a hidden
entry (``support.catalog_leaks``)."""

import contextlib
from pathlib import Path
from typing import Any

import pytest
from support.catalog_leaks import assert_hidden_absent
from support.catalog_policies import LIMITS, RIGHTS, Policy, company_env, reader
from support.services import ServiceEnv

from graph_cli.kernel.errors import GraphCliError
from graph_cli.services import describe
from graph_cli.services.catalog_access import Denied, allowed_entries, not_allowed
from graph_cli.services.schema_search import search_model

NO_FUNCTIONS = Policy(deny=("read.function",))
NO_FIND_NO_FUNCTIONS = Policy(deny=("read.function", "read.find"))
NO_TASKS = Policy(types=(("ops:Task", "read"),))
HIDE_TASK = Policy(types=(("ops:Task", "none"),))


@pytest.fixture
def env(tmp_path: Path) -> ServiceEnv:
    return company_env(tmp_path / "root")


class Seen:
    """What a caller may use and may not, per kind, as the commands get it."""

    def __init__(self, env: ServiceEnv, policy: Policy) -> None:
        read = reader(env, policy)
        published = read.published_actions(env.entry)
        assert published is not None
        functions = read.classified_functions(env.actor, env.entry)
        actions = read.classified_actions(env.actor, env.entry, published)
        views = read.classified_views(env.actor, env.entry)
        self.env = env
        self.functions = allowed_entries(functions) or None
        self.actions = allowed_entries(actions) or None
        self.views = allowed_entries(views) or None
        self.denied = Denied(not_allowed(functions), not_allowed(actions), not_allowed(views))

    def call(self, name: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        found = getattr(describe, name)(self.env.entry, self.env.model, *args, **kwargs)
        assert isinstance(found, dict)
        return found

    def search(self, text: str, **kwargs: Any) -> list[dict[str, Any]]:
        data, _ = search_model(
            self.env.entry,
            self.env.model,
            text,
            functions=self.functions,
            actions=self.actions,
            views=self.views,
            denied=self.denied,
            **kwargs,
        )
        return list(data["items"])  # type: ignore[arg-type]


# ---- lists ------------------------------------------------------------------------------


def test_a_list_counts_what_the_caller_may_not_use_and_lists_it_on_request(
    env: ServiceEnv,
) -> None:
    seen = Seen(env, NO_FUNCTIONS)
    plain = seen.call("describe_functions", seen.functions, denied=seen.denied.functions)
    assert plain["functions"] == [] and plain["not_allowed"] == 7
    marked = seen.call(
        "describe_functions",
        seen.functions,
        denied=seen.denied.functions,
        include_not_allowed=True,
    )
    assert len(marked["functions"]) == 7 and "not_allowed" in marked
    assert all(
        f["allowed"] is False and f["missing"] == [{"action": "read.function", "resource": "graph"}]
        for f in marked["functions"]
    )
    # the usual keys of an item stay
    assert {"name", "module", "summary", "signature", "status"} <= set(marked["functions"][0])


def test_nothing_changes_for_a_caller_who_may_use_everything(env: ServiceEnv) -> None:
    seen = Seen(env, Policy())
    assert seen.denied.count() == 0
    listed = seen.call("describe_actions", seen.actions, denied=seen.denied.actions)
    assert "not_allowed" not in listed
    assert all("allowed" not in a and "missing" not in a for a in listed["actions"])
    flagged = seen.call("describe_actions", seen.actions, denied=(), include_not_allowed=True)
    assert flagged == listed
    modules = seen.call("describe_modules", seen.functions, seen.actions, seen.views, seen.denied)[
        "modules"
    ]
    assert all("not_allowed" not in m for m in modules)


def test_the_count_follows_the_module_and_deprecation_filters(env: ServiceEnv) -> None:
    seen = Seen(env, NO_FIND_NO_FUNCTIONS)
    everything = seen.call("describe_views", seen.views, denied=seen.denied.views)
    organization = seen.call(
        "describe_views", seen.views, module="organization", denied=seen.denied.views
    )
    assert organization["not_allowed"] < everything["not_allowed"]
    assert organization["module"] == "organization"
    assert "legacy_unit_overview" not in [
        v["name"]
        for v in seen.call(
            "describe_views",
            seen.views,
            denied=seen.denied.views,
            include_not_allowed=True,
        )["views"]
    ]  # deprecated, listed only with include_deprecated


def test_a_module_view_counts_the_kinds_and_adds_views(env: ServiceEnv) -> None:
    seen = Seen(env, NO_FIND_NO_FUNCTIONS)
    modules = {
        m["id"]: m
        for m in seen.call(
            "describe_modules", seen.functions, seen.actions, seen.views, seen.denied
        )["modules"]
    }
    organization = modules["organization"]
    assert organization["functions"] == 0 and organization["actions"] == 2
    assert organization["views"] == 1  # position_overview: an entity view with no function
    assert (
        organization["not_allowed"]["functions"] >= 1 and organization["not_allowed"]["views"] >= 1
    )
    assert "actions" not in organization["not_allowed"]
    assert "not_allowed" not in modules["core"]  # no kind to count there


def test_a_module_page_lists_views_and_the_not_allowed_on_request(env: ServiceEnv) -> None:
    seen = Seen(env, NO_FIND_NO_FUNCTIONS)
    args = (seen.functions, seen.actions, seen.views, seen.denied)
    plain = seen.call("describe_module", "organization", *args)["module"]
    assert [v["name"] for v in plain["views"]] == ["position_overview"]
    assert plain["functions"] == [] and plain["not_allowed"]["functions"] >= 1
    flagged = seen.call("describe_module", "organization", *args, include_not_allowed=True)[
        "module"
    ]
    assert {f["name"] for f in flagged["functions"]} >= {"members_of_unit", "vacant_positions"}
    assert all(f["allowed"] is False for f in flagged["functions"])
    assert flagged["views"][0]["name"] == "position_overview"  # allowed first
    assert "allowed" not in flagged["views"][0]


def test_a_graph_with_no_views_has_no_view_counts(tmp_path: Path) -> None:
    found = company_env(tmp_path / "root")
    found.catalog.views.clear()  # type: ignore[union-attr]
    seen = Seen(found, Policy())
    modules = seen.call("describe_modules", seen.functions, seen.actions, seen.views, seen.denied)
    assert all("views" not in m for m in modules["modules"])


# ---- one entry --------------------------------------------------------------------------


def test_a_not_allowed_entry_is_described_like_any_other_and_marked(env: ServiceEnv) -> None:
    seen = Seen(env, NO_FUNCTIONS)
    marked = seen.call(
        "describe_function", seen.functions, "members_of_unit", seen.denied.functions
    )
    shown = marked["function"]
    assert shown["allowed"] is False and shown["query"].startswith("MATCH")
    assert shown["missing"] == [{"action": "read.function", "resource": "graph"}]
    plain = Seen(env, Policy()).call(
        "describe_function", Seen(env, Policy()).functions, "members_of_unit"
    )
    assert {k: v for k, v in shown.items() if k not in ("allowed", "missing")} == plain["function"]


def test_describe_by_name_suggests_what_the_caller_sees_not_allowed_ones_included(
    env: ServiceEnv,
) -> None:
    seen = Seen(env, NO_FUNCTIONS)
    with pytest.raises(GraphCliError) as caught:
        seen.call("describe_function", seen.functions, "members_of_unitt", seen.denied.functions)
    assert caught.value.code == "function.not_found"
    assert "members_of_unit" in caught.value.details["did_you_mean"]  # type: ignore[operator]


def test_a_replacement_the_caller_may_not_use_is_not_named(env: ServiceEnv) -> None:
    seen = Seen(env, Policy(grant="level: read"))  # no write right: every action is not allowed
    assert seen.actions is None
    shown = seen.call("describe_action", seen.actions, "assign_position", seen.denied.actions)
    assert shown["action"]["allowed"] is False
    assert shown["action"]["replaced_by"] is None


# ---- search -----------------------------------------------------------------------------


def test_a_search_finds_not_allowed_entries_marked_and_after_the_allowed_ones(
    env: ServiceEnv,
) -> None:
    seen = Seen(env, NO_FUNCTIONS)
    hits = seen.search("unit")
    marked = [h for h in hits if h.get("allowed") is False]
    assert {h["curie"] for h in marked} >= {"members_of_unit", "policies_for_unit"}
    assert all(h["kind"] in ("function", "view") for h in marked)
    assert all(h["missing"] == [{"action": "read.function", "resource": "graph"}] for h in marked)
    # an allowed hit carries neither key
    assert all("allowed" not in h and "missing" not in h for h in hits if h not in marked)
    # within one tier the allowed hits come first
    tier = {}
    for index, hit in enumerate(hits):
        tier.setdefault((hit["match"]["field"], hit["match"]["how"]), []).append(
            (hit.get("allowed") is False, index)
        )
    for entries in tier.values():
        flags = [flag for flag, _ in entries]
        assert flags == sorted(flags)


def test_a_search_of_a_kind_shows_only_that_kind_with_the_marks(env: ServiceEnv) -> None:
    seen = Seen(env, NO_FIND_NO_FUNCTIONS)
    views = seen.search("overview", kinds=("view",))
    assert views and {h["kind"] for h in views} == {"view"}
    assert {h["curie"]: h.get("missing") for h in views}["unit_overview"] == [
        {"action": "read.function", "resource": "graph"}
    ]
    in_module = seen.search("unit", kinds=("function",), module="organization")
    assert in_module and all(h["module"] == "organization" for h in in_module)


def test_the_same_policy_without_rights_finds_the_same_names_as_with_them(
    env: ServiceEnv,
) -> None:
    """A search lists the same entries for a caller who may use them and one who may not;
    only the marks differ."""
    with_rights = Seen(env, Policy()).search("position")
    without = Seen(env, Policy(grant="level: read", deny=("read.function", "read.find"))).search(
        "position"
    )
    assert [(h["kind"], h["curie"]) for h in with_rights] != []
    assert {(h["kind"], h["curie"]) for h in without} == {
        (h["kind"], h["curie"]) for h in with_rights
    }


# ---- hidden stays absent ----------------------------------------------------------------


def test_nothing_the_describe_services_return_names_a_hidden_entry(env: ServiceEnv) -> None:
    """Task is hidden: ``create_task`` and ``tasks_by_state`` occur in no list, count, module
    page, description, search hit or suggestion, with or without the rights."""
    hidden = {"create_task", "tasks_by_state"}
    for rights in (Policy(), Policy(grant="level: read", deny=("read.function", "read.find"))):
        seen = Seen(env, Policy(types=HIDE_TASK.types, grant=rights.grant, deny=rights.deny))
        outputs: list[object] = []
        for call, kwargs in (
            ("describe_functions", {"functions": seen.functions, "denied": seen.denied.functions}),
            ("describe_actions", {"actions": seen.actions, "denied": seen.denied.actions}),
            ("describe_views", {"views": seen.views, "denied": seen.denied.views}),
        ):
            source = next(iter(kwargs.values()))
            outputs.append(
                seen.call(call, source, denied=kwargs["denied"], include_not_allowed=True)
            )
        outputs.append(
            seen.call("describe_modules", seen.functions, seen.actions, seen.views, seen.denied)
        )
        for module in ("organization", "operations", "people", "core"):
            with contextlib.suppress(GraphCliError):  # a module with nothing the caller sees
                outputs.append(
                    seen.call(
                        "describe_module",
                        module,
                        seen.functions,
                        seen.actions,
                        seen.views,
                        seen.denied,
                        include_not_allowed=True,
                    )
                )
        for text in ("task", "create", "state", "checklist"):
            outputs.append(seen.search(text))
        with pytest.raises(GraphCliError) as caught:
            seen.call("describe_action", seen.actions, "create_task", seen.denied.actions)
        outputs.append(caught.value.details["did_you_mean"])  # the name asked is the caller's own
        with pytest.raises(GraphCliError) as caught:
            seen.call("describe_view", seen.views, "tasks_by_stat", seen.denied.views)
        outputs.append(caught.value.details["did_you_mean"])
        assert_hidden_absent(outputs, hidden)


def names_of(seen: Seen) -> dict[str, set[str]]:
    return {
        "functions": {f.name for f in seen.functions or ()}
        | {c.entry.name for c in seen.denied.functions},
        "actions": {a.name for a in seen.actions or ()}
        | {c.entry.name for c in seen.denied.actions},
        "views": {v.name for v in seen.views or ()} | {c.entry.name for c in seen.denied.views},
    }


def test_a_hidden_name_is_in_no_describe_output_over_the_policy_matrix(env: ServiceEnv) -> None:
    """For every read limit and every kind of missing right: whatever the read view hides is in
    none of the lists, counts, module pages and searches, marked or not."""
    every = names_of(Seen(env, Policy()))
    flat = {name for names in every.values() for name in names}
    hidden_seen = 0
    for limit in LIMITS:
        oracle = names_of(Seen(env, Policy(types=limit)))
        hidden = {name for kind in every for name in every[kind] - oracle[kind]}
        hidden_seen += len(hidden)
        for rights in RIGHTS:
            seen = Seen(env, Policy(types=limit, **rights))
            assert names_of(seen) == oracle  # the rights never change what is hidden
            outputs: list[object] = [
                seen.call(
                    "describe_functions",
                    seen.functions,
                    denied=seen.denied.functions,
                    include_not_allowed=True,
                ),
                seen.call(
                    "describe_actions",
                    seen.actions,
                    denied=seen.denied.actions,
                    include_not_allowed=True,
                ),
                seen.call(
                    "describe_views",
                    seen.views,
                    denied=seen.denied.views,
                    include_not_allowed=True,
                ),
                seen.call(
                    "describe_modules", seen.functions, seen.actions, seen.views, seen.denied
                ),
            ]
            for text in sorted(
                {word for name in flat for word in name.split("_") if len(word) > 3}
            ):
                outputs.append(seen.search(text))
            assert_hidden_absent(outputs, hidden)
    assert hidden_seen > 0
