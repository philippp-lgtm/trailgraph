"""The describe services with entries the caller sees but may not use (graph start plan S4,
step 2): lists count them and list them only on request, module views count them per kind and
module, a description and a search mark them (``allowed: false`` and ``missing``), a search
ranks them after the allowed hits of the same tier, and the repeated answers never name a hidden
entry (``support.catalog_leaks``)."""

import contextlib
import json
from pathlib import Path
from typing import Any

import pytest
from support.actions import VALID as ACTION_FILES
from support.actions import publish_actions, valid_sources
from support.catalog_leaks import assert_hidden_absent
from support.catalog_policies import LIMITS, RIGHTS, Policy, company_env, reader
from support.services import ServiceEnv

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.records import RequestMeta
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
    assert plain["functions"] == [] and plain["not_allowed"] == 9
    marked = seen.call(
        "describe_functions",
        seen.functions,
        denied=seen.denied.functions,
        include_not_allowed=True,
    )
    assert len(marked["functions"]) == 9 and "not_allowed" in marked
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


SENSITIVE_KEYS = (
    "query",
    "returns",
    "ops",
    "preconditions",
    "sections",
    "examples",
    "touches",
    "needs",
    "hash",
    "call",
    "featured",
    "max_bytes",
    "page_bytes",
)
NO_FIND = Policy(deny=("read.find", "read.function"))
READ_ONLY = Policy(grant="level: read")


def literals(detail: dict[str, Any]) -> list[str]:
    """Strings of the maintainer's definition that only who may use the entry reads: the query,
    the ops and conditions, the example texts, the sections."""
    found: list[str] = []
    for key in ("query", "hash"):
        if isinstance(detail.get(key), str):
            found.append(detail[key])
    for key in ("ops", "sections"):
        found += [json.dumps(item, sort_keys=True) for item in detail.get(key, [])]
    for item in detail.get("examples", []):
        found += [v for v in item.values() if isinstance(v, str)]
    for item in detail.get("preconditions", []):
        found += [item["summary"]]
    return found


@pytest.mark.parametrize(
    ("kind", "name", "policy"),
    [
        ("function", "members_of_unit", NO_FUNCTIONS),
        ("action", "assign_position", READ_ONLY),
        ("action", "offboard_employee", READ_ONLY),
        ("view", "unit_overview", NO_FUNCTIONS),
        ("view", "tasks_by_state", NO_FIND),
        ("view", "org_catalog", NO_FIND),
    ],
)
def test_a_not_allowed_entry_is_shown_for_what_it_is_for_and_how_it_is_called_only(
    env: ServiceEnv, kind: str, name: str, policy: Policy
) -> None:
    full = Seen(env, Policy())
    plural = f"{kind}s"
    complete = full.call(
        f"describe_{kind}", getattr(full, plural), name, getattr(full.denied, plural)
    )[kind]
    seen = Seen(env, policy)
    shown = seen.call(
        f"describe_{kind}", getattr(seen, plural), name, getattr(seen.denied, plural)
    )[kind]
    assert shown["allowed"] is False and shown["missing"]
    assert not set(SENSITIVE_KEYS) & set(shown), sorted(set(SENSITIVE_KEYS) & set(shown))
    text = json.dumps(shown)
    secrets = literals(complete)
    assert secrets, "the definition has something to withhold"
    for secret in secrets:
        assert secret not in text, secret
    # what stays is the same as for a caller who may use it, plus the marks
    for key in ("name", "module", "summary", "use_when", "status", "signature"):
        assert shown[key] == complete[key]
    assert [p["name"] for p in shown["params"]] == [p["name"] for p in complete["params"]]
    assert all("default" not in p and "required" in p for p in shown["params"])
    # a list shows no more than the usual item
    listed = seen.call(
        f"describe_{plural}",
        getattr(seen, plural),
        denied=getattr(seen.denied, plural),
        include_not_allowed=True,
    )[plural]
    item = next(i for i in listed if i["name"] == name)
    assert not set(SENSITIVE_KEYS) & set(item)


def withheld_word(complete: dict[str, Any], kind: str) -> str:
    """A word that occurs only in the withheld text of an entry (an example question or request,
    a section title), not in what is shown of it."""
    texts = [str(complete[k]) for k in ("name", "summary", "use_when")]
    texts += [p["name"] + " " + p["summary"] for p in complete["params"]]
    shown = " ".join(texts).lower()
    withheld = [
        v for e in complete.get("examples", []) for v in e.values() if isinstance(v, str)
    ] + [s["title"] for s in complete.get("sections", []) if s.get("title")]
    for text in withheld:
        for word in sorted(text.replace(",", " ").replace(".", " ").split(), key=len, reverse=True):
            if len(word) > 4 and word.isalpha() and word.lower() not in shown:
                return word
    raise AssertionError(f"no withheld word in {kind} {complete['name']}")


