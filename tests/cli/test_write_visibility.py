"""S: writes of a caller who does not see every type, through the local app (type read limits
plan V6; every kind of finding is covered in process in ``test_write_visibility.py`` of the unit
services).

The graph is the toy ontology ``notes_and_sources``: notes about topics that cite web pages and
books, stored files. Two callers do not see topics, books and plain files:

- the *writer*: a grant that reads notes, web pages and scans and may write them (``writes``);
- the *editor*: a grant of level ``write`` to which the graph layer makes topics and books ``none``
  or denies ``read.get`` on them.

A hidden type answers a write as a type the model lacks, whatever kind of caller asks; a hidden
entity answers as a missing one; a hidden edge survives an update; the files lookup does not name
a hidden file. Refusals are audited with the real decisions.
"""

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from harness import CliRunner
from support.admin import Outcome, admin, init, write_documents
from support.app import app, create_graph
from support.files import write_pdf
from support.policies import write_policy
from support.read_limits import NOT_THERE, install, shown, the_same_but_for

from graph_cli.audit.verify import verify_stream
from graph_cli.kernel.ids import EntityId, GraphId
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM, AuditStream, graph_stream
from graph_cli.storage.files.audit_log import FileAuditLog
from graph_cli.storage.files.store import FileStore

ENV = {"GRAPH_CLI_GRAPH": "example_graph"}
WRITER = """\
schema: graph_cli.policy/1
id: installation
grants:
  - entrypoint: app
    graphs: [example_graph]
    level: read
    reads: [nas:Note, nas:WebPage, nas:Scan, provenance:Claim]
    writes:
      nas:Note: {create: true, update: true, link: true, unlink: true, retract: true, alias: true}
      nas:WebPage: {create: true, update: true}
      nas:Scan: {create: true}
no_project: {"*": write}
"""
EDITOR = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {entrypoint: app, graphs: [example_graph], level: write}
no_project: {"*": write}
"""
NONE = 'schema: graph_cli.policy/1\nid: graph\ntypes:\n  "nas:Topic": none\n  "nas:Book": none\n'
DENY = """\
schema: graph_cli.policy/1
id: graph
deny:
  - {id: hide_topics, action: [read.get], type: [nas:Topic, nas:Book]}
