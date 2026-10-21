"""Read services and describe on both backends x both fixtures over the seeds (plan A4, A5).

Contracts §4.3, B2.10, B6.3, B7.6, B8.8, B8.12, B8.13, B9.7, B11.5.
"""

from pathlib import Path
from typing import Any

import pytest
from support.roles import Roles, load_roles
from support.services import RecordingActivity, ServiceEnv
from support.trees import tree_hash

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.kernel.query_ast import Query
from graph_cli.kernel.records import RequestMeta
from graph_cli.query.lookup import LookupSpec
from graph_cli.query.plan import FindSpec
from graph_cli.services import describe
from graph_cli.services.read import GraphRead, ReadService, refuse_as_of

REQUEST = RequestMeta("get", "sha256:" + "0" * 64)
ALL = frozenset({"active", "superseded", "retracted"})


@pytest.fixture
def svc(backend: str, ontology: str, tmp_path: Path) -> ServiceEnv:
    env = ServiceEnv(backend, ontology, tmp_path / "root")
    assert env.apply_seed().error is None
    return env


@pytest.fixture
def roles(ontology: str) -> Roles:
    return load_roles(ontology)


def open_view(svc: ServiceEnv, reader: ReadService | None = None) -> GraphRead:
    reader = reader or svc.make_reader()
    return reader.open(svc.actor, svc.catalog.resolve(svc.name), "read.get", REQUEST)


def by_status(svc: ServiceEnv, status: str) -> Any:
    query = Query(record_status=frozenset({status}), limit=10**6)
    return svc.snapshot().query(query).items[0]


def test_get_by_id_alias_and_abbreviation(svc: ServiceEnv) -> None:
    reader = svc.make_reader()
    view = open_view(svc, reader)
    entity = next(r for r in svc.all_revisions() if r.alias and r.record_status == "active")
    for ref in (str(entity.id), entity.alias, entity.id.hex[-12:]):
        data, warnings = reader.get(view, ref)
        assert data["entity"]["id"] == str(entity.id)  # type: ignore[index]
        assert warnings == [] and data["superseded_by"] == []
    assert view.envelope_graph()["seq"] == svc.head_seq()
    with pytest.raises(GraphCliError) as info:
        reader.get(view, "0192f3c0-11aa-7e01-b2c3-d4e5f6a7b8c9")
    assert info.value.code == "entity.not_found"
    with pytest.raises(GraphCliError) as info:
        reader.get(view, "not a ref")
    assert info.value.code == "usage.invalid_argument"


def test_superseded_and_retracted_reads_warn_without_redirect(svc: ServiceEnv) -> None:
    reader = svc.make_reader()
    view = open_view(svc, reader)
    old = by_status(svc, "superseded")
    data, warnings = reader.get(view, str(old.id))
    assert data["entity"]["record_status"] == "superseded"  # type: ignore[index]
    assert data["superseded_by"] and [w.code for w in warnings] == ["entity.superseded"]
    gone = by_status(svc, "retracted")
    data, warnings = reader.get(view, str(gone.id))
    assert [w.code for w in warnings] == ["entity.retracted"]
    assert data["entity"]["id"] == str(gone.id)  # type: ignore[index]


def test_find_pages_and_filters(svc: ServiceEnv, roles: Roles) -> None:
    reader = svc.make_reader()
    view = open_view(svc, reader)
    cls = roles.term("lifecycle_type")
    _, page, _ = reader.find(view, FindSpec(types=(cls,), limit=2))
    assert page.shown == 2 and page.total > 2
    assert page.to_json()["truncated"] is True and page.to_json()["next_offset"] == 2
    _, every, _ = reader.find(view, FindSpec(types=(cls,), include_inactive=True, limit=500))
    assert every.total > page.total  # superseded and retracted ones too
    rest, last, _ = reader.find(view, FindSpec(types=(cls,), limit=500, offset=2))
    assert last.shown == page.total - 2 and last.to_json()["next_offset"] is None
    enum_attr = roles.term("enum_attribute").rpartition(".")[2]
    _, filtered, _ = reader.find(view, FindSpec(types=(cls,), where=(f"{enum_attr}:exists=true",)))
    assert 0 < filtered.total <= page.total
    rel = roles.term("required_relation")
    _, related, _ = reader.find(view, FindSpec(types=(cls,), limit=500), rel_refs=[(rel, ())])
    assert related.total == page.total  # every active one has its required relation
    assert all(item["type"] for item in rest["items"])  # type: ignore[index, union-attr]


