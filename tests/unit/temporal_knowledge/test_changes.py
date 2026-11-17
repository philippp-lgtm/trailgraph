"""The pure core of the change feed: positions, the body read of a tx, edge targets."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from support.snapshots import AT, TX, edge, eid, revision

from graph_cli.kernel.actor import ABSENT, ActorContext, ClientInfo, Entrypoint
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, EventId, GraphId
from graph_cli.kernel.records import OntologyPin, RequestMeta, Revision, TxRecord
from graph_cli.temporal_knowledge.changes import (
    TARGET_CAP,
    FeedPosition,
    parse_since,
    position_text,
    targets,
    touched,
)

ENTITY = UUID("0192f000-0000-7000-8000-0000000000b1")
T0 = datetime(2030, 1, 1, tzinfo=UTC)


def refused(text: str) -> GraphCliError:
    with pytest.raises(GraphCliError) as info:
        parse_since(text)
    assert info.value.code == "usage.invalid_argument"
    assert info.value.details["argument"] == "--since"
    assert info.value.details["actual"] == text
    assert info.value.message.startswith("--since: ")
    return info.value


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("tx:0", FeedPosition(0)),
        ("tx:41", FeedPosition(41)),
        ("tx:007", FeedPosition(7)),
        (f"tx:3/{ENTITY}", FeedPosition(3, EntityId(ENTITY))),
        (f"tx:3/{str(ENTITY).upper()}", FeedPosition(3, EntityId(ENTITY))),
        (f"tx:3/urn:uuid:{ENTITY}", FeedPosition(3, EntityId(ENTITY))),
        ("2030-01-01T00:00:00Z", T0),
        ("2030-01-01T02:00:00.5+02:00", T0 + timedelta(milliseconds=500)),
    ],
)
def test_parse_since_accepts_every_form(text: str, expected: FeedPosition | datetime) -> None:
    assert parse_since(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "41",
        "tx:",
        "tx:-1",
        "tx:+1",
        "tx:1.5",
        "tx: 1",
        "tx:1 ",
        "tx:\u0661",  # an Arabic-Indic digit one
        "tx:" + "9" * 19,
        "TX:1",
        "tx:1/",
        "tx:1/not-a-uuid",
        f"tx:1/{ENTITY}/{ENTITY}",
        f"tx:1/{str(ENTITY).replace('-', '')}",
        f"tx:0/{ENTITY}",
        f"/{ENTITY}",
        "2030-01-01T00:00:00",  # no offset
        "2030-01-01",
        "yesterday",
    ],
)
def test_parse_since_refuses_malformed_text(text: str) -> None:
    refused(text)


def test_refusals_name_the_argument_and_what_is_expected() -> None:
    error = refused("tx:x")
    assert "--as-of" not in error.message
    assert "tx:<seq>/<entity id>" in str(error.details["expected"])
    assert "--as-of" not in refused("2030-01-01T00:00:00").message


def test_the_cursor_id_is_a_sort_key_never_looked_up() -> None:
    # any well-formed id parses, whether or not an entity of that id exists anywhere
    assert parse_since(f"tx:2/{eid('unknown')}") == FeedPosition(2, eid("unknown"))


@pytest.mark.parametrize(
    "position",
    [FeedPosition(0), FeedPosition(41), FeedPosition(3, EntityId(ENTITY))],
)
def test_position_text_round_trips(position: FeedPosition) -> None:
    text = position_text(position)
    assert parse_since(text) == position
    assert position_text(parse_since(text)) == text  # type: ignore[arg-type]


def test_position_text_is_canonical() -> None:
    assert position_text(FeedPosition(7)) == "tx:7"
    assert position_text(FeedPosition(3, EntityId(ENTITY))) == f"tx:3/{ENTITY}"
    assert position_text(parse_since("tx:007")) == "tx:7"  # type: ignore[arg-type]
    upper = f"tx:3/{str(ENTITY).upper()}"
    assert position_text(parse_since(upper)) == f"tx:3/{ENTITY}"  # type: ignore[arg-type]


def test_a_position_is_never_negative_or_inside_tx_zero() -> None:
    with pytest.raises(ValueError, match="not negative"):
        FeedPosition(-1)
    with pytest.raises(ValueError, match="tx"):
        FeedPosition(0, EntityId(ENTITY))


def make_tx(*revisions: Revision) -> TxRecord:
    return TxRecord(
        graph_id=GraphId(ENTITY),
        tx_id=TX,
        seq=1,
        recorded_at=AT,
        event_id=EventId(ENTITY),
        actor=ActorContext(
            principal=ABSENT,
            agent=ABSENT,
            session=ABSENT,
            device=ABSENT,
            project=ABSENT,
            client=ClientInfo("graph", "0.1.0", Entrypoint.APP),
            pid=None,
        ),
        ontology=OntologyPin("example", "1.0.0", "sha256:" + "a" * 64),
        request=RequestMeta("create", "sha256:" + "b" * 64),
        reason="secret reason",
        idempotency_key=None,
        changeset={"schema": "graph_cli.changeset/1", "ops": [{"secret": "changeset body"}]},
        changeset_hash="sha256:" + "c" * 64,
        resolution={"locals": {}, "refs": {}},
        revisions=revisions,
        alias_allocations=((revisions[0].id, "SECRET-0001"),) if revisions else (),
        prev_hash="sha256:" + "0" * 64,
        hash="sha256:" + "d" * 64,
    )


def test_touched_is_sorted_by_id_and_holds_ids_and_revs_only() -> None:
    mine = [
        revision("a", "ex:Thing", {"label": "secret value"}, [edge("ex:keeps", "b")]),
        revision("b", "ex:Thing", rev=3),
        revision("c", "ex:Thing", rev=2),
    ]
    found = touched(make_tx(*reversed(mine)))
    assert found == tuple(sorted((r.id, r.rev) for r in mine))
    assert [entity for entity, _ in found] == sorted(entity for entity, _ in found)
    assert all(type(entity) is UUID and type(rev) is int for entity, rev in found)
    for text in ("secret", "changeset body", "SECRET-0001", "ex:Thing", "ex:keeps"):
        assert text not in repr(found)


def test_touched_of_a_tx_without_revisions_is_empty() -> None:
    assert touched(make_tx()) == ()


def test_targets_of_a_creation_are_all_its_edge_targets() -> None:
    created = revision("a", "ex:Thing", edges=[edge("ex:keeps", "c"), edge("ex:holds", "b")])
    assert targets(None, created) == (tuple(sorted([eid("b"), eid("c")])), 0)


def test_targets_of_a_creation_without_edges_are_empty() -> None:
    assert targets(None, revision("a", "ex:Thing")) == ((), 0)


def test_targets_unique_when_two_edges_end_at_one_entity() -> None:
    created = revision("a", "ex:Thing", edges=[edge("ex:keeps", "b"), edge("ex:holds", "b")])
    assert targets(None, created) == ((eid("b"),), 0)


def test_targets_of_added_and_removed_edges() -> None:
    kept = edge("ex:keeps", "b")
    before = revision("a", "ex:Thing", edges=[kept, edge("ex:drops", "c")])
    after = replace(before, rev=2, edges=(kept, edge("ex:adds", "d")))
    assert targets(before, after) == (tuple(sorted([eid("c"), eid("d")])), 0)


def test_targets_of_a_changed_edge_name_both_ends() -> None:
    moves = edge("ex:moves", "b", name="one")
    before = revision("a", "ex:Thing", edges=[moves, edge("ex:keeps", "e")])
    after = replace(before, rev=2, edges=(replace(moves, target=eid("c")), before.edges[1]))
    assert targets(before, after) == (tuple(sorted([eid("b"), eid("c")])), 0)


def test_targets_of_a_changed_qualifier() -> None:
    before = revision("a", "ex:Thing", edges=[edge("ex:keeps", "b", name="one", role="x")])
    after = replace(before, rev=2, edges=(edge("ex:keeps", "b", name="one", role="y"),))
    assert targets(before, after) == ((eid("b"),), 0)


def test_targets_ignore_unchanged_edges_and_other_changes() -> None:
    before = revision("a", "ex:Thing", {"label": "x"}, [edge("ex:keeps", "b")])
    after = replace(before, rev=2, attrs={"label": "y"})
    assert targets(before, after) == ((), 0)


def test_targets_are_capped_with_a_remainder() -> None:
    names = [f"t{n:03d}" for n in range(TARGET_CAP + 7)]
    created = revision("a", "ex:Thing", edges=[edge("ex:keeps", name) for name in names])
    listed, more = targets(None, created)
    everything = sorted(eid(name) for name in names)
    assert listed == tuple(everything[:TARGET_CAP])
    assert more == 7


def test_targets_at_the_cap_have_no_remainder() -> None:
    names = [f"t{n:03d}" for n in range(TARGET_CAP)]
    created = revision("a", "ex:Thing", edges=[edge("ex:keeps", name) for name in names])
    listed, more = targets(None, created)
    assert len(listed) == TARGET_CAP
    assert more == 0


def test_targets_cap_is_a_parameter() -> None:
    created = revision("a", "ex:Thing", edges=[edge("ex:keeps", n) for n in "bcd"])
    listed, more = targets(None, created, cap=2)
    assert len(listed) == 2
    assert more == 1