"""
HIDDEN = ("nas:Topic", "nas:Book")
BUILD = {
    "ops": [
        {"op": "create", "type": "nas:Topic", "local": "t1", "attrs": {"label": "Alpha topic"}},
        {"op": "create", "type": "nas:Topic", "local": "t2", "attrs": {"label": "Beta topic"}},
        {
            "op": "create",
            "type": "nas:WebPage",
            "local": "w",
            "attrs": {"title": "A page", "url": "https://example.org/p"},
        },
        {"op": "create", "type": "nas:Book", "local": "b", "attrs": {"title": "A book"}},
        {
            "op": "create",
            "type": "nas:Note",
            "local": "n",
            "attrs": {"title": "First note", "body": "text", "kind": "summary"},
            "edges": [
                {"rel": "nas:about", "target": "@t1"},
                {"rel": "nas:cites", "target": "@w"},
                {"rel": "nas:cites", "target": "@b"},
            ],
        },
    ]
}


class World:
    def __init__(self, run_cli: CliRunner, root: Path, work: Path, scans: Path) -> None:
        self.run_cli, self.root, self.work, self.scans = run_cli, root, work, scans
        self.ids: dict[str, str] = {}
        self.aliases: dict[str, str] = {}

    def ask(self, *args: str) -> Outcome:
        return app(self.run_cli, *args, env=ENV)

    def ok(self, *args: str) -> Outcome:
        outcome = self.ask(*args)
        assert outcome.code == 0, (args, outcome.envelope)
        return outcome

    def apply(self, *documents: dict[str, Any], dry_run: bool = False) -> Outcome:
        text = "\n".join(json.dumps(d) for d in documents)
        flags = ["--dry-run"] if dry_run else []
        return app(self.run_cli, "apply", "-", *flags, env=ENV, input=text)

    def create(self, type_: str, *, dry_run: bool = False, **attrs: Any) -> Outcome:
        op = {"op": "create", "type": type_, "attrs": attrs}
        return self.apply({"ops": [op], "reason": "test"}, dry_run=dry_run)

    def install(self, text: str) -> None:
        install(self.run_cli, self.work, text)

    def layer(self, text: str) -> None:
        path = write_policy(
            self.work, f"graph_{hashlib.sha256(text.encode()).hexdigest()[:8]}", text
        )
        applied = admin(
            self.run_cli, "policy", "apply", str(path), "--graph", "example_graph", "--reason", "t"
        )
        assert applied.code == 0, applied.envelope

    def store(self) -> FileStore:
        (graph,) = list((self.root / "graphs").iterdir())
        return FileStore(graph, GraphId(UUID(graph.name)))

    def raw_edges(self, local: str) -> list[str]:
        """The targets of the edges an entity holds in the store, as locals."""
        names = {entity: name for name, entity in self.ids.items()}
        revision = self.store().snapshot().get(EntityId(UUID(self.ids[local])))
        assert revision is not None
        return sorted(f"{e.rel}>{names[str(e.target)]}" for e in revision.edges)

    def events(self, stream: AuditStream) -> list[dict[str, Any]]:
        report = verify_stream(FileAuditLog(self.root).read_lines(stream), stream=stream)
        assert report.findings == ()
        return [dict(event) for event in report.events]

    def installation_events(self) -> list[dict[str, Any]]:
        return self.events(INSTALLATION_STREAM)

    def graph_events(self) -> list[dict[str, Any]]:
        return self.events(graph_stream(str(self.store().graph_id)))


@pytest.fixture
def world(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> World:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "notes_and_sources", version="1.1.0")
    config = Path(isolated_env["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.write_text(config.read_text().rstrip("\n") + '\n\n[roots]\nnotes = "~/notes"\n')
    scans = Path(isolated_env["HOME"]) / "notes"
    write_documents(root, "example_graph", [BUILD])
    found = World(run_cli, root, tmp_path / "policies", scans)
    for name, kind in (("plain", None), ("scan", "nas:Scan")):
        extra = ["--type", kind] if kind else []
        added = found.ok("file", "add", str(write_pdf(scans / f"{name}.pdf", name)), *extra)
        found.ids[name] = added.data["entity"]["id"]
    everything = found.ok("find", "--include-inactive", "--limit", "100")
    titles = {
        "Alpha topic": "t1",
        "Beta topic": "t2",
        "A page": "w",
        "A book": "b",
        "First note": "n",
    }
    for item in everything.data["items"]:
        attrs = item["attrs"]
        local = titles.get(attrs.get("title") or attrs.get("label"))
        if local:
            found.ids[local] = item["id"]
            found.aliases[local] = found.ok("get", item["id"]).data["entity"]["alias"]
    assert len(found.ids) == 7, found.ids
    return found


def suffix_of(entity: str) -> str:
    return entity.replace("-", "")[-10:]


# ---- a type the caller cannot see is a type that does not exist --------------------------


@pytest.mark.parametrize("hidden", HIDDEN)
@pytest.mark.parametrize("layer", [None, NONE], ids=["allow_list", "allow_list_and_none"])
def test_a_writer_is_denied_a_hidden_type_as_an_unknown_one(
    world: World, hidden: str, layer: str | None
) -> None:
    """The caller's write rights are a ``writes`` list: an unlisted type is a default denial.
    With ``none`` on the type as well, the refusal must not name the rule of the limit."""
    world.install(WRITER)
    if layer is not None:
        world.layer(layer)
    before = len(world.installation_events())
    asked = shown(world.create(hidden, label="X", title="X"))
    absent = shown(world.create("nas:Gone", label="X", title="X"))
    the_same_but_for(asked, absent, (hidden, "nas:Gone"))
    assert asked["error"]["code"] == "access.denied"
    assert asked["error"]["details"]["rule"] == "default.deny"
    events = world.installation_events()[before:]
    assert [e["result"]["status"] for e in events] == ["denied", "denied"]
    # the audit has the real decisions, among them the denial of the type's visibility
    resources = {
        (d["action"], d["resource"].rpartition("type:")[2]): d
        for d in events[0]["authz"]["decisions"]
    }
    assert resources[("read.get", hidden)]["effect"] == "deny"
    assert ("read.get", "nas:Gone") not in {
        (d["action"], d["resource"].rpartition("type:")[2]) for d in events[1]["authz"]["decisions"]
    }


@pytest.mark.parametrize("hidden", HIDDEN)
@pytest.mark.parametrize("layer", [NONE, DENY], ids=["none", "deny_read"])
def test_an_editor_gets_the_answer_for_an_unknown_type_and_the_audit_the_real_decisions(
    world: World, hidden: str, layer: str
) -> None:
    world.install(EDITOR)
    world.layer(layer)
    before = len(world.installation_events())
    asked = shown(world.create(hidden, label="X", title="X"))
    absent = shown(world.create("nas:Gone", label="X", title="X"))
    suggested = []
    for answer in (asked, absent):
        (finding,) = answer["error"]["details"]["findings"]
        suggested.append(finding.pop("expected", None))
    the_same_but_for(asked, absent, (hidden, "nas:Gone"))
    assert asked["error"]["code"] == "write.invalid"
    (finding,) = asked["error"]["details"]["findings"]
    assert finding["code"] == "data.structural.unknown_type"
    assert finding["message"] == f"type {hidden} does not exist"
    assert not set(HIDDEN) & set(suggested[0] or [])  # never offered as a close match
    # the refusal is audited as a denial of what the type's visibility denies
    event = world.installation_events()[before]
    assert event["result"] == {"status": "denied", "code": "access.denied"}
    decisions = {
        (d["action"], d["resource"].rpartition("type:")[2]): d for d in event["authz"]["decisions"]
    }
    assert decisions[("read.get", hidden)]["effect"] == "deny"
    rule = f"type.{hidden}" if layer == NONE else "deny.hide_topics"
    assert decisions[("read.get", hidden)]["rule"] == rule
    # the unknown type is no denial: it was refused by validation only
    assert len(world.installation_events()) == before + 1


def test_the_refusals_leave_no_write_and_the_audit_verifies(world: World) -> None:
    world.install(EDITOR)
    world.layer(NONE)
    head = world.ok("find").envelope["graph"]["seq"]
    assert world.create("nas:Topic", label="X").code != 0
    assert world.ok("find").envelope["graph"]["seq"] == head
    verified = admin(world.run_cli, "audit", "verify")
    assert verified.code == 0, verified.envelope


def test_a_dry_run_of_a_hidden_create_is_answered_alike_and_not_audited(world: World) -> None:
    for installed in (WRITER, EDITOR):
        world.install(installed)
        if installed == EDITOR:
            world.layer(NONE)
        before = len(world.installation_events()), len(world.graph_events())
        asked = shown(world.create("nas:Topic", dry_run=True, label="X"))
        absent = shown(world.create("nas:Gone", dry_run=True, label="X"))
        for answer in (asked, absent):
            for finding in answer["error"]["details"].get("findings", []):
                finding.pop("expected", None)
        the_same_but_for(asked, absent, ("nas:Topic", "nas:Gone"))
        assert (len(world.installation_events()), len(world.graph_events())) == before


# ---- entities the caller cannot see are missing ones -------------------------------------


def test_an_edge_target_given_as_a_hidden_id_is_a_missing_target(world: World) -> None:
    world.install(WRITER)
    note = world.ids["n"]
    hidden = shown(world.ask("link", note, "nas:cites", world.ids["b"]))
    missing = shown(world.ask("link", note, "nas:cites", NOT_THERE))
    the_same_but_for(hidden, missing, (world.ids["b"], NOT_THERE))
    assert hidden["error"]["details"]["findings"][0]["code"] == "data.referential.target_missing"


def test_an_edge_target_given_as_an_alias_or_a_suffix_is_an_unknown_one(world: World) -> None:
    world.install(WRITER)
    note = world.ids["n"]
    pairs = ((world.aliases["t1"], "TOPIC-0099"), (suffix_of(world.ids["b"]), "0123456789"))
    for ref, unknown in pairs:
        hidden = shown(world.ask("link", note, "nas:cites", ref))
        missing = shown(world.ask("link", note, "nas:cites", unknown))
        assert hidden["error"]["code"] == missing["error"]["code"] == "entity.not_found"
        the_same_but_for(hidden, missing, (ref, unknown))
    # a visible page is found by both
    page = world.ok("link", note, "nas:cites", suffix_of(world.ids["w"]), "--dry-run")
    assert page.code == 0


def test_an_inline_edge_to_a_hidden_target_is_a_missing_target(world: World) -> None:
    world.install(EDITOR)
    world.layer(NONE)

    def refusal(target: str) -> list[dict[str, Any]]:
        op = {
            "op": "create",
            "type": "nas:Note",
            "attrs": {"title": "T", "body": "b", "kind": "summary"},
            "edges": [
                {"rel": "nas:about", "target": world.ids["w"]},
                {"rel": "nas:cites", "target": target},
            ],
        }
        done = world.apply({"ops": [op], "reason": "test"}, dry_run=True)
        findings: list[dict[str, Any]] = done.error["details"]["findings"]
        return findings

    hidden, missing = refusal(world.ids["b"]), refusal(NOT_THERE)
    assert str(hidden).replace(world.ids["b"], NOT_THERE) == str(missing)
    assert "data.referential.target_missing" in [f["code"] for f in hidden]


def test_a_ref_to_a_hidden_entity_is_a_ref_to_a_missing_one(world: World) -> None:
    world.install(WRITER)
    for local in ("t1", "b", "plain"):
        entity = world.ids[local]
        refs = [(entity, NOT_THERE), (suffix_of(entity), "0123456789")]
        if world.aliases.get(local):
            refs.append((world.aliases[local], world.aliases[local][:-1] + "9"))
        for ref, unknown in refs:
            hidden = shown(world.ask("update", ref, "--set", "title=X"))
            missing = shown(world.ask("update", unknown, "--set", "title=X"))
            the_same_but_for(hidden, missing, (ref, unknown))
            assert hidden["error"]["code"] == "entity.not_found", (local, ref)


# ---- what a write keeps ------------------------------------------------------------------


def test_an_update_keeps_the_edges_the_caller_cannot_see(world: World) -> None:
    world.install(WRITER)
    before = world.raw_edges("n")
    assert before == ["nas:about>t1", "nas:cites>b", "nas:cites>w"]
    shown_edges = world.ok("get", world.ids["n"]).data["entity"]["edges"]
    assert [e["target"] for e in shown_edges] == [world.ids["w"]]
    world.ok("update", world.ids["n"], "--set", "title=Renamed", "--reason", "test")
    assert world.raw_edges("n") == before  # the topic and the book are still there
    # an unlink of the hidden edge, by target, finds none; the visible one goes
    hidden = shown(world.ask("unlink", world.ids["n"], "nas:cites", world.ids["b"]))
    missing = shown(world.ask("unlink", world.ids["n"], "nas:cites", NOT_THERE))
    the_same_but_for(hidden, missing, (world.ids["b"], NOT_THERE))
    assert hidden["error"]["details"]["findings"][0]["code"] == "data.referential.edge_missing"
    assert world.raw_edges("n") == before
    world.ok("unlink", world.ids["n"], "nas:cites", world.ids["w"], "--reason", "t")
    assert world.raw_edges("n") == ["nas:about>t1", "nas:cites>b"]


def test_a_write_result_names_only_what_the_caller_may_see(world: World) -> None:
    world.install(WRITER)
    done = world.ok("update", world.ids["n"], "--set", "title=Again", "--reason", "test")
    assert [e["id"] for e in done.data["entities"]] == [world.ids["n"]]
    text = json.dumps(done.envelope)
    for hidden in ("t1", "b", "plain"):
        assert world.ids[hidden] not in text


# ---- files -------------------------------------------------------------------------------


@pytest.mark.doc_claim("security_model.integrity_refusals_concealed")
def test_file_add_of_bytes_a_hidden_file_holds_names_that_file_nowhere(world: World) -> None:
    world.install(WRITER)
    copy = world.scans / "copy.pdf"
    copy.write_bytes((world.scans / "plain.pdf").read_bytes())  # a file the caller cannot see
    refused = world.ask("file", "add", str(copy), "--type", "nas:Scan")
    # the residual leak: the refusal says that something holds these bytes, nothing more
    assert refused.code != 0 and "file.already_present" not in refused.warning_codes
    assert refused.error["code"] == "write.invalid"
    (finding,) = refused.error["details"]["findings"]
    assert finding["code"] == "data.semantic.unique_violation"
    assert finding["message"] == "conflicts with data you cannot see"
    assert finding["target"] == {"type": "nas:Scan", "path": "attrs.content"}
    assert world.ids["plain"] not in json.dumps(refused.envelope)
    # the bytes of a file the caller sees are found, and named
    other = world.scans / "copy_of_scan.pdf"
    other.write_bytes((world.scans / "scan.pdf").read_bytes())
    found = world.ok("file", "add", str(other), "--type", "nas:Scan")
    assert found.warning_codes == ["file.already_present"]
    assert found.data["entity"]["id"] == world.ids["scan"]


def test_file_add_names_the_file_classes_the_caller_sees(world: World) -> None:
    world.install(WRITER)
    copy = world.scans / "other.pdf"
    write_pdf(copy, "other")
    answers = {}
    for kind in (["--type", "nas:Book"], ["--type", "nas:Gone"], []):
        refused = world.ask("file", "add", str(copy), *kind)
        assert refused.code == 2, refused.envelope
        answers[tuple(kind)] = refused.error
    for error in answers.values():
        assert error["details"]["expected"] == ["nas:Scan"]
        assert "files:File" not in json.dumps(error)
    assert answers[("--type", "nas:Book")]["message"] == answers[("--type", "nas:Gone")]["message"]
    added = world.ok("file", "add", str(copy), "--type", "nas:Scan")
    assert added.data["entity"]["type"] == "nas:Scan"


# ---- dry runs and batches ----------------------------------------------------------------


def test_a_dry_run_batch_sees_earlier_creates_and_names_nothing_hidden(world: World) -> None:
    world.install(EDITOR)
    world.layer(NONE)
    first = {
        "ops": [
            {
                "op": "create",
                "type": "nas:WebPage",
                "local": "page",
                "attrs": {"title": "Batch page", "url": "https://example.org/batch"},
            }
        ]
    }
    second = {"ops": [{"op": "link", "id": world.ids["n"], "rel": "nas:cites", "target": "@page"}]}
    done = world.apply(first, second, dry_run=True)
    assert done.code == 0, done.envelope
    assert done.data["documents"] == 2
    text = json.dumps(done.envelope)
    assert world.ids["t1"] not in text and world.ids["b"] not in text
    # a later document that names a hidden type fails, as for an unknown one
    third = {"ops": [{"op": "create", "type": "nas:Topic", "attrs": {"label": "X"}}]}
    failed = world.apply(first, second, third, dry_run=True)
    assert failed.code != 0
    assert failed.data["results"][-1]["index"] == 2
    assert failed.error["details"]["findings"][0]["code"] == "data.structural.unknown_type"
    # nothing of a dry run is written
    assert world.raw_edges("n") == ["nas:about>t1", "nas:cites>b", "nas:cites>w"]