def test_neighbors(svc: ServiceEnv, roles: Roles) -> None:
    reader = svc.make_reader()
    view = open_view(svc, reader)
    rel = roles.term("required_relation")
    source = next(r for r in svc.all_revisions() if any(e.rel == rel for e in r.edges))
    data, _ = reader.neighbors(view, str(source.id), rels=[rel], direction="out")
    items = data["items"]
    assert isinstance(items, list) and items
    assert all(i["direction"] == "out" for i in items)  # type: ignore[index]
    deep, _ = reader.neighbors(view, str(source.id), depth=3)
    assert len(deep["items"]) >= len(items)


def test_as_of_is_refused() -> None:
    with pytest.raises(GraphCliError) as info:
        refuse_as_of("tx:3")
    assert info.value.code == "temporal.as_of_unsupported"
    refuse_as_of(None)


def test_pin_mismatch_and_archived_read_with_warnings(svc: ServiceEnv) -> None:
    svc.tamper_artifact()
    svc.set_entry(state="archived")
    view = open_view(svc)
    assert sorted(w.code for w in view.warnings) == [
        "graph.archived_read",
        "ontology.pin_mismatch_read",
    ]
    assert svc.make_reader().find(view, FindSpec())[1].total > 0


def test_reads_write_nothing(svc: ServiceEnv, roles: Roles, tmp_path: Path) -> None:
    before = (tree_hash(tmp_path), len(svc.installation_events()))
    reader = svc.make_reader()
    view = open_view(svc, reader)
    reader.find(view, FindSpec(types=(roles.term("lifecycle_type"),)))
    reader.graphs(svc.actor, REQUEST, allowed=None)
    describe.describe_types(view.entry, view.model)
    assert (tree_hash(tmp_path), len(svc.installation_events())) == before


def _policy(*rules: dict[str, JsonValue]) -> object:
    document: dict[str, JsonValue] = {
        "schema": "graph_cli.policy/1",
        "id": "test",
        "rules": list(rules),
        "obligations": [],
    }
    policy = PolicySet("sha256:" + "1" * 64, (document,))

    class Policy:
        def current(self, graph: object) -> PolicySet:
            return policy

    return Policy()


def test_denied_read_is_audited(svc: ServiceEnv) -> None:
    """B6.15: a visible graph gives access.denied with the rule."""
    list_only = _policy(
        {"id": "list", "effect": "allow", "action": ["read.list"], "resource": "graph:*"}
    )
    with pytest.raises(GraphCliError) as info:
        open_view(svc, svc.make_reader(list_only))
    assert info.value.code == "access.denied"
    assert info.value.details["rule"] == "default.deny"
    assert "do not work around it" in info.value.message
    denial = svc.installation_events()[-1]
    assert denial["result"] == {"status": "denied", "code": "access.denied"}


def test_denied_read_on_an_invisible_graph_is_not_found(svc: ServiceEnv) -> None:
    """Q4: an invisible graph reads like a missing one; the denial is still audited."""
    with pytest.raises(GraphCliError) as info:
        open_view(svc, svc.make_reader(_policy()))
    assert info.value.code == "graph.not_found"
    assert svc.name not in info.value.message
    denial = svc.installation_events()[-1]
    assert denial["result"] == {"status": "denied", "code": "access.denied"}
    reader = svc.make_reader(_policy())
    assert reader.graphs(svc.actor, REQUEST, allowed=None)[:2] == ([], 0)  # never counted


