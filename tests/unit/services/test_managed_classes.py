"""Managed classes: derived file metadata is written by its extractor only (file metadata M9).

Run on ``notes_and_sources`` 1.3.0, which imports two kinds of metadata. Records are made the
way an extraction run makes them: a request whose source has the reserved kind ``extractor``.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from support.services import ServiceEnv

from graph_cli.access_control.compiler import compile_policy
from graph_cli.access_control.policy import policy_set
from graph_cli.kernel.canonical import hash_value
from graph_cli.kernel.changeset import parse_changeset
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, parse_id
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli.kernel.records import RequestMeta, SourceRef
from graph_cli.services.files import FileAddRequest
from graph_cli.services.write import WriteRequest, WriteResult

CODE = "data.semantic.managed_class"
PDF = b"%PDF-1.4\n% synthetic\n"


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.env = ServiceEnv("memory", "notes_and_sources", tmp_path / "root", version="1.3.0")
        path = tmp_path / "doc.pdf"
        path.write_bytes(PDF)
        added = self.env.make_files().add(
            FileAddRequest(
                actor=self.env.actor,
                graph=self.env.name,
                path=path,
                request=RequestMeta("file add", hash_value({"p": "x"})),
            )
        )
        self.file = str(added.entity["id"])
        self.record = self.extract(self.create_ops("pdf"))
        self.record_id = EntityId(parse_id(str(self.record.entities[0].id)))

    def create_ops(
        self, extractor: str, local: str = "record", suffix: str = ""
    ) -> list[dict[str, Any]]:
        return [
            {
                "op": "create",
                "type": "files_pdf:PdfMetadata",
                "local": local,
                "attrs": {
                    "key": f"{extractor}/{self.file}{suffix}",
                    "extractor": extractor,
                    "extractor_version": 1,
                    "status": "ok",
                    "page_count": 1,
                },
                "edges": [{"rel": "files:metadata_of", "target": self.file}],
            }
        ]

    def request(
        self,
        ops: list[dict[str, Any]],
        source: SourceRef | None = None,
        dry_run: bool = False,
    ) -> WriteRequest:
        changeset = replace(parse_changeset({"ops": ops, "reason": "test"}), source=source)
        return WriteRequest(
            self.env.actor, self.env.name, changeset, dry_run, RequestMeta("apply", hash_value(ops))
        )

    def extract(self, ops: list[dict[str, Any]], name: str = "pdf") -> WriteResult:
        return self.env.service.apply(self.request(ops, SourceRef(name, "1", None, "extractor")))

    def refused(
        self, ops: list[dict[str, Any]], source: SourceRef | None = None, dry_run: bool = False
    ) -> GraphCliError:
        with pytest.raises(GraphCliError) as info:
            self.env.service.apply(self.request(ops, source, dry_run))
        return info.value

    def findings(self, error: GraphCliError) -> list[dict[str, Any]]:
        assert error.code == "write.invalid", error.message
        return list(error.details["findings"])  # type: ignore[arg-type]


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_an_extractor_writes_its_own_record(world: World) -> None:
    assert world.record.entities[0].type == "files_pdf:PdfMetadata"
    updated = world.extract(
        [{"op": "update", "id": str(world.record_id), "set": {"extractor_version": 2}}]
    )
    assert updated.seq is not None
    retracted = world.extract([{"op": "retract", "id": str(world.record_id)}])
    assert retracted.seq is not None


def test_a_plain_create_is_refused(world: World) -> None:
    ops = world.create_ops("pdf", "other")
    ops[0]["attrs"]["key"] = f"pdf/{world.file}x"
    found = world.findings(world.refused(ops))
    assert [f["code"] for f in found] == [CODE]
    assert found[0]["target"] == {
        "local": "other",
        "path": "ops.0",
        "type": "files_pdf:PdfMetadata",
    }
    assert "extractors" in found[0]["message"]
    assert "graph-admin" not in found[0]["message"]


@pytest.mark.parametrize(
    "op",
    [
        {"op": "update", "set": {"page_count": 9}},
        {"op": "transition", "to": "x"},
        {"op": "retract"},
        {"op": "set_alias", "alias": "REC-0001"},
        {"op": "link", "rel": "core:source", "target": "FILE"},
        {"op": "unlink", "rel": "files:metadata_of", "target": "FILE"},
    ],
    ids=lambda op: str(op["op"]),
)
def test_a_plain_op_on_a_record_is_refused(world: World, op: dict[str, Any]) -> None:
    op = {**op, "id": str(world.record_id)}
    if op.get("target") == "FILE":
        op["target"] = world.file
    found = world.findings(world.refused([op]))
    assert [(f["code"], f["target"]["id"]) for f in found] == [(CODE, str(world.record_id))]


def test_supersede_names_both_sides(world: World) -> None:
    other = world.extract(world.create_ops("pdf", "second", "-2"), "pdf")
    other_id = str(other.entities[0].id)
    ops = [{"op": "supersede", "by": other_id, "targets": [str(world.record_id)]}]
    found = world.findings(world.refused(ops))
    assert sorted(f["target"]["id"] for f in found) == sorted([other_id, str(world.record_id)])


def test_a_source_without_the_extractor_kind_is_refused(world: World) -> None:
    ops = [{"op": "update", "id": str(world.record_id), "set": {"extractor_version": 2}}]
    found = world.findings(world.refused(ops, SourceRef("pdf", "1")))
    assert [f["code"] for f in found] == [CODE]


def test_an_extractor_never_writes_another_extractors_record(world: World) -> None:
    ops = [{"op": "update", "id": str(world.record_id), "set": {"extractor_version": 2}}]
    error = world.refused(ops, SourceRef("sqlite", "1", None, "extractor"))
    assert [f["code"] for f in world.findings(error)] == [CODE]


def test_an_extractor_cannot_create_under_another_name(world: World) -> None:
    ops = world.create_ops("sqlite", "alien")
    error = world.refused(ops, SourceRef("pdf", "1", None, "extractor"))
    assert [f["code"] for f in world.findings(error)] == [CODE]


def test_a_name_that_only_starts_like_the_extractor_does_not_own(world: World) -> None:
    """``pdf`` owns ``pdf/...``, not ``pdfx/...``."""
    ops = world.create_ops("pdfx", "alien")
    error = world.refused(ops, SourceRef("pdf", "1", None, "extractor"))
    assert [f["code"] for f in world.findings(error)] == [CODE]


def test_an_edge_from_someone_elses_entity_to_a_record_is_not_a_write_to_it(world: World) -> None:
    """The edge lives in the revision of its source, here the file: people may point at records."""
    linked = world.env.apply(
        {"op": "link", "id": world.file, "rel": "core:source", "target": str(world.record_id)}
    )
    assert linked.seq is not None


def test_the_refusal_is_audited_and_names_the_record(world: World) -> None:
    before = len(world.env.graph_events())
    world.refused([{"op": "retract", "id": str(world.record_id)}])
    events = world.env.graph_events()
    assert len(events) == before + 1
    assert events[-1]["result"]["status"] == "invalid"


def test_a_dry_run_is_refused_too(world: World) -> None:
    ops = [{"op": "retract", "id": str(world.record_id)}]
    found = world.findings(world.refused(ops, dry_run=True))
    assert [f["code"] for f in found] == [CODE]


def test_a_graph_without_the_metadata_classes_is_not_affected(tmp_path: Path) -> None:
    env = ServiceEnv("memory", "inventory", tmp_path / "root")
    assert env.create("inv:Location", "a").seq is not None


def _reconcile(world: World) -> dict[str, Any]:
    return {
        "reason": "test",
        "source": {"name": "pdf", "version": "1"},
        "reconcile": [
            {
                "type": "files_pdf:PdfMetadata",
                "key": "key",
                "under": world.file,
                "fields": ["extractor", "extractor_version", "page_count", "status"],
                "edges": ["files:metadata_of"],
                "items": [
                    {
                        "key": f"{world.file}-3",
                        "attrs": {
                            "extractor": "pdf",
                            "extractor_version": 1,
                            "status": "ok",
                            "page_count": 2,
                        },
                        "edges": [{"rel": "files:metadata_of", "target": world.file}],
                    }
                ],
            }
        ],
    }


def _reconcile_request(world: World, kind: str | None, dry_run: bool) -> WriteRequest:
    document = _reconcile(world)
    changeset = parse_changeset(document)
    assert changeset.source is not None
    changeset = replace(changeset, source=replace(changeset.source, kind=kind))
    return WriteRequest(
        world.env.actor,
        world.env.name,
        changeset,
        dry_run,
        RequestMeta("apply", hash_value(document)),
    )


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_reconcile_document_of_a_plain_source_into_a_record_scope_is_refused(
    world: World, dry_run: bool
) -> None:
    """The expanded ops are what is judged: the planner's creates name the managed class."""
    with pytest.raises(GraphCliError) as info:
        world.env.service.apply(_reconcile_request(world, None, dry_run))
    assert [f["code"] for f in world.findings(info.value)] == [CODE]


