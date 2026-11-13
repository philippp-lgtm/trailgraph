"""Functions, actions and the skill documents of a reader with type read limits, through the
services on both backends (type read limits plan V7): what the reader may run is listed, run and
described; what it may not is as unknown as a name that was never published; a need is met only
by a type the reader also sees; the skill documents hold nothing hidden and its digest follows
the visible set.

The deny rules below stand for a policy with a read allow list: both hide types through the
decision ``read.get`` on ``graph:<id>/type:<T>`` (the visible set is made of those decisions).
"""

import json
from pathlib import Path
from typing import Any

import pytest
from support.actions import action_service, do, publish_actions, read_dir, valid_sources
from support.fixtures import FIXTURES
from support.functions import FUNCTIONS
from support.services import ServiceEnv

from graph_cli.access_control.policy import BUILTIN_POLICY, policy_set
from graph_cli.kernel.canonical import JsonObject, JsonValue, hash_value
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ports.catalog import FunctionsSlot, functions_hash
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.kernel.records import RequestMeta
from graph_cli.kinetic.compile import compile_catalog
from graph_cli.services.read import GraphRead, ReadService

REQUEST = RequestMeta("run", "sha256:" + "0" * 64)
FUNCTION_SOURCES = FUNCTIONS.parent / "inventory" / "valid"
ACTION_SOURCES = FIXTURES / "actions" / "inventory" / "valid"
HIDDEN = ("Location", "located_in", "Category", "in_category", "items_in_location", "move_item")


class Source:
    """A policy source with one fixed policy."""

    def __init__(self, policy: PolicySet) -> None:
        self.policy = policy

    def current(self, graph: object) -> PolicySet:
        return self.policy

    def refresh(self) -> None:
        pass


def policy(svc: ServiceEnv, *rules: tuple[str, str, str]) -> Source:
    """The builtin policy plus a deny rule per ``(action, type, id)``."""
    denies: list[JsonValue] = [
        {
            "id": rule_id,
            "effect": "deny",
            "action": [action],
            "resource": f"graph:{svc.entry.id}/type:{type_curie}",
        }
        for action, type_curie, rule_id in rules
    ]
    document = {**BUILTIN_POLICY, "rules": [*BUILTIN_POLICY["rules"], *denies], "obligations": []}  # type: ignore[misc]
    return Source(policy_set(document))


def hiding(svc: ServiceEnv, *types: str) -> Source:
    """Types the reader may not see."""
    return policy(svc, *((("read.get", t, f"hide_{n}") for n, t in enumerate(types))))