def test_graphs_and_whoami(svc: ServiceEnv) -> None:
    reader = svc.make_reader()
    items, hidden, _ = reader.graphs(svc.actor, REQUEST, allowed=None)
    assert [i["name"] for i in items] == [svc.name] and hidden == 0  # type: ignore[index]
    assert reader.graphs(svc.actor, REQUEST, allowed=["other"])[:2] == ([], 1)
    who = reader.whoami(svc.actor)
    permissions = who["permissions"]
    assert isinstance(permissions, dict)
    assert "write.create" in permissions["allowed"]  # type: ignore[operator]
    assert not any(str(a).startswith("maint.") for a in permissions["allowed"])  # type: ignore[union-attr]
    assert permissions["obligations"] == {
        "write.retract": {"require_reason": True},
        "write.supersede": {"require_reason": True},
    }
    assert who["actor"]["client"]["entrypoint"] == "app"  # type: ignore[index]


def test_describe_projections(svc: ServiceEnv, roles: Roles) -> None:
    view = open_view(svc)
    cls = roles.term("lifecycle_type")
    types = describe.describe_types(view.entry, view.model)
    assert types["schema"] == "graph_cli.model/1"
    assert cls in [t["curie"] for t in types["types"]]  # type: ignore[index, union-attr]
    one = describe.describe_type(view.entry, view.model, cls)["type"]
    assert isinstance(one, dict)
    assert one["lifecycles"] and one["fields"]
    assert roles.term("required_relation") in [r["curie"] for r in one["relations_out"]]  # type: ignore[index, union-attr]
    lifecycle = describe.describe_lifecycle(view.entry, view.model, cls)["lifecycles"]
    assert lifecycle == one["lifecycles"]
    assert describe.describe_relations(view.entry, view.model)["relations"]
    assert describe.describe_fields(view.entry, view.model, cls)["fields"] == one["fields"]
    with pytest.raises(GraphCliError) as info:
        describe.describe_type(view.entry, view.model, cls + "x")
    assert info.value.code == "term.unknown" and info.value.details["did_you_mean"]


def test_describe_model_bundles_the_documents(svc: ServiceEnv) -> None:
    """The graph skill input: exactly what the describe commands print, one ``type`` each."""
    view = open_view(svc)
    bundle = describe.describe_model(view.entry, view.model)
    assert bundle["types"] == describe.describe_types(view.entry, view.model)
    assert bundle["relations"] == describe.describe_relations(view.entry, view.model)
    assert bundle["type"] == {
        curie: describe.describe_type(view.entry, view.model, curie)
        for curie in sorted(view.model.classes)
    }


def lookup_ids(reader: ReadService, view: GraphRead, **changes: Any) -> list[str]:
    data, _ = reader.lookup(view, LookupSpec(**changes))
    return [item["id"] for item in data["items"]]  # type: ignore[index, union-attr]


def first_with(svc: ServiceEnv, attribute: str, *, status: str = "active") -> Any:
    curie, _, name = attribute.rpartition(".")
    return next(
        r
        for r in svc.all_revisions()
        if r.record_status == status and r.attrs.get(name) and svc.model.is_a(r.type, curie)
    )


def test_lookup_finds_main_alternative_and_hidden_labels(svc: ServiceEnv, roles: Roles) -> None:
    reader = svc.make_reader()
    view = open_view(svc, reader)
    for role_name, role in (
        ("label_attribute", "label"),
        ("alt_label_attribute", "alt_label"),
        ("hidden_label_attribute", "hidden_label"),
    ):
        attribute = str(roles.get(role_name))
        name = attribute.rpartition(".")[2]
        entity = first_with(svc, attribute)
        value = entity.attrs[name]
        text = (value[0] if isinstance(value, list) else value).swapcase()  # case-insensitive
        data, page = reader.lookup(view, LookupSpec(text=text))
        (item,) = [i for i in data["items"] if i["id"] == str(entity.id)]  # type: ignore[index, union-attr]
        assert item["match"]["role"] == role and item["match"]["kind"] == "exact"  # type: ignore[index]
        assert item["match"]["attr"] == name  # type: ignore[index]
        shown = None if role == "hidden_label" else text.swapcase()
        assert item["match"]["value"] == shown  # type: ignore[index]
        assert item["type"] == entity.type and item["alias"] == entity.alias  # type: ignore[index]
        assert page.total >= 1


