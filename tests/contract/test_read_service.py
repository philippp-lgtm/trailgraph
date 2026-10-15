"""Read services and describe on both backends x both fixtures over the seeds (plan A4, A5).

Contracts §4.3, B2.10, B6.3, B7.6, B8.8, B8.12, B8.13, B9.7, B11.5.
"""

from pathlib import Path
from typing import Any

import pytest
from support.roles import Roles, load_roles
from support.services import ServiceEnv
from support.trees import tree_hash

from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.kernel.query_ast import Query
from graph_cli.kernel.records import RequestMeta
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
    _, related, _ = reader.find(view, FindSpec(types=(cls,), limit=500), rel_refs=[(rel, None)])
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
    assert len(deep["items"]) >= len(items)  # type: ignore[arg-type]


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
    describe.describe_types(view)
    assert (tree_hash(tmp_path), len(svc.installation_events())) == before


def test_denied_read_is_audited(svc: ServiceEnv) -> None:
    deny_all = PolicySet(
        "sha256:" + "1" * 64,
        ({"schema": "graph_cli.policy/1", "id": "none", "rules": [], "obligations": []},),
    )

    class Policy:
        def current(self, graph: object) -> PolicySet:
            return deny_all

    with pytest.raises(GraphCliError) as info:
        open_view(svc, svc.make_reader(Policy()))
    assert info.value.code == "access.denied"
    denial = svc.installation_events()[-1]
    assert denial["result"] == {"status": "denied", "code": "access.denied"}


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
    types = describe.describe_types(view)
    assert types["schema"] == "graph_cli.model/1"
    assert cls in [t["curie"] for t in types["types"]]  # type: ignore[index, union-attr]
    one = describe.describe_type(view, cls)["type"]
    assert isinstance(one, dict)
    assert one["lifecycles"] and one["fields"]
    assert roles.term("required_relation") in [r["curie"] for r in one["relations_out"]]  # type: ignore[index, union-attr]
    lifecycle = describe.describe_lifecycle(view, cls)["lifecycles"]
    assert lifecycle == one["lifecycles"]
    assert describe.describe_relations(view)["relations"]
    assert describe.describe_fields(view, cls)["fields"] == one["fields"]
    with pytest.raises(GraphCliError) as info:
        describe.describe_type(view, cls + "x")
    assert info.value.code == "term.unknown" and info.value.details["did_you_mean"]