@pytest.mark.parametrize(
    ("kind", "name", "policy"),
    [
        ("function", "members_of_unit", NO_FUNCTIONS),
        ("action", "create_task", READ_ONLY),
        ("view", "unit_overview", NO_FUNCTIONS),
    ],
)
def test_a_search_does_not_match_the_texts_only_who_may_use_an_entry_reads(
    env: ServiceEnv, kind: str, name: str, policy: Policy
) -> None:
    """The example texts and section titles of a not-allowed entry are not searchable (nor are
    its touched terms): a search would tell a caller whether a literal of the definition is in
    them. The same word finds the entry for a caller who may use it."""
    plural = f"{kind}s"
    full, refused = Seen(env, Policy()), Seen(env, policy)
    complete = full.call(f"describe_{kind}", getattr(full, plural), name)[kind]
    word = withheld_word(complete, kind)
    assert name in {h["curie"] for h in full.search(word, kinds=(kind,))}
    assert name not in {h["curie"] for h in refused.search(word, kinds=(kind,))}
    # what it is for is still searchable, and marked
    use_when = complete["use_when"].split()
    found = refused.search(max(use_when, key=len), kinds=(kind,))
    assert any(h["curie"] == name and h.get("allowed") is False for h in found)


def test_describe_by_name_suggests_what_the_caller_sees_not_allowed_ones_included(
    env: ServiceEnv,
) -> None:
    seen = Seen(env, NO_FUNCTIONS)
    with pytest.raises(GraphCliError) as caught:
        seen.call("describe_function", seen.functions, "members_of_unitt", seen.denied.functions)
    assert caught.value.code == "function.not_found"
    assert "members_of_unit" in caught.value.details["did_you_mean"]  # type: ignore[operator]


def deprecated(name: str, source: str, replaced_by: str) -> str:
    return (ACTION_FILES / source).read_text().replace(
        f"name: {source[:-5]}", f"name: {name}"
    ) + f"status: deprecated\nreplaced_by: {replaced_by}\n"


def with_deprecated(tmp_path: Path) -> ServiceEnv:
    found = company_env(tmp_path / "root")
    publish_actions(
        found,
        {
            **valid_sources(),
            "old_task.yaml": deprecated("old_task", "create_task.yaml", "offboard_employee"),
            "old_offboard.yaml": deprecated(
                "old_offboard", "offboard_employee.yaml", "create_task"
            ),
        },
    )
    return found


def replaced_by(seen: Seen, name: str) -> object:
    return seen.call("describe_action", seen.actions, name, seen.denied.actions)["action"][
        "replaced_by"
    ]


def test_a_replacement_is_named_only_when_the_caller_may_use_it(tmp_path: Path) -> None:
    env = with_deprecated(tmp_path)
    everything = Seen(env, Policy())
    assert replaced_by(everything, "old_task") == "offboard_employee"  # allowed: named
    # employees are read-only: offboard_employee is seen but not allowed, so not named; the
    # deprecated old_offboard is itself not allowed, and its replacement create_task is allowed
    employees_read = Seen(env, Policy(types=(("ppl:Employee", "read"),)))
    assert "offboard_employee" in {c.entry.name for c in employees_read.denied.actions}
    assert replaced_by(employees_read, "old_task") is None
    assert "old_offboard" in {c.entry.name for c in employees_read.denied.actions}
    assert replaced_by(employees_read, "old_offboard") == "create_task"
    # employees hidden: offboard_employee is not seen at all, and is never named
    hidden = Seen(env, Policy(types=(("ppl:Employee", "none"),)))
    assert "offboard_employee" not in names_of(hidden)["actions"]
    assert replaced_by(hidden, "old_task") is None
    assert_hidden_absent(
        hidden.call("describe_action", hidden.actions, "old_task", hidden.denied.actions),
        {"offboard_employee"},
    )


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


# ---- calls ------------------------------------------------------------------------------


def open_for_view(env: ServiceEnv, policy: Policy) -> tuple[Any, Any]:
    read = reader(env, policy)
    return read, read.open(env.actor, env.entry, "read.get", RequestMeta("view", "x"))


def test_a_default_view_the_caller_may_not_use_is_skipped_like_a_hidden_one(
    env: ServiceEnv,
) -> None:
    """``unit_overview`` is the default view of a unit and runs a function: without
    ``read.function`` the card answers, no warning and no recorded denial; only a call by name is
    refused."""
    department = next(e for e in env.create("org:Department", "d").entities if e.local == "d")
    ref = str(department.id)
    read, opened = open_for_view(env, Policy())
    data, _ = read.view(opened, ref)
    assert data["view"]["name"] == "unit_overview"
    events = len(env.installation_events())
    read, opened = open_for_view(env, NO_FUNCTIONS)
    data, warnings = read.view(opened, ref)
    assert data["view"] == {"name": "card", "origin": "generated"} and warnings == []
    assert len(env.installation_events()) == events
    with pytest.raises(GraphCliError) as caught:
        read.view(opened, ref, name="unit_overview")
    assert caught.value.code == "access.denied"
    assert len(env.installation_events()) == events + 1


def test_a_refusal_names_every_right_the_view_lacks_and_records_the_first(
    env: ServiceEnv,
) -> None:
    read, opened = open_for_view(env, NO_FIND_NO_FUNCTIONS)
    with pytest.raises(GraphCliError) as caught:
        read.view(opened, None, name="org_catalog")  # a set view that runs a function
    error = caught.value
    assert error.code == "access.denied"
    assert "read.find on graph" in error.message and "read.function on graph" in error.message
    assert error.details["action"] == "read.find"  # the first denying decision is the record
    assert env.installation_events()[-1]["result"]["status"] == "denied"
    with pytest.raises(GraphCliError) as typo:
        read.view(opened, None, name="org_catalogue")
    assert typo.value.code == "view.not_found"
    assert "org_catalog" in typo.value.details["did_you_mean"]  # a name the caller sees
