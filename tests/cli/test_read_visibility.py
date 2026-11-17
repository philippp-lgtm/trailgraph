"""S: every read command under a type read limit answers about a hidden entity, type or relation
exactly as about one that does not exist (type read limits plan V5, V9, leak catalogue; the
property test of the same rule is ``tests/contract/test_as_if_absent.py``).

The local app caller is limited by an installation grant with ``reads``: it sees notes, web pages,
scans and claims, but not topics, books or plain files. The graph holds, among others:

- notes with edges to hidden topics and books (only hidden, or hidden and visible ones),
- a note whose second revision only added an edge to a hidden topic,
- a note superseded by a hidden topic and one superseded by a visible note,
- a hidden plain file and a visible scan.

Per command the tests check what is gone (no hidden id, alias, label or relation anywhere in the
answer), what is left (the visible part, with correct totals) and that the answers about hidden
things equal those about missing ones.
"""

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from harness import CliRunner
from support.admin import Outcome, init, write_documents
from support.app import app, create_graph
from support.files import write_pdf
from support.read_limits import NOT_THERE, install, shown, the_same_but_for, without_suggestions

from graph_cli.kernel.records import EdgeRecord, content_hash, content_projection

ENV = {"GRAPH_CLI_GRAPH": "example_graph"}
VISIBLE = ("nas:Note", "nas:WebPage", "nas:Scan", "provenance:Claim")
LIMITED = f"""\
schema: graph_cli.policy/1
id: installation
grants:
  - entrypoint: app
    graphs: [example_graph]
    level: read
    reads: [{", ".join(VISIBLE)}]
no_project: {{"*": write}}
"""
# Names that must never reach a caller who cannot see the hidden types (the curated prose of the
# ontology may still say "topic": it is the maintainer's text, not data).
HIDDEN_TERMS = (
    "nas:Topic",
    "nas:Book",
    "files:File",
    "nas:about",
    "nas:broader",
    "has_note",
    "narrower",
)


def about(topic: str) -> dict[str, str]:
    return {"rel": "nas:about", "target": topic}


def cites(source: str) -> dict[str, str]:
    return {"rel": "nas:cites", "target": source}


def topic(local: str, label: str, *edges: dict[str, str]) -> dict[str, Any]:
    return {
        "op": "create",
        "type": "nas:Topic",
        "local": local,
        "attrs": {"label": label},
        "edges": list(edges),
    }


def note(local: str, title: str, *edges: dict[str, str]) -> dict[str, Any]:
    return {
        "op": "create",
        "type": "nas:Note",
        "local": local,
        "attrs": {"title": title, "body": "text", "kind": "summary"},
        "edges": list(edges),
    }


BUILD = {
    "ops": [
        topic("t1", "Alpha topic"),
        topic("t2", "Beta topic", {"rel": "nas:broader", "target": "@t1"}),
        topic("t3", "Gamma topic"),
        {
            "op": "create",
            "type": "nas:WebPage",
            "local": "w",
            "attrs": {"title": "A page", "url": "https://example.org/p"},
        },
        {
            "op": "create",
            "type": "nas:WebPage",
            "local": "w2",
            "attrs": {"title": "Another page", "url": "https://example.org/q"},
        },
        {"op": "create", "type": "nas:Book", "local": "b", "attrs": {"title": "A book"}},
        note("n", "First note", about("@t1"), cites("@w")),
        note("m", "Second note", about("@t2"), cites("@w"), cites("@b")),
        note("k", "Book note", about("@t1"), cites("@b")),
        note("o", "Third note", about("@t1")),
        note("p", "Old note", about("@t1")),
        note("q", "Older note", about("@t1")),
        note("r", "New note", about("@t1")),
    ]
}
# The second revision of ``n`` only adds an edge to a hidden topic; ``o`` changes a title and
# gets a hidden edge in a later revision; a hidden topic supersedes ``p``, a visible note ``q``.
CHANGES = [
    {
        "reason": "one more topic",
        "ops": [{"op": "link", "id": "@n", "rel": "nas:about", "target": "@t2"}],
    },
    {
        "reason": "retitle",
        "ops": [{"op": "update", "id": "@o", "set": {"title": "Third note, edited"}}],
    },
    {"reason": "link", "ops": [{"op": "link", "id": "@o", "rel": "nas:about", "target": "@t3"}]},
    {
        "reason": "replace",
        "ops": [
            {"op": "supersede", "by": "@t3", "targets": ["@p"]},
            {"op": "supersede", "by": "@r", "targets": ["@q"]},
        ],
    },
]
HIDDEN_LOCALS = ("t1", "t2", "t3", "b")
FILES = {"hidden_file": ("plain.pdf", None), "scan": ("scan.pdf", "nas:Scan")}


@dataclass
class World:
    ids: dict[str, str]
    aliases: dict[str, str | None]
    work: Path
    run_cli: CliRunner

    def limit(self) -> None:
        """Install the policy that hides topics, books and plain files from the app."""
        install(self.run_cli, self.work, LIMITED)

    def hidden_tokens(self) -> list[str]:
        """Everything that names a hidden entity or term."""
        hidden = [*HIDDEN_LOCALS, "hidden_file"]
        found = [self.ids[local] for local in hidden]
        found += [a for local in hidden if (a := self.aliases[local]) is not None]
        found += ["Alpha topic", "Beta topic", "Gamma topic", "A book", "plain.pdf"]
        return [*found, *HIDDEN_TERMS]

    def local(self, entity: str) -> str:
        return {i: local for local, i in self.ids.items()}[entity]

    def locals(self, ids: Iterable[str]) -> list[str]:
        return sorted(self.local(i) for i in ids)


def read(run_cli: CliRunner, *args: str) -> Outcome:
    return app(run_cli, *args, env=ENV)


def ok(run_cli: CliRunner, *args: str) -> Outcome:
    outcome = read(run_cli, *args)
    assert outcome.code == 0, (args, outcome.envelope)
    return outcome


def leaks(outcome: Outcome, world: World) -> list[str]:
    """The hidden names that appear anywhere in the answer."""
    text = json.dumps(outcome.envelope)
    return [token for token in world.hidden_tokens() if token in text]


def twin(
    run_cli: CliRunner,
    build: Callable[[str], Sequence[str]],
    hidden: str,
    missing: str = NOT_THERE,
    *swaps: tuple[str, str],
    suggestions: bool = True,
) -> dict[str, Any]:
    """Ask with a hidden name and with a missing one: the answers are the same but for the name
    (and, with ``suggestions=False``, the ``did_you_mean`` of an error, which follows the
    spelling of the name asked). Returns the answer about the hidden one."""
    asked = shown(read(run_cli, *build(hidden)))
    absent = shown(read(run_cli, *build(missing)))
    if suggestions:
        the_same_but_for(asked, absent, (hidden, missing), *swaps)
    else:
        the_same_but_for(
            without_suggestions(asked), without_suggestions(absent), (hidden, missing), *swaps
        )
    return asked