class CatalogWithFunctions:
    """The env's catalog plus a published function catalog (see ``test_run_function``)."""

    def __init__(self, inner: Any, slot: FunctionsSlot) -> None:
        self._inner = inner
        self._slot = slot

    def read_functions(self, graph: Any, *, sources: bool = False) -> FunctionsSlot:
        return self._slot

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@pytest.fixture(params=("inventory",))
def ontology(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def svc(backend: str, ontology: str, tmp_path: Path) -> ServiceEnv:
    env = ServiceEnv(backend, ontology, tmp_path / "root")
    assert env.apply_seed().error is None
    publish_actions(env, read_dir(ACTION_SOURCES))
    sources = {p.name: p.read_text() for p in sorted(FUNCTION_SOURCES.glob("*.yaml"))}
    compiled, problems = compile_catalog(sources, env.model)
    assert problems == []
    files = {name: text.encode() for name, text in sources.items()}
    slot = FunctionsSlot(functions_hash(files), hash_value(compiled), env.entry.pin, compiled, {})
    env.catalog = CatalogWithFunctions(env.catalog, slot)  # type: ignore[assignment]
    return env


def names(entries: Any) -> list[str]:
    return [e.name for e in entries or ()]


def open_view(svc: ServiceEnv, reader: ReadService) -> GraphRead:
    return reader.open(svc.actor, svc.entry, "read.function", REQUEST)


def raised(call: Any, *args: Any, **kw: Any) -> tuple[str, str, dict[str, Any]]:
    with pytest.raises(GraphCliError) as info:
        call(*args, **kw)
    return info.value.code, info.value.message, dict(info.value.details)


def the_same_but_for(asked: Any, absent: Any, hidden: str, other: str) -> None:
    """Two answers equal once the name asked is the other one (suggestions follow the spelling
    of the name asked, so they are compared on their own)."""
    for answer in (asked, absent):
        answer[2].pop("did_you_mean", None)
    assert json.loads(json.dumps(asked).replace(hidden, other)) == json.loads(json.dumps(absent))


# ---- functions ---------------------------------------------------------------------------


def test_a_reader_lists_and_runs_only_the_functions_that_fit_its_view(svc: ServiceEnv) -> None:
    full = svc.make_reader()
    assert names(full.functions(svc.actor, svc.entry)) == [
        "items_by_condition",
        "items_in_location",
    ]
    limited = svc.make_reader(hiding(svc, "inv:Location", "inv:Category"))
    assert names(limited.functions(svc.actor, svc.entry)) == ["items_by_condition"]
    view = open_view(svc, limited)
    assert view.visible == {"inv:Item"}
    data, page, _ = limited.run(
        view, "items_by_condition", [("condition", "damaged")], limit=50, offset=0
    )
    assert data["function"]["name"] == "items_by_condition"  # type: ignore[index]
    assert page.total == len(data["items"]) > 0  # type: ignore[arg-type]
    # a reader who sees everything on a limited graph keeps every function
    assert names(svc.make_reader(hiding(svc, "inv:Category")).functions(svc.actor, svc.entry)) == [
        "items_by_condition",
        "items_in_location",
    ]


def test_running_a_hidden_function_is_running_one_that_does_not_exist(svc: ServiceEnv) -> None:
    limited = svc.make_reader(hiding(svc, "inv:Location", "inv:Category"))
    view = open_view(svc, limited)
    asked = raised(limited.run, view, "items_in_location", [], limit=5, offset=0)
    absent = raised(limited.run, view, "items_in_nowhere", [], limit=5, offset=0)
    assert asked[0] == "function.not_found"
    the_same_but_for(asked, absent, "items_in_location", "items_in_nowhere")
    # the parameters are not looked at before the name is
    with_params = raised(
        limited.run, view, "items_in_location", [("location", "x")], limit=5, offset=0
    )
    the_same_but_for(with_params, absent, "items_in_location", "items_in_nowhere")
    # a typo is not corrected to the hidden name
    typo = raised(limited.run, view, "items_in_locatio", [], limit=5, offset=0)
    assert "items_in_location" not in typo[2]["did_you_mean"]
    # the unlimited reader has it
    assert "items_in_location" in names(svc.make_reader().functions(svc.actor, svc.entry))


# ---- actions -----------------------------------------------------------------------------


def test_a_reader_lists_and_runs_only_the_actions_that_fit_its_view(svc: ServiceEnv) -> None:
    published = svc.make_reader().published_actions(svc.entry)
    assert published is not None
    full = svc.make_reader()
    assert names(full.visible_actions(svc.actor, svc.entry, published)) == [
        "move_item",
        "retire_item",
    ]
    limited = svc.make_reader(hiding(svc, "inv:Location", "inv:Category"))
    assert names(limited.actions(svc.actor, svc.entry)) == ["retire_item"]
    assert names(limited.visible_actions(svc.actor, svc.entry, published)) == ["retire_item"]
    # the unlimited listing of the same reader, as the name check of a curated skill needs it
    assert names(limited.visible_actions(svc.actor, svc.entry, published, limits=False)) == [
        "move_item",
        "retire_item",
    ]


def test_doing_a_hidden_action_is_doing_one_that_does_not_exist(svc: ServiceEnv) -> None:
    source = hiding(svc, "inv:Location", "inv:Category")
    service = action_service(svc, source)
    asked = raised(do, service, svc, "move_item", "item=x", "location=y")
    absent = raised(do, service, svc, "move_nothing", "item=x", "location=y")
    assert asked[0] == "action.not_found"
    the_same_but_for(asked, absent, "move_item", "move_nothing")
    typo = raised(do, service, svc, "move_ite", "item=x")
    assert "move_item" not in typo[2]["did_you_mean"]
    # an action that fits runs
    item = next(r for r in svc.all_revisions() if r.type == "inv:Item")
    result = do(service, svc, "retire_item", f"item={item.id}", dry_run=True, reason="test")
    assert result.action.name == "retire_item"
    # and the unlimited reader may move an item: it sees the action
    assert names(svc.make_reader().actions(svc.actor, svc.entry)) == ["move_item", "retire_item"]


# ---- the skill documents -----------------------------------------------------------------


def catalog_names(model: JsonObject, key: str) -> list[str]:
    document = model[key]
    assert isinstance(document, dict)
    return [e["name"] for e in document[key]]  # type: ignore[index,union-attr]


def test_the_skill_documents_of_a_limited_reader_hold_nothing_hidden(svc: ServiceEnv) -> None:
    full, _ = svc.make_reader().skill_model(svc.actor, svc.entry, REQUEST)
    assert catalog_names(full, "functions") == ["items_by_condition", "items_in_location"]
    assert catalog_names(full, "actions") == ["move_item", "retire_item"]
    limited = svc.make_reader(hiding(svc, "inv:Location", "inv:Category"))
    model, _ = limited.skill_model(svc.actor, svc.entry, REQUEST)
    assert catalog_names(model, "functions") == ["items_by_condition"]
    assert catalog_names(model, "actions") == ["retire_item"]
    text = json.dumps(model)
    for name in HIDDEN:
        assert name not in text, name
    assert "inv:Item" in text


def test_a_reader_left_without_functions_or_actions_gets_a_graph_without_them(
    svc: ServiceEnv,
) -> None:
    model, _ = svc.make_reader(hiding(svc, "inv:Item")).skill_model(svc.actor, svc.entry, REQUEST)
    assert "functions" not in model and "actions" not in model


def test_the_names_check_sees_the_full_model_and_only_for_a_limited_reader(
    svc: ServiceEnv,
) -> None:
    assert svc.make_reader().skill_names_model(svc.actor, svc.entry, REQUEST) is None
    limited = svc.make_reader(hiding(svc, "inv:Location", "inv:Category"))
    full, _ = svc.make_reader().skill_model(svc.actor, svc.entry, REQUEST)
    names_model = limited.skill_names_model(svc.actor, svc.entry, REQUEST)
    assert names_model == full  # every type, function and action the reader's rights allow


def test_the_digest_follows_what_the_reader_sees(svc: ServiceEnv) -> None:
    assert svc.make_reader().skill_digest(svc.actor, svc.entry) is None
    assert svc.make_reader(hiding(svc, "inv:Category")).skill_digest(svc.actor, svc.entry)
    digests = {
        svc.make_reader(hiding(svc, *types)).skill_digest(svc.actor, svc.entry)
        for types in (
            ("inv:Category",),
            ("inv:Location",),
            ("inv:Category", "inv:Location"),
            ("inv:Category", "inv:Location", "inv:Item"),
        )
    }
    assert len(digests) == 4 and None not in digests
    same = svc.make_reader(hiding(svc, "inv:Location", "inv:Category"))
    again = svc.make_reader(hiding(svc, "inv:Category", "inv:Location"))
    assert same.skill_digest(svc.actor, svc.entry) == again.skill_digest(svc.actor, svc.entry)


# ---- needs -------------------------------------------------------------------------------


@pytest.fixture
def company(backend: str, tmp_path: Path) -> ServiceEnv:
    env = ServiceEnv(backend, "company", tmp_path / "root", example_version="1.1.0")
    publish_actions(env, valid_sources())
    return env


def listed(svc: ServiceEnv, source: Source | None) -> list[str]:
    reader = svc.make_reader(source)
    published = reader.published_actions(svc.entry)
    assert published is not None
    return names(reader.visible_actions(svc.actor, svc.entry, published))


def test_a_need_is_met_only_by_a_type_the_reader_sees(company: ServiceEnv) -> None:
    """``hand_over_position`` and ``assign_position`` link a ``ppl:Person``: an employee or an
    external. Writing externals is denied, so only the employee meets the need."""
    svc = company
    no_externals = ("write.*", "ppl:External", "no_external_writes")
    allowed = listed(svc, policy(svc, no_externals))
    assert {"assign_position", "hand_over_position"} <= set(allowed)
    # employees hidden: the only option left is one the reader does not see
    hide_employees = ("read.get", "ppl:Employee", "hide_employees")
    hidden = listed(svc, policy(svc, no_externals, hide_employees))
    assert "assign_position" not in hidden and "hand_over_position" not in hidden
    assert "create_task" in hidden  # a need on a visible type is met as before
    # externals writable: the visible option meets the need
    only_hidden = listed(svc, policy(svc, hide_employees))
    assert {"assign_position", "hand_over_position"} <= set(only_hidden)
    # a reader without read limits is unaffected
    assert listed(svc, policy(svc, no_externals)) == allowed
    assert set(allowed) - set(hidden) >= {"assign_position", "hand_over_position"}


def test_an_action_hidden_by_its_needs_is_not_found_by_do(company: ServiceEnv) -> None:
    svc = company
    source = policy(
        svc, ("write.*", "ppl:External", "no_external_writes"), ("read.get", "ppl:Employee", "h")
    )
    service = action_service(svc, source)
    asked = raised(do, service, svc, "hand_over_position", "position=a", "person=b")
    absent = raised(do, service, svc, "hand_over_nothing", "position=a", "person=b")
    assert asked[0] == "action.not_found"
    the_same_but_for(asked, absent, "hand_over_position", "hand_over_nothing")
    # an action that names the hidden employee type is not listed or described at all
    assert "offboard_employee" not in listed(svc, source)