def test_the_same_document_of_an_extraction_run_is_written(world: World) -> None:
    result = world.env.service.apply(_reconcile_request(world, "extractor", False))
    assert result.seq is not None
    assert result.reconcile is not None


def test_a_key_given_as_text_or_missing_or_not_a_string_is_not_owned(world: World) -> None:
    source = SourceRef("pdf", "1", None, "extractor")
    for change in (
        lambda op: (op["attrs"].pop("key"), op.update(attrs_text={"key": f"pdf/{world.file}t"})),
        lambda op: op["attrs"].pop("key"),
        lambda op: op["attrs"].update(key=5),
    ):
        ops = world.create_ops("pdf", "alien")
        change(ops[0])
        found = world.findings(world.refused(ops, source))
        assert [f["code"] for f in found] == [CODE]


def test_a_new_record_must_carry_its_extractors_name(world: World) -> None:
    ops = world.create_ops("pdf", "alien", "-3")
    ops[0]["attrs"]["extractor"] = "sqlite"
    error = world.refused(ops, SourceRef("pdf", "1", None, "extractor"))
    assert [f["code"] for f in world.findings(error)] == [CODE]
    ops[0]["attrs"]["extractor"] = "pdf"
    assert world.extract(ops).seq is not None


def test_the_source_the_tx_records_decides_not_the_one_of_the_change_set(world: World) -> None:
    """``request.source`` is what the audit keeps; a change set that claims the kind does not
    make a request an extraction run."""
    ops = [{"op": "update", "id": str(world.record_id), "set": {"extractor_version": 2}}]
    changeset = replace(
        parse_changeset({"ops": ops, "reason": "test"}),
        source=SourceRef("pdf", "1", None, "extractor"),
    )
    meta = RequestMeta("apply", hash_value(ops), source=SourceRef("pdf", "1"))
    with pytest.raises(GraphCliError) as info:
        world.env.service.apply(
            WriteRequest(world.env.actor, world.env.name, changeset, False, meta)
        )
    assert [f["code"] for f in world.findings(info.value)] == [CODE]


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_reconcile_scope_over_a_record_type_is_refused_before_planning(
    world: World, dry_run: bool
) -> None:
    """A plain source, a scope whose plan has no op: only the scope's type can refuse it."""
    document = {
        "reason": "test",
        "source": {"name": "format", "version": "1"},
        "reconcile": [
            {
                "type": "files:FormatMetadata",
                "key": "key",
                "under": world.file,
                "fields": ["extractor"],
                "items": [],
            }
        ],
    }
    request = WriteRequest(
        world.env.actor,
        world.env.name,
        parse_changeset(document),
        dry_run,
        RequestMeta("apply", hash_value(document)),
    )
    with pytest.raises(GraphCliError) as info:
        world.env.service.apply(request)
    found = world.findings(info.value)
    assert [(f["code"], f["target"]) for f in found] == [
        (CODE, {"type": "files:FormatMetadata", "path": "reconcile[0]"})
    ]