@pytest.fixture
def world(run_cli: CliRunner, isolated_env: dict[str, str], tmp_path: Path) -> World:
    root = init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "notes_and_sources", version="1.1.0")
    config = Path(isolated_env["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
    config.write_text(config.read_text().rstrip("\n") + '\n\n[roots]\nnotes = "~/notes"\n')
    scans = Path(isolated_env["HOME"]) / "notes"
    write_documents(root, "example_graph", [BUILD, *CHANGES])
    found = {}
    for name, (file_name, kind) in FILES.items():
        path = write_pdf(scans / file_name, name)
        extra = ["--type", kind] if kind else []
        added = ok(run_cli, "file", "add", str(path), "--no-extract", *extra)
        found[name] = added.data["entity"]["id"]
    everything = ok(run_cli, "find", "--include-inactive", "--limit", "500")
    ids = dict(found)
    for item in everything.data["items"]:
        attrs = item["attrs"]
        title = attrs.get("title") or attrs.get("label")
        local = {
            "Alpha topic": "t1",
            "Beta topic": "t2",
            "Gamma topic": "t3",
            "A page": "w",
            "Another page": "w2",
            "A book": "b",
            "First note": "n",
            "Second note": "m",
            "Book note": "k",
            "Third note, edited": "o",
            "Old note": "p",
            "Older note": "q",
            "New note": "r",
        }.get(title)
        if local:
            ids[local] = item["id"]
    assert len(ids) == 15, sorted(ids)
    aliases = {
        local: read(run_cli, "get", entity).data["entity"]["alias"] for local, entity in ids.items()
    }
    return World(ids, aliases, tmp_path / "policies", run_cli)


@pytest.fixture
def limited(world: World) -> World:
    world.limit()
    return world


def recomputed(entity: dict[str, Any]) -> str:
    """The content hash of an entity as shown (``--full``), computed from what is shown."""
    projection = content_projection(
        type=entity["type"],
        record_status=entity["record_status"],
        alias=entity["alias"],
        attrs=entity["attrs"],
        edges=tuple(EdgeRecord.from_json(e) for e in entity["edges"]),
        valid_from=entity["valid_from"],
        valid_to=entity["valid_to"],
    )
    return content_hash(projection)


def each_hidden(world: World) -> list[str]:
    return [*HIDDEN_LOCALS, "hidden_file"]


def suffix_of(entity: str) -> str:
    return entity.replace("-", "")[-10:]


# ---- get ---------------------------------------------------------------------------------


def test_get_of_a_hidden_entity_is_get_of_a_missing_one_in_every_ref_form(limited: World) -> None:
    run_cli = limited.run_cli
    for local in each_hidden(limited):
        entity, alias = limited.ids[local], limited.aliases[local]
        for options in ((), ("--full",), ("--fields", "edges"), ("--as-of", "tx:1")):
            answer = twin(run_cli, lambda ref, options=options: ("get", ref, *options), entity)
            assert answer["error"]["code"] == "entity.not_found", (local, options)
        for rev in ("1", "2", "9"):
            by_rev = twin(run_cli, lambda ref, rev=rev: ("get", ref, "--rev", rev), entity)
            assert by_rev["error"]["code"] == "entity.not_found", (local, rev)
        twin(run_cli, lambda ref: ("get", ref, "--affordances"), entity)
        if alias is not None:
            unknown = alias[:-1] + "9"
            assert unknown != alias
            twin(run_cli, lambda ref: ("get", ref), alias, unknown)
            twin(run_cli, lambda ref: ("get", ref, "--rev", "1"), alias, unknown)
        twin(run_cli, lambda ref: ("get", ref), suffix_of(entity), "0123456789")
    # a visible entity is still reached by its alias and by the end of its id
    note_ = limited.ids["n"]
    for ref in (limited.aliases["n"], suffix_of(note_)):
        assert ok(run_cli, "get", str(ref)).data["entity"]["id"] == note_


def test_get_hides_edges_to_hidden_entities_and_keeps_a_matching_hash(limited: World) -> None:
    run_cli, ids = limited.run_cli, limited.ids
    outcome = ok(run_cli, "get", ids["n"], "--full")
    entity = outcome.data["entity"]
    assert [(e["rel"], e["target"]) for e in entity["edges"]] == [("nas:cites", ids["w"])]
    assert leaks(outcome, limited) == []
    assert entity["content_hash"] == recomputed(entity)
    assert (entity["rev"], entity["tx_seq"]) == (2, 2)  # the revisions stay as they were
    # the same entity at its first revision, at its first transaction and without --full
    first = ok(run_cli, "get", ids["n"], "--rev", "1", "--full")
    assert first.data["entity"]["rev"] == 1
    assert first.data["entity"]["content_hash"] == recomputed(first.data["entity"])
    assert [e["target"] for e in first.data["entity"]["edges"]] == [ids["w"]]
    as_of = ok(run_cli, "get", ids["n"], "--as-of", "tx:1", "--full")
    assert as_of.data["entity"] == first.data["entity"]
    for options in ((), ("--fields", "edges"), ("--rev", "2")):
        assert leaks(ok(run_cli, "get", ids["n"], *options), limited) == [], options
    # every hash shown is the hash of what is shown; it differs from the stored one
    # (the baseline hash of a note with only hidden edges)
    only_hidden = ok(run_cli, "get", ids["o"], "--full").data["entity"]
    assert only_hidden["edges"] == [] and only_hidden["content_hash"] == recomputed(only_hidden)
    # an entity without a hidden edge shows its stored hash
    page = ok(run_cli, "get", ids["w"], "--full").data["entity"]
    assert page["content_hash"] == recomputed(page)


def test_get_superseded_by_hides_a_hidden_superseder(limited: World) -> None:
    run_cli, ids = limited.run_cli, limited.ids
    hidden_superseder = ok(run_cli, "get", ids["p"])  # superseded by a hidden topic
    assert hidden_superseder.data["entity"]["record_status"] == "superseded"
    assert hidden_superseder.data["superseded_by"] == []
    assert leaks(hidden_superseder, limited) == []
    visible_superseder = ok(run_cli, "get", ids["q"])
    assert visible_superseder.data["superseded_by"] == [ids["r"]]
    assert visible_superseder.data["retracted_by"] == []
    # at the revision before the supersession nothing is superseded yet
    assert ok(run_cli, "get", ids["q"], "--rev", "1").data["entity"]["record_status"] == "active"
    # the supersession edge of the visible superseder stays, the hidden one is not shown
    assert [e["rel"] for e in ok(run_cli, "get", ids["r"]).data["entity"]["edges"]] == [
        "core:supersedes"
    ]


# ---- find --------------------------------------------------------------------------------


def found(outcome: Outcome, world: World) -> list[str]:
    return world.locals(i["id"] for i in outcome.data["items"])


def test_find_lists_only_visible_entities_and_counts_what_is_left(world: World) -> None:
    run_cli = world.run_cli
    raw = ok(run_cli, "find", "--limit", "500")
    assert raw.page is not None and raw.page["total"] == 13  # 15 entities, two superseded
    world.limit()
    everything = ok(run_cli, "find", "--limit", "500")
    assert found(everything, world) == ["k", "m", "n", "o", "r", "scan", "w", "w2"]
    assert everything.page is not None and everything.page["total"] == 8
    assert leaks(everything, world) == []
    inactive = ok(run_cli, "find", "--include-inactive", "--limit", "500")
    assert found(inactive, world) == ["k", "m", "n", "o", "p", "q", "r", "scan", "w", "w2"]
    assert inactive.page is not None and inactive.page["total"] == 10
    # paging counts what is visible: pages of three cover the eight, in order
    pages = [ok(run_cli, "find", "--limit", "3", "--offset", str(o)) for o in (0, 3, 6)]
    assert [len(p.data["items"]) for p in pages] == [3, 3, 2]
    assert all(p.page is not None and p.page["total"] == 8 for p in pages)
    assert sorted(i["id"] for p in pages for i in p.data["items"]) == sorted(
        i["id"] for i in everything.data["items"]
    )
    past = ok(run_cli, "find", "--limit", "3", "--offset", "9")
    assert past.data["items"] == [] and past.page is not None and past.page["total"] == 8
    # fields, edges and hashes of the items
    full = ok(run_cli, "find", "--full", "--type", "nas:Note", "--limit", "500")
    for item in full.data["items"]:
        assert item["content_hash"] == recomputed(item), item["id"]
    assert leaks(full, world) == []
    edges = ok(run_cli, "find", "--type", "nas:Note", "--fields", "edges", "--limit", "500")
    by_local = {world.local(i["id"]): i["edges"] for i in edges.data["items"]}
    assert {k: [e["target"] for e in v] for k, v in by_local.items()} == {
        "n": [world.ids["w"]],
        "m": [world.ids["w"]],
        "k": [],
        "o": [],
        "r": [world.ids["q"]],  # the supersession of a visible note stays
    }


def test_find_by_type_follows_the_view(limited: World) -> None:
    run_cli = limited.run_cli
    sources = ok(run_cli, "find", "--type", "nas:Source")  # abstract: the visible subtypes only
    assert found(sources, limited) == ["w", "w2"]
    assert {i["type"] for i in sources.data["items"]} == {"nas:WebPage"}
    for hidden in ("nas:Topic", "nas:Book", "files:File"):
        answer = twin(
            run_cli,
            lambda t: ("find", "--type", t),
            hidden,
            "nas:Nothing",
            (hidden.split(":")[1], "Nothing"),
            suggestions=False,
        )
        assert answer["error"]["code"] == "term.unknown", hidden
        suggestions = answer["error"]["details"].get("did_you_mean", [])
        assert hidden not in suggestions
    # by the unique local name, too
    for name in ("Topic", "Book"):
        by_name = twin(run_cli, lambda t: ("find", "--type", t), name, "Nothing", suggestions=False)
        assert by_name["error"]["code"] == "term.unknown", name


def test_find_by_relation_ignores_edges_to_hidden_entities(world: World) -> None:
    run_cli, ids = world.run_cli, world.ids
    raw_cites = ok(run_cli, "find", "--type", "nas:Note", "--rel", "nas:cites")
    assert found(raw_cites, world) == ["k", "m", "n"]
    raw_book = ok(run_cli, "find", "--type", "nas:Note", "--rel", f"nas:cites={ids['b']}")
    assert found(raw_book, world) == ["k", "m"]
    world.limit()
    # exists: k cites only a hidden book, so it has no visible citation
    cites_ = ok(run_cli, "find", "--type", "nas:Note", "--rel", "nas:cites")
    assert found(cites_, world) == ["m", "n"]
    assert cites_.page is not None and cites_.page["total"] == 2
    assert leaks(cites_, world) == []
    visible = ok(run_cli, "find", "--type", "nas:Note", "--rel", f"nas:cites={ids['w']}")
    assert found(visible, world) == ["m", "n"]
    # a hidden target id: an empty page, as for an id that is not there
    hidden = ok(run_cli, "find", "--type", "nas:Note", "--rel", f"nas:cites={ids['b']}")
    missing = ok(run_cli, "find", "--type", "nas:Note", "--rel", f"nas:cites={NOT_THERE}")
    assert shown(hidden) == shown(missing)
    assert (hidden.data["items"], hidden.page["total"]) == ([], 0)  # type: ignore[index]
    mixed = ok(run_cli, "find", "--rel", f"nas:cites={ids['b']},{ids['w']}", "--type", "nas:Note")
    assert found(mixed, world) == ["m", "n"]
    # the other ref forms of a hidden entity are unknown refs, as unknown aliases and suffixes
    alias = str(world.aliases["t3"])
    twin(
        run_cli,
        lambda r: ("find", "--include-inactive", "--in-rel", f"core:supersedes={r}"),
        alias,
        alias[:-1] + "9",
    )
    twin(
        run_cli,
        lambda r: ("find", "--type", "nas:Note", "--rel", f"nas:cites={r}"),
        suffix_of(ids["b"]),
        "0123456789",
    )
    # a relation to a hidden type is unknown, as a relation that is not there
    for relation, other in (("nas:about", "nas:nothing"), ("nas:broader", "nas:nothing")):
        for option in ("--rel", "--in-rel"):
            answer = twin(
                run_cli,
                lambda r, option=option: ("find", option, r),
                relation,
                other,
                (relation.split(":")[1], "nothing"),
                suggestions=False,
            )
            assert answer["error"]["code"] == "term.unknown", (relation, option)
            suggestions = answer["error"]["details"].get("did_you_mean", [])
            assert relation not in suggestions


def test_find_by_incoming_relation_ignores_hidden_sources(world: World) -> None:
    run_cli, ids = world.run_cli, world.ids
    raw = ok(run_cli, "find", "--include-inactive", "--in-rel", "core:supersedes")
    assert found(raw, world) == ["p", "q"]
    world.limit()
    # p is superseded by a hidden topic: for the caller nothing supersedes it
    superseded = ok(run_cli, "find", "--include-inactive", "--in-rel", "core:supersedes")
    assert found(superseded, world) == ["q"]
    assert superseded.page is not None and superseded.page["total"] == 1
    by_source = ok(run_cli, "find", "--include-inactive", "--in-rel", f"core:supersedes={ids['r']}")
    assert found(by_source, world) == ["q"]
    hidden = ok(run_cli, "find", "--include-inactive", "--in-rel", f"core:supersedes={ids['t3']}")
    missing = ok(run_cli, "find", "--include-inactive", "--in-rel", f"core:supersedes={NOT_THERE}")
    assert shown(hidden) == shown(missing) and hidden.data["items"] == []
    # the visible web pages: cited by visible notes only; the book is hidden
    cited = ok(run_cli, "find", "--in-rel", "nas:cites")
    assert found(cited, world) == ["w"]
    by_hidden_note_free = ok(run_cli, "find", "--in-rel", f"nas:cites={ids['k']}")
    assert by_hidden_note_free.data["items"] == []  # k cites only the hidden book
    assert (
        ok(run_cli, "find", "--in-rel", f"nas:cites={ids['m']}").data["items"][0]["id"]
        == (ids["w"])
    )


# ---- lookup and search -------------------------------------------------------------------


def test_lookup_and_search_know_no_hidden_entity(world: World) -> None:
    run_cli = world.run_cli
    for command in ("lookup", "search"):
        raw = ok(run_cli, command, "Alpha topic")
        assert [i["label" if command == "lookup" else "id"] for i in raw.data["items"]]
    prefix = ok(run_cli, "lookup", "A", "--match", "prefix", "--limit", "500")
    raw_prefix = world.locals(i["id"] for i in prefix.data["items"])
    world.limit()
    for command in ("lookup", "search"):
        for text in ("Alpha topic", "Beta topic", "A book", "plain"):
            asked = twin(
                run_cli, lambda t, command=command: (command, t), text, "Nothing of the kind"
            )
            assert asked["code"] == 0 and asked["data"]["items"] == [], (command, text)
        visible = ok(run_cli, command, "First note")
        assert [i["id"] for i in visible.data["items"]] == [world.ids["n"]], command
        assert leaks(visible, world) == [], command
    counts = ok(run_cli, "lookup", "Alpha topic").data["texts"]
    assert counts == [{"text": "Alpha topic", "total": 0, "exact": 0, "excluded": 0}]
    # a prefix that matches visible and hidden entities finds the visible ones only
    several = ok(run_cli, "lookup", "A", "--match", "prefix", "--limit", "500")
    assert world.locals(i["id"] for i in several.data["items"]) == ["w", "w2"]
    assert raw_prefix == ["b", "t1", "w", "w2"]
    assert leaks(several, world) == []
    several = ok(run_cli, "search", "note", "--limit", "500")
    assert world.locals(i["id"] for i in several.data["items"]) == ["k", "m", "n", "o", "r"]
    assert leaks(several, world) == []
    for kind in ("nas:Topic", "nas:Book"):
        answer = twin(
            run_cli,
            lambda t: ("lookup", "Alpha topic", "--type", t),
            kind,
            "nas:Nothing",
            (kind.split(":")[1], "Nothing"),
            suggestions=False,
        )
        assert answer["error"]["code"] == "term.unknown"
        answer = twin(
            run_cli,
            lambda t: ("search", "Alpha topic", "--type", t),
            kind,
            "nas:Nothing",
            (kind.split(":")[1], "Nothing"),
            suggestions=False,
        )
        assert answer["error"]["code"] == "term.unknown"


def test_lookup_fit_hints_name_no_hidden_type_or_relation(world: World) -> None:
    run_cli = world.run_cli
    raw = ok(run_cli, "lookup", "A page", "--for", "cites")
    assert raw.data["fit"]["types"] == ["nas:Book", "nas:Source", "nas:WebPage"]
    world.limit()
    cites_ = ok(run_cli, "lookup", "A page", "--for", "cites")
    assert cites_.data["fit"]["types"] == ["nas:Source", "nas:WebPage"]
    assert [i["id"] for i in cites_.data["items"]] == [world.ids["w"]]
    source = ok(run_cli, "lookup", "First note", "--for-source", "cites")
    assert source.data["fit"]["types"] == ["nas:Note"]
    for option in ("--for", "--for-source"):
        for hidden, other in (("about", "nothing"), ("nas:about", "nas:nothing"), ("broader", "x")):
            answer = twin(
                run_cli,
                lambda r, option=option: ("lookup", "First note", option, r),
                hidden,
                other,
                suggestions=False,
            )
            assert answer["error"]["code"] == "term.unknown", (option, hidden)
            assert "about" not in json.dumps(answer["error"]["details"].get("did_you_mean", []))


# ---- neighbors ---------------------------------------------------------------------------


def reached(outcome: Outcome, world: World) -> list[str]:
    return world.locals(i["entity"]["id"] for i in outcome.data["items"])


def test_neighbors_never_walk_through_a_hidden_node(world: World) -> None:
    run_cli, ids = world.run_cli, world.ids
    raw = ok(run_cli, "neighbors", ids["n"], "--direction", "both", "--depth", "3")
    assert {"t1", "t2", "t3", "o", "k", "w", "m"} <= set(reached(raw, world))
    world.limit()
    expected = {
        ("n", "both", 1): ["w"],
        ("n", "both", 2): ["m", "w"],
        ("n", "both", 3): ["m", "w"],  # not the notes behind the topics
        ("n", "out", 3): ["w"],
        ("w", "in", 1): ["m", "n"],
        ("w", "in", 3): ["m", "n"],
        ("w", "both", 3): ["m", "n"],
        ("m", "out", 3): ["w"],
        ("m", "both", 3): ["n", "w"],
        ("k", "both", 3): [],  # only edges to a hidden book and a hidden topic
        ("o", "both", 3): [],
        ("p", "both", 3): [],  # superseded by a hidden topic
        ("q", "both", 2): ["r"],
        ("r", "out", 1): ["q"],
        ("scan", "both", 3): [],
    }
    for (start, direction, depth), names in expected.items():
        outcome = ok(
            run_cli,
            *("neighbors", ids[start], "--direction", direction, "--depth", str(depth)),
        )
        assert reached(outcome, world) == names, (start, direction, depth)
        assert leaks(outcome, world) == [], (start, direction, depth)
    # every shown neighbour is stripped, with the hash of what is shown
    full = ok(run_cli, "neighbors", ids["n"], "--depth", "3", "--direction", "both", "--full")
    for item in full.data["items"]:
        assert item["entity"]["content_hash"] == recomputed(item["entity"])
    by_relation = ok(run_cli, "neighbors", ids["m"], "--rel", "nas:cites", "--direction", "out")
    assert reached(by_relation, world) == ["w"]
    asof = ok(
        run_cli, "neighbors", ids["n"], "--direction", "both", "--depth", "3", "--as-of", "tx:1"
    )
    assert reached(asof, world) == ["m", "w"]


def test_neighbors_of_a_hidden_start_or_relation_are_as_for_a_missing_one(limited: World) -> None:
    run_cli = limited.run_cli
    for local in each_hidden(limited):
        entity = limited.ids[local]
        for direction in ("out", "in", "both"):
            for depth in ("1", "3"):
                answer = twin(
                    run_cli,
                    lambda ref, direction=direction, depth=depth: (
                        "neighbors",
                        ref,
                        "--direction",
                        direction,
                        "--depth",
                        depth,
                    ),
                    entity,
                )
                assert answer["error"]["code"] == "entity.not_found"
        twin(run_cli, lambda ref: ("neighbors", ref, "--as-of", "tx:1"), entity)
    for relation in ("nas:about", "nas:broader"):
        answer = twin(
            run_cli,
            lambda r: ("neighbors", limited.ids["n"], "--rel", r),
            relation,
            "nas:nothing",
            (relation.split(":")[1], "nothing"),
            suggestions=False,
        )
        assert answer["error"]["code"] == "term.unknown"


# ---- view --------------------------------------------------------------------------------


def card_section(outcome: Outcome, kind: str, variant: str | None = None) -> dict[str, Any]:
    found = [
        s for s in outcome.data["sections"] if s["kind"] == kind and s.get("variant") == variant
    ]
    assert len(found) <= 1, outcome.data
    section: dict[str, Any] = found[0] if found else {}
    return section


def cite_files(world: World) -> None:
    """Pages of the scan cited by two visible notes and a hidden topic, and a note that points
    to the hidden plain file too (before the limit, so every write is allowed)."""
    run_cli, ids = world.run_cli, world.ids
    for source, target, qualifiers in (
        ("n", "scan", ("page=4",)),
        ("m", "scan", ("page=9", "last_page=11")),
        ("t1", "scan", ("page=2",)),
        ("n", "hidden_file", ("page=1",)),
    ):
        extra = [a for q in qualifiers for a in ("--qualifier-json", q)]
        linked = ok(run_cli, "link", ids[source], "files:from_file", ids[target], *extra)
        assert linked.code == 0


def test_view_of_a_hidden_entity_is_view_of_a_missing_one_in_every_ref_form(
    limited: World,
) -> None:
    run_cli = limited.run_cli
    for local in each_hidden(limited):
        entity, alias = limited.ids[local], limited.aliases[local]
        for options in ((), ("--as-of", "tx:1")):
            answer = twin(run_cli, lambda ref, options=options: ("view", ref, *options), entity)
            assert answer["error"]["code"] == "entity.not_found", (local, options)
        if alias is not None:
            unknown = alias[:-1] + "9"
            twin(run_cli, lambda ref: ("view", ref), alias, unknown)
        twin(run_cli, lambda ref: ("view", ref), suffix_of(entity), "0123456789")
    visible = limited.ids["n"]
    for ref in (limited.aliases["n"], suffix_of(visible)):
        assert ok(run_cli, "view", str(ref)).data["entity"]["id"] == visible


def test_view_counts_and_names_only_what_the_reader_sees(world: World) -> None:
    run_cli, ids = world.run_cli, world.ids
    raw = ok(run_cli, "view", ids["n"])
    assert {g["relation"] for g in card_section(raw, "relations")["groups"]} == {
        "nas:about",
        "nas:cites",
    }
    world.limit()
    outcome = ok(run_cli, "view", ids["n"])
    groups = card_section(outcome, "relations")["groups"]
    assert [(g["relation"], g["direction"], g["count"]) for g in groups] == [
        ("nas:cites", "out", 1)
    ]
    assert [i["ref"] for i in groups[0]["items"]] == [ids["w"]]  # a web page has no alias
    assert leaks(outcome, world) == []
    # a group whose neighbours are all hidden is no group, and no relation name is left over
    only_hidden = ok(run_cli, "view", ids["k"])
    assert "relations" not in [s["kind"] for s in only_hidden.data["sections"]]
    assert leaks(only_hidden, world) == []
    # in-edges from hidden sources are not counted: w is cited by n and m
    page = ok(run_cli, "view", ids["w"])
    (group,) = card_section(page, "relations")["groups"]
    assert (group["relation"], group["direction"], group["count"]) == ("nas:cites", "in", 2)
    assert [i["label"] for i in group["items"]] == ["First note", "Second note"]
    assert leaks(page, world) == []
    # at an earlier point, too
    earlier = ok(run_cli, "view", ids["n"], "--as-of", "tx:1")
    assert [g["relation"] for g in card_section(earlier, "relations")["groups"]] == ["nas:cites"]
    assert leaks(earlier, world) == []


def test_view_names_a_hidden_superseder_as_nothing(limited: World) -> None:
    run_cli, ids = limited.run_cli, limited.ids
    hidden_superseder = ok(run_cli, "view", ids["p"])  # superseded by a hidden topic
    assert hidden_superseder.data["entity"]["record_status"] == "superseded"
    assert "superseded_by" not in hidden_superseder.data["entity"]
    assert leaks(hidden_superseder, limited) == []
    visible_superseder = ok(run_cli, "view", ids["q"])
    assert visible_superseder.data["entity"]["superseded_by"] == [ids["r"]]


def test_view_files_follow_the_type_of_the_file_and_of_the_citing_entity(world: World) -> None:
    run_cli, ids = world.run_cli, world.ids
    cite_files(world)
    raw = ok(run_cli, "view", ids["n"])
    assert [i["ref"] for i in card_section(raw, "files")["items"]] == [
        str(world.aliases["hidden_file"]),
        str(world.aliases["scan"]),
    ]
    cited_raw = card_section(ok(run_cli, "view", ids["scan"]), "files", "cited_by")
    assert (cited_raw["count"], cited_raw["page_min"], cited_raw["page_max"]) == (3, 2, 11)
    world.limit()
    outcome = ok(run_cli, "view", ids["n"])
    files = card_section(outcome, "files")
    assert [(i["ref"], i["page"]) for i in files["items"]] == [(str(world.aliases["scan"]), 4)]
    assert "more" not in files
    assert leaks(outcome, world) == []
    cited = card_section(ok(run_cli, "view", ids["scan"]), "files", "cited_by")
    assert (cited["count"], cited["page_min"], cited["page_max"]) == (2, 4, 11)
    assert [i["ref"] for i in cited["items"]] == [world.aliases["n"], world.aliases["m"]]
    assert leaks(ok(run_cli, "view", ids["scan"]), world) == []


# ---- query (Cypher) ----------------------------------------------------------------------


def rows(outcome: Outcome) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = outcome.data["items"]
    return found


def test_cypher_scan_of_a_hidden_label_or_relation_is_an_unknown_term(limited: World) -> None:
    run_cli = limited.run_cli
    labels = [("Topic", "Zzzz"), ("Book", "Zzzz"), ("File", "Zzzz")]
    labels += [("`nas:Topic`", "`nas:Zzzz`"), ("`files:File`", "`nas:Zzzz`")]
    for label, other in labels:
        answer = twin(
            run_cli,
            lambda name: ("query", f"MATCH (n:{name}) RETURN n"),
            label,
            other,
            (label.strip("`"), other.strip("`")),
            suggestions=False,
        )
        assert answer["error"]["code"] == "term.unknown", label
    relations = [("about", "zzzz"), ("broader", "zzzz"), ("has_note", "zzzz")]
    relations += [("`nas:about`", "`nas:zzzz`")]
    for relation, other in relations:
        answer = twin(
            run_cli,
            lambda name: ("query", f"MATCH (n:Note)-[:{name}]->(x) RETURN n"),
            relation,
            other,
            (relation.strip("`"), other.strip("`")),
            suggestions=False,
        )
        assert answer["error"]["code"] == "term.unknown", relation
        assert relation.strip("`") not in answer["error"]["details"].get("did_you_mean", [])
    # a hidden type among several labels, and as a label predicate
    twin(
        run_cli,
        lambda name: ("query", f"MATCH (n:Note|{name}) RETURN n"),
        "Topic",
        "Zzzz",
        suggestions=False,
    )


def test_cypher_counts_nodes_edges_and_paths_over_the_view(world: World) -> None:
    run_cli = world.run_cli

    def query(text: str, *params: str) -> Outcome:
        return ok(run_cli, "query", text, *params)

    nodes = "MATCH (n) RETURN count(n) AS c"
    edges = "MATCH (a)-[r]->(b) RETURN type(r) AS t, count(*) AS c ORDER BY t"
    pairs = "MATCH (a:Note)-[*1..3]-(b:Note) RETURN a.alias AS a, b.alias AS b ORDER BY a, b"
    walks = "MATCH (a:Note)-[:cites*1..3]-(b) RETURN count(*) AS c"
    cited = (
        "MATCH (n:Note) OPTIONAL MATCH (n)-[:cites]->(s) "
        "RETURN n.alias AS note, count(s) AS cited ORDER BY note"
    )
    raw = {text: rows(query(text)) for text in (nodes, edges, pairs, walks, cited)}
    assert raw[nodes] == [{"c": 13}]
    assert {r["t"] for r in raw[edges]} >= {"about", "broader"}
    alias = {local: world.aliases[local] for local in ("n", "m", "k", "o", "r")}
    assert len(raw[pairs]) > 2 and raw[walks][0]["c"] > 4
    assert {r["note"]: r["cited"] for r in raw[cited]}[alias["k"]] == 1
    world.limit()
    assert rows(query(nodes)) == [{"c": 8}]
    assert rows(query(edges)) == [{"t": "cites", "c": 2}]  # no edge of an inactive node
    # the notes reach each other only through the page they cite, never through a topic
    assert rows(query(pairs)) == sorted(
        [{"a": alias["m"], "b": alias["n"]}, {"a": alias["n"], "b": alias["m"]}],
        key=lambda row: row["a"],
    )
    assert rows(query(walks)) == [{"c": 4}]  # n: the page and m; m: the page and n
    assert rows(query(cited)) == [
        {"note": alias["n"], "cited": 1},  # the first note cites the visible page only
        {"note": alias["m"], "cited": 1},
        {"note": alias["k"], "cited": 0},  # its only citation is a hidden book
        {"note": alias["o"], "cited": 0},
        {"note": alias["r"], "cited": 0},
    ]


def test_cypher_returned_nodes_have_no_hidden_edges(limited: World) -> None:
    run_cli, ids = limited.run_cli, limited.ids
    notes = ok(run_cli, "query", "MATCH (n:Note) RETURN n ORDER BY n.alias")
    assert leaks(notes, limited) == []
    by_local = {limited.local(r["n"]["id"]): r["n"] for r in rows(notes)}
    assert sorted(by_local) == ["k", "m", "n", "o", "r"]
    assert [e["target"] for e in by_local["n"]["edges"]] == [ids["w"]]
    assert by_local["o"]["edges"] == []
    found_ = ok(run_cli, "find", "--type", "nas:Note", "--limit", "500")
    assert sorted(i["id"] for i in found_.data["items"]) == sorted(
        r["n"]["id"] for r in rows(notes)
    )
    assert {i["id"]: i for i in found_.data["items"]}[ids["n"]] == by_local["n"]
    # properties that name edges and ids of hidden entities
    hidden = ids["t1"]
    for text in (
        "MATCH (n) WHERE n.id = $id RETURN n",
        "MATCH (n) WHERE n.id = $id RETURN count(n) AS c",
        "MATCH (n:Note)-[*1..2]-(x) WHERE x.id = $id RETURN n",
    ):
        asked = twin(run_cli, lambda i, text=text: ("query", text, "--param", f"id={i}"), hidden)
        assert asked["code"] == 0
        assert asked["data"]["items"] in ([], [{"c": 0}]), text
    twin(
        run_cli,
        lambda a: ("query", "MATCH (n) WHERE n.alias = $a RETURN n", "--param", f"a={a}"),
        str(limited.aliases["t1"]),
        "TOPIC-0099",
    )


# ---- history and time --------------------------------------------------------------------


def changed(outcome: Outcome) -> dict[int, Any]:
    return {i["rev"]: i["changed"] for i in outcome.data["items"]}


def test_history_of_a_hidden_entity_is_history_of_a_missing_one(limited: World) -> None:
    run_cli = limited.run_cli
    for local in each_hidden(limited):
        entity = limited.ids[local]
        for options in ((), ("--limit", "1"), ("--as-of", "tx:1"), ("--offset", "1")):
            answer = twin(run_cli, lambda ref, options=options: ("history", ref, *options), entity)
            assert answer["error"]["code"] == "entity.not_found", (local, options)
        alias = limited.aliases[local]
        if alias is not None:
            twin(run_cli, lambda ref: ("history", ref), alias, alias[:-1] + "9")


def test_a_revision_that_only_changed_a_hidden_edge_shows_no_change(world: World) -> None:
    run_cli, ids = world.run_cli, world.ids
    raw = ok(run_cli, "history", ids["n"])
    assert changed(raw)[2]["edges"]["added"] != []  # the link to the second topic
    world.limit()
    history = ok(run_cli, "history", ids["n"])
    assert [i["rev"] for i in history.data["items"]] == [2, 1]  # the revisions are all there
    assert [i["tx_seq"] for i in history.data["items"]] == [2, 1]
    assert changed(history) == {
        2: {"attrs": [], "edges": {"added": [], "removed": [], "changed": []}, "record": []},
        1: None,
    }
    assert history.data["items"][0]["reason"] == "one more topic"  # the writer's words stay
    assert leaks(history, world) == []
    # a title change and, later, a hidden edge: only the title shows
    edited = ok(run_cli, "history", ids["o"])
    assert [i["rev"] for i in edited.data["items"]] == [3, 2, 1]
    assert changed(edited)[2]["attrs"] == ["title"]
    assert changed(edited)[3] == {
        "attrs": [],
        "edges": {"added": [], "removed": [], "changed": []},
        "record": [],
    }
    # a supersession by a hidden entity changes the record status and nothing else
    superseded = ok(run_cli, "history", ids["p"])
    assert changed(superseded)[2]["record"] == ["record_status"]
    assert changed(superseded)[2]["edges"] == {"added": [], "removed": [], "changed": []}
    assert leaks(superseded, world) == []
    paged = ok(run_cli, "history", ids["o"], "--limit", "1", "--offset", "1")
    assert [i["rev"] for i in paged.data["items"]] == [2]
    assert paged.page is not None and paged.page["total"] == 3
    # the stripped revisions behind it
    for rev in (1, 2, 3):
        shown_ = ok(run_cli, "get", ids["o"], "--rev", str(rev), "--full").data["entity"]
        assert shown_["edges"] == [] and shown_["content_hash"] == recomputed(shown_)


def test_as_of_reads_are_views_of_the_past(world: World) -> None:
    run_cli, ids = world.run_cli, world.ids
    raw_first = ok(run_cli, "find", "--as-of", "tx:1", "--limit", "500")
    assert raw_first.page is not None and raw_first.page["total"] == 13
    world.limit()
    first = ok(run_cli, "find", "--as-of", "tx:1", "--limit", "500")
    # at the first transaction: the nine notes and pages that were active then, not yet the scan
    assert found(first, world) == ["k", "m", "n", "o", "p", "q", "r", "w", "w2"]
    assert first.page is not None and first.page["total"] == 9
    assert leaks(first, world) == []
    # the same state named by a time: the moment of the first transaction
    first_time = ok(run_cli, "history", ids["n"]).data["items"][-1]["recorded_at"]
    by_time = ok(run_cli, "find", "--as-of", first_time, "--limit", "500")
    assert shown(by_time)["data"] == shown(first)["data"]
    assert by_time.page == first.page
    again = ok(run_cli, "history", ids["n"], "--as-of", "tx:1")
    assert [i["rev"] for i in again.data["items"]] == [1]
    # an entity superseded later is still active then
    assert ok(run_cli, "get", ids["p"], "--as-of", "tx:1").data["entity"]["record_status"] == (
        "active"
    )
    assert ok(run_cli, "get", ids["p"], "--as-of", "tx:1").data["superseded_by"] == []
    assert ok(run_cli, "get", ids["q"], "--as-of", "tx:5").data["superseded_by"] == [ids["r"]]
    assert ok(run_cli, "get", ids["p"], "--as-of", "tx:5").data["superseded_by"] == []
    for command in ("lookup", "search"):
        past = twin(
            run_cli,
            lambda t, command=command: (command, t, "--as-of", "tx:1"),
            "Alpha topic",
            "Nothing",
        )
        assert past["code"] == 0 and past["data"]["items"] == []
    visible = ok(run_cli, "lookup", "First note", "--as-of", "tx:1")
    assert [i["id"] for i in visible.data["items"]] == [ids["n"]]
    # an entity that did not exist yet is missing, hidden or not
    scan = twin(run_cli, lambda ref: ("get", ref, "--as-of", "tx:1"), ids["scan"])
    assert scan["error"]["code"] == "entity.not_found"


# ---- changes -----------------------------------------------------------------------------


def feed(run_cli: CliRunner, *args: str, since: str = "tx:0", limit: int = 500) -> Outcome:
    return ok(run_cli, "changes", "--since", since, "--limit", str(limit), *args)


def listed(outcome: Outcome, world: World) -> list[tuple[int, list[tuple[str, int]]]]:
    """``[(tx_seq, [(local name, rev)])]`` of a feed page, the entities in feed order."""
    return [
        (e["tx_seq"], [(world.local(i["id"]), i["rev"]) for i in e["entities"]])
        for e in outcome.data["items"]
    ]


def items(outcome: Outcome, world: World) -> dict[tuple[str, int], dict[str, Any]]:
    return {
        (world.local(i["id"]), i["rev"]): i for e in outcome.data["items"] for i in e["entities"]
    }


def test_changes_list_the_visible_part_of_every_tx(world: World) -> None:
    run_cli = world.run_cli
    everything = feed(run_cli)
    assert everything.data["view"] is None and everything.data["more"] is False
    full = listed(everything, world)
    assert [seq for seq, _ in full] == [1, 2, 3, 4, 5, 6, 7]
    assert (6, [("hidden_file", 1)]) in full  # the tx that stored the hidden plain file
    raw_items = items(everything, world)
    world.limit()
    limited = feed(run_cli)
    assert limited.data["view"] is not None
    assert leaks(limited, world) == []
    # the feed of the view is the full one without the hidden entities; a tx with nothing
    # left has no entry, and ``next`` passes it
    hidden = {*HIDDEN_LOCALS, "hidden_file"}
    expected = [(seq, [e for e in ents if e[0] not in hidden]) for seq, ents in full]
    expected = [(seq, ents) for seq, ents in expected if ents]
    assert listed(limited, world) == expected
    assert 6 not in {seq for seq, _ in expected}
    assert (limited.data["head"], limited.data["next"]) == (7, "tx:7")
    assert limited.data["more"] is False
    assert (limited.data["since"], limited.data["since_tx_id"]) == ("tx:0", None)
    assert limited.data["next_tx_id"] == everything.data["next_tx_id"]  # random ids tell nothing
    shown = items(limited, world)
    # a note with edges to a hidden topic and a visible page: only the visible target, and a
    # revision that only linked a hidden topic is there with nothing changed
    assert shown[("n", 1)]["targets"] == [world.ids["w"]]
    assert sorted(raw_items[("n", 1)]["targets"]) == sorted([world.ids["t1"], world.ids["w"]])
    none = {"attrs": [], "edges": {"added": [], "removed": [], "changed": []}, "record": []}
    assert raw_items[("n", 2)]["changed"]["edges"]["added"] == ["nas:about"]
    assert (shown[("n", 2)]["changed"], shown[("n", 2)]["targets"]) == (none, [])
    assert shown[("o", 2)]["changed"]["attrs"] == ["title"]
    assert (shown[("o", 3)]["changed"], shown[("o", 3)]["targets"]) == (none, [])
    # a supersession by a hidden topic changes the status and nothing else; by a visible note
    # the note that replaces shows the link to the old one
    assert shown[("p", 2)]["record_status"] == "superseded"
    assert shown[("p", 2)]["changed"] == {**none, "record": ["record_status"]}
    assert ("t3", 2) in raw_items and ("t3", 2) not in shown
    assert shown[("r", 2)]["changed"]["edges"]["added"] == ["core:supersedes"]
    assert shown[("r", 2)]["targets"] == [world.ids["q"]]
    assert shown[("q", 2)]["record_status"] == "superseded"
    # the writer's words stay, as in history; the visible scan is there, the plain file not
    by_seq = {e["tx_seq"]: e for e in limited.data["items"]}
    assert by_seq[2]["reason"] == "one more topic" and by_seq[2]["command"] == "apply"
    assert [i["type"] for i in by_seq[7]["entities"]] == ["nas:Scan"]
    # the item lines agree with history of the same entity
    for local in ("n", "o", "p"):
        for item in ok(run_cli, "history", world.ids[local]).data["items"]:
            entry = by_seq[item["tx_seq"]]
            feed_item = next(i for i in entry["entities"] if i["id"] == world.ids[local])
            assert (feed_item["rev"], feed_item["changed"]) == (item["rev"], item["changed"])
            assert (entry["reason"], entry["command"]) == (item["reason"], item["command"])


def test_changes_page_over_visible_items_and_resume_through_every_cursor(world: World) -> None:
    run_cli = world.run_cli
    world.limit()
    whole = listed(feed(run_cli), world)
    flat = [(seq, entity) for seq, ents in whole for entity in ents]
    for limit in (1, 2, 3, 20):
        got: list[tuple[int, tuple[str, int]]] = []
        since, pages = "tx:0", 0
        while True:
            page = feed(run_cli, since=since, limit=limit)
            assert leaks(page, world) == [], limit
            pages += 1
            shown = [(seq, e) for seq, ents in listed(page, world) for e in ents]
            assert len(shown) <= limit  # counted over what the reader sees
            got += shown
            assert page.data["since"] == since
            since = page.data["next"]
            if not page.data["more"]:
                break
            assert shown or since != page.data["since"]
        assert got == flat, limit
        assert pages >= -(-len(flat) // limit), limit
    # a page ends before a tx that does not fit: its ``next`` is the whole tx before
    page = feed(run_cli, limit=9)  # the nine visible entities of tx 1 fill it
    assert (page.data["next"], page.data["more"]) == ("tx:1", True)
    assert [e["tx_seq"] for e in page.data["items"]] == [1]
    assert not page.data["items"][0].get("partial")


def test_a_cursor_inside_a_tx_is_a_sort_key_only(world: World) -> None:
    """The entity id of a cursor is never looked up: a hidden, a visible and an unknown id
    give the entities of that tx after it and the txs after, and no error."""
    run_cli = world.run_cli
    world.limit()
    first = feed(run_cli).data["items"][0]
    assert first["tx_seq"] == 1 and len(first["entities"]) > 2
    for local in (*HIDDEN_LOCALS, "hidden_file", "n", "w"):
        cursor = world.ids[local]
        page = ok(run_cli, "changes", "--since", f"tx:1/{cursor}", "--limit", "500")
        after = [i for i in first["entities"] if i["id"] > cursor]
        seqs = [e["tx_seq"] for e in page.data["items"]]
        assert seqs == [*([1] if after else []), 2, 3, 4, 5, 7], local
        if after:
            assert page.data["items"][0]["entities"] == after, local
    missing = ok(run_cli, "changes", "--since", f"tx:1/{NOT_THERE}", "--limit", "500")
    assert missing.data["items"][0]["entities"] == first["entities"]  # sorts before every id
    # the hidden id is echoed as given, like any value the caller asked with
    assert missing.data["since"] == f"tx:1/{NOT_THERE}"


def test_a_position_can_be_a_time_and_a_seq_above_the_head_is_refused(world: World) -> None:
    run_cli = world.run_cli
    world.limit()
    whole = feed(run_cli)
    second = next(e for e in whole.data["items"] if e["tx_seq"] == 2)
    by_time = feed(run_cli, since=second["recorded_at"])  # after the last tx at or before it
    assert by_time.data["since"] == "tx:2"
    assert [e["tx_seq"] for e in by_time.data["items"]] == [3, 4, 5, 7]
    assert by_time.data["since_tx_id"] == second["tx_id"]
    before = feed(run_cli, since="2000-01-01T00:00:00Z")
    assert before.data["since"] == "tx:0" and before.data["items"] == whole.data["items"]
    up_to_date = feed(run_cli, since="tx:7")
    assert (up_to_date.data["items"], up_to_date.data["more"]) == ([], False)
    assert up_to_date.data["since_tx_id"] == whole.data["next_tx_id"]
    # a hidden tx at the end: nothing to deliver, the same answer as at the head
    after_hidden = feed(run_cli, since="tx:6")
    assert [e["tx_seq"] for e in after_hidden.data["items"]] == [7]
    # above the head: the same refusal for a limited reader, naming the seq and the head
    refused = read(run_cli, "changes", "--since", "tx:8")
    assert (refused.code, refused.error["code"]) == (2, "usage.invalid_argument")
    assert refused.error["details"] == {"argument": "--since", "actual": "tx:8", "head_seq": 7}
    assert leaks(refused, world) == []


# ---- describe ----------------------------------------------------------------------------

SCHEMA_HIDDEN = (*HIDDEN_TERMS, '"Topic"', '"Book"', '"File"', '"about"', '"broader"')


def schema_leaks(outcome: Outcome, args: Sequence[str] = ()) -> list[str]:
    """The hidden names in a schema answer; the text a search asked for is the caller's own."""
    text = json.dumps(outcome.envelope)
    if args[:2] == ("describe", "search"):
        return [token for token in HIDDEN_TERMS if token in text]
    return [token for token in SCHEMA_HIDDEN if token in text]


def test_describe_shows_no_hidden_type_relation_or_field(world: World) -> None:
    run_cli = world.run_cli
    commands = [
        ("describe", "types"),
        ("describe", "type", "nas:Note"),
        ("describe", "type", "nas:Scan"),
        ("describe", "type", "nas:Source"),
        ("describe", "type", "nas:WebPage"),
        ("describe", "type", "provenance:Claim"),
        ("describe", "relations"),
        ("describe", "fields", "nas:Note"),
        ("describe", "fields", "nas:Scan"),
        ("describe", "lifecycle", "nas:Note"),
        ("describe", "schema"),
        ("describe", "modules"),
        ("describe", "module", "nas"),
        ("describe", "module", "files"),
        ("describe", "module", "provenance"),
        *(("describe", "search", text) for text in ("topic", "book", "file", "about", "broader")),
    ]
    raw = {args: ok(run_cli, *args) for args in commands}
    assert schema_leaks(raw[("describe", "types")]) != []  # the control: they are named raw
    assert schema_leaks(raw[("describe", "schema")]) != []
    world.limit()
    for args in commands:
        assert schema_leaks(ok(run_cli, *args), args) == [], args
    types = ok(run_cli, "describe", "types").data["types"]
    assert [t["curie"] for t in types] == [
        "nas:Note",
        "nas:Scan",
        "nas:Source",
        "nas:WebPage",
        "provenance:Claim",
    ]
    relations = ok(run_cli, "describe", "relations").data["relations"]
    names = [r["curie"] for r in relations]
    assert "nas:cites" in names and "nas:asserts" in names
    assert not {"nas:about", "nas:broader"} & set(names)
    cites_ = next(r for r in relations if r["curie"] == "nas:cites")
    assert cites_["domain"] == ["nas:Note"] and cites_["range"] == ["nas:Source"]
    # the relation of a type that is hidden does not show under the type that is not
    note_ = ok(run_cli, "describe", "type", "nas:Note").data["type"]
    assert "nas:about" not in json.dumps(note_)
    modules = {m["id"]: m for m in ok(run_cli, "describe", "modules").data["modules"]}
    raw_modules = {m["id"]: m for m in raw[("describe", "modules")].data["modules"]}
    assert modules["notes_and_sources"]["types"] < raw_modules["notes_and_sources"]["types"]
    assert modules["notes_and_sources"]["relations"] < raw_modules["notes_and_sources"]["relations"]
    assert modules["files"]["types"] == 0 and raw_modules["files"]["types"] == 4
    labels = [label["label"] for label in ok(run_cli, "describe", "schema").data["labels"]]
    assert labels == ["Note", "Scan", "Source", "WebPage", "Claim"]
    # the fields of a type that lost its hidden parent are all still there, declared on it
    scan = ok(run_cli, "describe", "type", "nas:Scan").data["type"]
    assert scan["extends"] is None
    assert {f["declared_in"] for f in scan["fields"]} == {"nas:Scan"}
    assert {"content", "media_type", "scanned_on"} <= {f["name"] for f in scan["fields"]}


def test_describing_a_hidden_term_is_describing_a_missing_one(limited: World) -> None:
    run_cli = limited.run_cli
    terms = [
        ("nas:Topic", "nas:Nothing"),
        ("nas:Book", "nas:Nothing"),
        ("files:File", "nas:Nothing"),
    ]
    terms += [("Topic", "Nothing"), ("Book", "Nothing"), ("File", "Nothing")]
    for hidden, other in terms:
        for sub in ("type", "fields", "lifecycle"):
            answer = twin(
                run_cli,
                lambda t, sub=sub: ("describe", sub, t),
                hidden,
                other,
                (hidden.split(":")[-1], "Nothing"),
                suggestions=False,
            )
            assert answer["error"]["code"] == "term.unknown", (sub, hidden)
            offered = answer["error"]["details"].get("did_you_mean", [])
            assert hidden not in offered and not {"nas:Topic", "nas:Book", "files:File"} & set(
                offered
            )
    # an enum, a datatype or a lifecycle that only hidden types use is as unknown as a missing one
    for sub in ("type", "fields"):
        for hidden in ("nas:about", "nas:broader"):
            answer = twin(
                run_cli,
                lambda t, sub=sub: ("describe", sub, t),
                hidden,
                "nas:nothing",
                (hidden.split(":")[1], "nothing"),
                suggestions=False,
            )
            assert answer["error"]["code"] == "term.unknown"
    for text in ("topic", "Topic", "about"):
        found_ = ok(run_cli, "describe", "search", text)
        assert not {"nas:Topic", "nas:about"} & {i["curie"] for i in found_.data["items"]}


# ---- files -------------------------------------------------------------------------------


def test_a_hidden_file_is_not_found_before_any_type_message(world: World) -> None:
    run_cli, ids = world.run_cli, world.ids
    unlimited = read(run_cli, "file", "path", ids["n"])  # the answer of a caller without limits
    assert unlimited.error["message"].endswith("is a nas:Note, not a stored file (files:File)")
    raw_check = ok(run_cli, "file", "check")
    assert raw_check.page is not None and raw_check.page["total"] == 2
    assert ok(run_cli, "file", "path", ids["hidden_file"]).data["type"] == "files:File"
    world.limit()
    hidden = ids["hidden_file"]
    for sub in (("path",), ("path", "--origin"), ("check",)):
        answer = twin(run_cli, lambda ref, sub=sub: ("file", *sub, ref), hidden)
        assert answer["error"]["code"] == "entity.not_found", sub
    alias = str(world.aliases["hidden_file"])
    for sub in (("path",), ("check",)):
        twin(run_cli, lambda ref, sub=sub: ("file", *sub, ref), alias, alias[:-1] + "9")
        twin(run_cli, lambda ref, sub=sub: ("file", *sub, ref), suffix_of(hidden), "0123456789")
    # an entity of another hidden type is not found either, not "not a stored file"
    for local in ("t1", "b"):
        answer = twin(run_cli, lambda ref: ("file", "path", ref), ids[local])
        assert answer["error"]["code"] == "entity.not_found", local
    # ... while a visible entity that is no file keeps the answer about its type
    note_ = read(run_cli, "file", "path", ids["n"])
    assert note_.error["code"] == "usage.invalid_argument"
    # ... which names the file classes the caller sees, never the hidden files:File
    assert note_.error["message"].endswith("is a nas:Note, not a stored file (nas:Scan)")
    assert leaks(note_, world) == []
    # among several refs a hidden file fails the call as a missing one does
    mixed = twin(run_cli, lambda ref: ("file", "check", ids["scan"], ref), hidden)
    assert mixed["error"]["code"] == "entity.not_found"


def test_files_of_visible_types_stay_and_the_listing_skips_the_hidden_ones(world: World) -> None:
    run_cli, ids = world.run_cli, world.ids
    world.limit()
    path = ok(run_cli, "file", "path", ids["scan"])
    assert path.data["type"] == "nas:Scan" and Path(path.data["path"]).is_file()
    one = ok(run_cli, "file", "check", ids["scan"])
    assert [i["id"] for i in one.data["items"]] == [ids["scan"]]
    listing = ok(run_cli, "file", "check")
    assert [i["id"] for i in listing.data["items"]] == [ids["scan"]]
    assert listing.page is not None and listing.page["total"] == 1
    assert sum(listing.data["counts"].values()) == 1
    assert leaks(listing, world) == []
    answer = twin(
        run_cli,
        lambda t: ("find", "--type", t),
        "files:File",
        "nas:Nothing",
        ("File", "Nothing"),
        suggestions=False,
    )
    assert answer["error"]["code"] == "term.unknown"


# ---- what nothing may reveal -------------------------------------------------------------


def test_a_hash_filter_cannot_tell_hidden_edges_from_none(world: World) -> None:
    run_cli, ids = world.run_cli, world.ids
    stored = ok(run_cli, "get", ids["n"], "--full").data["entity"]["content_hash"]
    world.limit()
    shown_hash = ok(run_cli, "get", ids["n"], "--full").data["entity"]["content_hash"]
    assert shown_hash != stored
    by_shown = ok(run_cli, "find", "--where", f"content_hash={shown_hash}", "--full")
    assert found(by_shown, world) == ["n"]
    by_stored = ok(run_cli, "find", "--where", f"content_hash={stored}", "--full")
    absent = ok(run_cli, "find", "--where", f"content_hash=sha256:{'0' * 64}", "--full")
    assert shown(by_stored) == shown(absent) and by_stored.data["items"] == []
    ordered = ok(run_cli, "find", "--sort", "content_hash", "--full", "--limit", "500")
    hashes = [i["content_hash"] for i in ordered.data["items"]]
    assert hashes == sorted(hashes)


SWEEP = [
    ("find", "--limit", "500", "--include-inactive", "--full"),
    ("find", "--type", "nas:Note", "--limit", "500", "--fields", "edges"),
    ("find", "--in-rel", "nas:cites", "--full"),
    ("find", "--in-rel", "core:supersedes", "--include-inactive"),
    ("lookup", "note", "--match", "prefix", "--limit", "500"),
    ("search", "note", "--limit", "500"),
    ("query", "MATCH (n) RETURN n"),
    ("query", "MATCH (a)-[r]->(b) RETURN a, r, b"),
    ("query", "MATCH (a)-[*1..3]-(b) RETURN a.id AS a, b.id AS b"),
    ("describe", "schema"),
    ("describe", "relations"),
    ("describe", "types"),
    ("file", "check"),
]


def test_nothing_hidden_appears_in_any_answer_about_the_rest(limited: World) -> None:
    """Every visible entity read in every way, and the listings: no hidden id, alias, label, term
    or file name anywhere. (The checks per command above say what each answer holds.)"""
    run_cli = limited.run_cli
    visible = [limited.ids[k] for k in ("n", "m", "k", "o", "p", "q", "r", "w", "w2", "scan")]
    commands: list[Sequence[str]] = list(SWEEP)
    for entity in visible:
        commands += [
            ("get", entity, "--full", "--affordances"),
            ("get", entity, "--rev", "1", "--full"),
            ("get", entity, "--as-of", "tx:1", "--full"),
            ("history", entity),
            ("neighbors", entity, "--direction", "both", "--depth", "3", "--full"),
            ("view", entity),
            ("view", entity, "--as-of", "tx:1"),
        ]
    for command in commands:
        outcome = read(run_cli, *command)
        assert outcome.code in (0, 1), command  # 1: a past revision that does not exist
        found_ = [token for token in leaks(outcome, limited) if token not in command]
        # the asked-for entity is a visible one; the hidden names must not appear at all
        assert found_ == [], command
