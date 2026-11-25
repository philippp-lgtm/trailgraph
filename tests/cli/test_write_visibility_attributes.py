"""S: writes that name a hidden attribute, through the local app (field visibility plan F6, R6;
every case and finding kind is covered in process in
``tests/unit/services/test_write_visibility_attributes.py``).

One data root holds ``example_graph`` (the ``staff`` module) and ``twin_graph`` (its write twin:
the module without what a caller holding no marking may see, plus a marked attribute of its own,
so the caller has an attribute view of both), written with the same seeded ids. Without an
installation policy the local caller holds no marking: every write it makes answers the same in
both graphs, though ``stf:Employee`` hides the attribute in one and lacks it in the other.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init, write_documents
from support.app import app
from support.files import write_pdf
from support.markings import HOLDER, STAFF, documents, leaks, write_twin
from support.read_limits import install

from graph_cli.catalog.file_catalog import FileCatalog
from graph_cli.kernel.ids import EntityId
from graph_cli.storage.files.store import FileStore

GRAPH = {"GRAPH_CLI_GRAPH": "example_graph"}
TWIN = {"GRAPH_CLI_GRAPH": "twin_graph"}
E1, E2 = "EMP-0001", "EMP-0002"


@dataclass
class World:
    run_cli: CliRunner
    root: Path
    work: Path
    ids: dict[str, str]

    def ask(self, graph: dict[str, str], *args: str) -> Outcome:
        return app(self.run_cli, *args, env=graph)

    def answer(self, outcome: Outcome, graph: str) -> Any:
        body = {"code": outcome.code, "data": outcome.data, "error": outcome.envelope.get("error")}
        body["warnings"] = outcome.warnings
        text = json.dumps(body, sort_keys=True).replace(self.ids[graph], self.ids["example_graph"])
        return json.loads(text.replace(graph, "example_graph"))

    def same(self, *args: str) -> Outcome:
        """Ask both graphs: the same answers but for the ids a committed write mints (they are
        not seeded here), and nothing hidden in them."""
        marked, twin = self.ask(GRAPH, *args), self.ask(TWIN, *args)
        got, want = minted(self.answer(marked, "example_graph"), self.answer(twin, "twin_graph"))
        assert got == want, json.dumps({"args": args, "marked": got, "twin": want}, indent=1)
        found = [t for t in leaks(marked.envelope) if not any(t in arg for arg in args)]
        assert found == [], (args, found)
        return marked

    def raw(self, alias: str) -> dict[str, Any]:
        """The attributes an entity of ``example_graph`` holds in the store."""
        graph = FileCatalog(self.root).resolve("example_graph").id
        snapshot = FileStore(self.root / "graphs" / str(graph), graph).snapshot()
        found = snapshot.resolve_alias(alias)
        assert found is not None
        revision = snapshot.get(EntityId(UUID(str(found[0]))))
        assert revision is not None
        return dict(revision.attrs)


MINTED = frozenset({"tx_id", "event_id", "id"})


def minted(a: Any, b: Any) -> tuple[Any, Any]:
    """``a`` and ``b`` with the minted ids where they differ (keys ``MINTED``) made equal."""
    if isinstance(a, dict) and isinstance(b, dict):
        pairs = {
            k: ("<minted>", "<minted>")
            if k in MINTED and isinstance(a[k], str) and k in b and a[k] != b[k]
            else minted(a[k], b.get(k))
            for k in a
        }
        return {k: v[0] for k, v in pairs.items()}, {**b, **{k: v[1] for k, v in pairs.items()}}
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        found = [minted(x, y) for x, y in zip(a, b, strict=True)]
        return [x for x, _ in found], [y for _, y in found]
    return a, b


@pytest.fixture
def world(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> World:
    root = init(run_cli, isolated_env)
    ids = {}
    twin = write_twin(tmp_path / "twin")
    for name, source in (("example_graph", STAFF / "1.0.0"), ("twin_graph", twin)):
        created = admin(run_cli, "graph", "create", name, "--ontology", str(source))
        assert created.code == 0, created.envelope
        ids[name] = created.data["id"]
    write_documents(root, "example_graph", documents())
    write_documents(root, "twin_graph", documents(twin=True))
    return World(run_cli, root, tmp_path / "policies", ids)


@pytest.mark.doc_claim("security_model.field_visibility_complete")
def test_a_write_naming_a_hidden_attribute_answers_as_over_the_twin(world: World) -> None:
    before = world.raw(E1)
    for args in (
        ("update", E1, "--set", "weekly_hours=31"),
        ("update", E1, "--set", "email=ada@new.example"),  # its current value
        ("update", E1, "--unset", "email"),  # present
        ("update", E2, "--unset", "pay_grade"),  # absent
        ("update", E1, "--set", "phone=+1 555 0100", "--set", "salary=1.00"),
        ("create", "stf:Employee", "--set", "name=Zed", "--set", "birth_date=2000-01-01"),
    ):
        for extra in ((), ("--dry-run",)):
            outcome = world.same(*args, *extra, "--reason", "test")
            assert outcome.error["code"] == "write.invalid", outcome.envelope
            (finding,) = outcome.error["details"]["findings"]
            assert finding["code"] == "data.structural.unknown_attribute"
    assert world.raw(E1) == before


def test_an_update_keeps_the_hidden_values(world: World) -> None:
    before = world.raw(E1)
    outcome = world.same("update", E1, "--set", "phone=+1 555 0100", "--reason", "test")
    assert outcome.code == 0, outcome.envelope
    assert world.raw(E1) == {**before, "phone": "+1 555 0100"}


def test_file_add_of_a_hidden_attribute(world: World) -> None:
    """A file whose content the graph already holds is ``file.already_present``, whatever the
    request sets; a new one naming a hidden attribute is refused as for an undeclared one."""
    pdf = write_pdf(world.work.parent / "files" / "contract.pdf", "contract")
    add = ("file", "add", str(pdf), "--type", "stf:PersonnelFile", "--no-origin")
    refused = world.same(*add, "--set", "remark=Confidential")
    assert refused.error["code"] == "write.invalid", refused.envelope
    added = world.same(*add)
    assert added.code == 0, added.envelope
    for name in ("remark", "undeclared"):
        again = world.same(*add, "--set", f"{name}=Confidential")
        assert again.code == 0 and again.warnings[0]["code"] == "file.already_present", again


def test_a_holder_writes_the_hidden_attribute(world: World) -> None:
    install(world.run_cli, world.work, HOLDER)
    outcome = world.ask(GRAPH, "update", E1, "--set", "weekly_hours=31", "--reason", "test")
    assert outcome.code == 0, outcome.envelope
    assert world.raw(E1)["weekly_hours"] == 31
