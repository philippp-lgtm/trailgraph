"""Chained JSONL codec (contracts C.2, C.3, B4.5, B4.11, B5.3)."""

import hashlib
import json

import pytest

from graph_cli.kernel.canonical import GENESIS_HASH, canonical_bytes, hash_document
from graph_cli.storage.common.chain import (
    GENESIS,
    ChainError,
    ChainLink,
    link_of,
    scan,
    seal,
    verify_line,
)


def _chain(count: int) -> tuple[list[dict[str, object]], bytes]:
    documents, data, link = [], b"", GENESIS
    for i in range(count):
        document, line = seal({"schema": "test/1", "n": i}, link)
        documents.append(document)
        data += line
        link = link_of(document)
    return documents, data


def test_genesis_line() -> None:
    document, line = seal({"schema": "test/1"}, GENESIS)
    assert document["seq"] == 1
    assert document["prev_hash"] == GENESIS_HASH
    assert document["hash"] == hash_document(document)
    assert line == canonical_bytes(document) + b"\n"


def test_continuity() -> None:
    documents, data = _chain(3)
    assert [d["seq"] for d in documents] == [1, 2, 3]
    assert documents[1]["prev_hash"] == documents[0]["hash"]
    result = scan(data)
    assert [line.document for line in result.lines] == documents
    assert result.end == ChainLink(3, documents[2]["hash"])  # type: ignore[arg-type]
    assert (result.tail, result.corruption, result.good_end) == ("clean", None, len(data))


def test_seal_refuses_chain_keys() -> None:
    with pytest.raises(ValueError, match="chain keys"):
        seal({"seq": 4}, GENESIS)


def test_scan_continues_from_a_link() -> None:
    _, data = _chain(3)
    first_end = data.index(b"\n") + 1
    head = scan(data[:first_end])
    rest = scan(data[first_end:], head.end)
    assert [line.document["seq"] for line in rest.lines] == [2, 3]
    assert rest.lines[0].start == 0


def test_non_canonical_line_is_invalid() -> None:
    document, _ = seal({"schema": "test/1", "b": 1, "a": 2}, GENESIS)
    pretty = json.dumps(document).encode() + b"\n"  # insertion order, spaces
    with pytest.raises(ChainError) as info:
        verify_line(pretty, GENESIS)
    assert info.value.kind == "invalid"


def test_a_hash_over_non_canonical_bytes_does_not_make_the_line_valid() -> None:
    body = b'{"b":1,"a":2,"prev_hash":"' + GENESIS_HASH.encode() + b'","seq":1}'
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    line = body[:-1] + b',"hash":"' + digest.encode() + b'"}\n'
    with pytest.raises(ChainError) as info:
        verify_line(line, GENESIS)
    assert info.value.kind == "invalid"


@pytest.mark.parametrize(
    "line",
    [b"not json\n", b"[1]\n", b"\n", b'{"seq":1}\n', b'{"hash":"x","prev_hash":"y","seq":1.5}\n'],
)
def test_malformed_lines_are_invalid(line: bytes) -> None:
    with pytest.raises(ChainError) as info:
        verify_line(line, GENESIS)
    assert info.value.kind == "invalid"


def test_hash_mismatch_is_invalid() -> None:
    _, line = seal({"schema": "test/1", "n": 1}, GENESIS)
    with pytest.raises(ChainError) as info:
        verify_line(line.replace(b'"n":1', b'"n":2'), GENESIS)
    assert info.value.kind == "invalid"


def test_discontinuity_is_broken() -> None:
    _, data = _chain(2)
    second = data[data.index(b"\n") + 1 :]
    with pytest.raises(ChainError) as info:
        verify_line(second, GENESIS)
    assert info.value.kind == "broken"
    wrong_prev = ChainLink(1, GENESIS_HASH)
    with pytest.raises(ChainError) as info:
        verify_line(second, wrong_prev)
    assert info.value.kind == "broken"


def test_torn_tail() -> None:
    _, data = _chain(3)
    torn = data[:-5]
    result = scan(torn)
    assert result.tail == "torn"
    assert result.corruption is None
    assert len(result.lines) == 2
    assert result.good_end == result.lines[-1].end


def test_invalid_last_line_is_a_tail() -> None:
    _, data = _chain(2)
    result = scan(data + b"garbage\n")
    assert (result.tail, result.corruption, len(result.lines)) == ("invalid", None, 2)
    assert result.good_end == len(data)


def test_tail_in_a_non_final_part_is_corruption() -> None:
    _, data = _chain(2)
    result = scan(data[:-3], final=False)
    assert result.corruption is not None
    assert result.corruption.expected_seq == 2


def test_interior_invalid_line_is_corruption() -> None:
    _, data = _chain(3)
    lines = data.splitlines(keepends=True)
    damaged = lines[0] + lines[1].replace(b'"n":1', b'"n":9') + lines[2]
    result = scan(damaged)
    assert result.corruption is not None
    assert result.corruption.offset == len(lines[0])
    assert result.corruption.expected_seq == 2
    assert [line.document["seq"] for line in result.lines] == [1]


def test_valid_last_line_out_of_chain_is_corruption() -> None:
    """Deleting a line leaves a well-formed but discontinuous last line: never truncated."""
    _, data = _chain(3)
    lines = data.splitlines(keepends=True)
    result = scan(lines[0] + lines[2])
    assert result.corruption is not None
    assert result.tail == "clean"


def test_empty_data() -> None:
    result = scan(b"")
    assert (result.lines, result.end, result.tail) == ((), GENESIS, "clean")
    assert result.corruption is None