LIMITED = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {entrypoint: app, graphs: [example_graph], level: write}
no_project: {"*": write}
"""
HIDE_SQLITE = """\
schema: graph_cli.policy/1
id: graph
types:
  "files_sqlite:SqliteMetadata": none
  "files_sqlite:SqliteObject": none
"""


class Fixed:
    def __init__(self, policy: PolicySet) -> None:
        self.policy = policy

    def current(self, graph: object) -> PolicySet:
        return self.policy

    def refresh(self) -> None:
        pass


def test_a_caller_that_does_not_see_every_type_gets_the_same_refusal(world: World) -> None:
    graph = {"example_graph": world.env.entry.id}
    base = compile_policy(LIMITED.encode(), file="installation.yaml", graphs=graph).document
    layer = compile_policy(
        HIDE_SQLITE.encode(),
        file="graph.yaml",
        graphs=graph,
        graph=world.env.entry.id,
        roles=frozenset(),
    ).document
    limited = world.env.make_service(Fixed(policy_set(base, layer)))  # type: ignore[arg-type]
    ops = [{"op": "update", "id": str(world.record_id), "set": {"page_count": 9}}]
    request = world.request(ops)
    with pytest.raises(GraphCliError) as info:
        limited.apply(request)
    assert [f["code"] for f in world.findings(info.value)] == [CODE]
    created = world.create_ops("pdf", "alien", "-4")
    with pytest.raises(GraphCliError) as info:
        limited.apply(world.request(created))
    assert [f["code"] for f in world.findings(info.value)] == [CODE]