def test_lookup_skips_inactive_unless_asked(svc: ServiceEnv, roles: Roles) -> None:
    reader = svc.make_reader()
    view = open_view(svc, reader)
    label_type = str(roles.get("label_type"))
    old = next(
        r
        for r in svc.all_revisions()
        if r.record_status != "active" and view.model.label_attributes(r.type)
    )
    attr = view.model.label_attributes(old.type)[0][0]
    text = str(old.attrs[attr])
    assert str(old.id) not in lookup_ids(reader, view, text=text)
    assert str(old.id) in lookup_ids(reader, view, text=text, include_inactive=True)
    typed, _ = reader.lookup(view, LookupSpec(text=text, types=(label_type,), limit=500))
    assert all(view.model.is_a(i["type"], label_type) for i in typed["items"])  # type: ignore[index, union-attr]


def test_lookup_pages_after_ranking(svc: ServiceEnv, roles: Roles) -> None:
    reader = svc.make_reader()
    view = open_view(svc, reader)
    entity = first_with(svc, str(roles.get("label_attribute")))
    prefix = str(entity.attrs[str(roles.get("label_attribute")).rpartition(".")[2]])[0]
    every = lookup_ids(reader, view, text=prefix, match="prefix", limit=500)
    assert str(entity.id) in every
    data, page = reader.lookup(view, LookupSpec(text=prefix, match="prefix", limit=1, offset=1))
    assert page.total == len(every) and page.shown == min(1, len(every) - 1)
    assert [i["id"] for i in data["items"]] == every[1:2]  # type: ignore[index, union-attr]


def test_lookup_refuses_types_without_labels(svc: ServiceEnv) -> None:
    reader = svc.make_reader()
    view = open_view(svc, reader)
    bare = next(c for c in sorted(view.model.classes) if not view.model.label_attributes(c))
    with pytest.raises(GraphCliError) as info:
        reader.lookup(view, LookupSpec(text="x", types=(bare,)))
    assert info.value.code == "term.no_labels"
    assert info.value.details == {"types": [bare]}


def test_reads_signal_the_ids_they_return(svc: ServiceEnv, roles: Roles) -> None:
    activity = RecordingActivity()
    reader = svc.make_reader(activity=activity)
    view = open_view(svc, reader)
    graph = str(view.entry.id)
    entity = next(r for r in svc.all_revisions() if r.record_status == "active")
    reader.get(view, str(entity.id))
    data, _, _ = reader.find(view, FindSpec(types=(roles.term("lifecycle_type"),), limit=3))
    found = [item["id"] for item in data["items"]]  # type: ignore[index, union-attr]
    labels = [
        r.attrs[a]
        for r in svc.all_revisions()
        if r.record_status == "active"
        for a, role in view.model.label_attributes(r.type)
        if role == "label" and isinstance(r.attrs.get(a), str)
    ]
    shown = (
        [lookup_ids(reader, view, text=str(labels[0]))] if labels else []
    )  # a model may have none
    rel = roles.term("required_relation")
    source = next(r for r in svc.all_revisions() if any(e.rel == rel for e in r.edges))
    near, _ = reader.neighbors(view, str(source.id), rels=[rel], direction="out")
    neighbours = [item["entity"]["id"] for item in near["items"]]  # type: ignore[index, union-attr]
    assert activity.signals == [
        (graph, [str(entity.id)]),
        (graph, found),
        *((graph, ids) for ids in shown),
        (graph, [str(source.id), *neighbours]),
    ]


def test_failed_and_denied_reads_signal_nothing(svc: ServiceEnv) -> None:
    activity = RecordingActivity()
    reader = svc.make_reader(activity=activity)
    view = open_view(svc, reader)
    with pytest.raises(GraphCliError):
        reader.get(view, "0192f3c0-11aa-7e01-b2c3-d4e5f6a7b8c9")
    with pytest.raises(GraphCliError):
        reader.neighbors(view, "0192f3c0-11aa-7e01-b2c3-d4e5f6a7b8c9")
    reader.graphs(svc.actor, REQUEST, allowed=None)
    reader.whoami(svc.actor)
    assert activity.signals == []
