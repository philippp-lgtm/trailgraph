"""Set views: one page of a view that takes no entity, and its cursor (plan V6).

A set view lists whole types and runs functions; its answer is a *page* cut at whole rows, in
section order, so the size of the answer is bounded however large the set is:

- rows are added until the next row would push the document over the page limit; a page always
  holds at least one row (a single row larger than the limit goes alone and the page says
  ``oversize``; values are never cut in a set view);
- a page loads at most ``PAGE_LOADS`` revisions (the rows and their neighbours) plus those of
  the one row that crosses the budget: the page ends after that row, at the same row every time
  (a row is never cut short by the budget, so it reads the same on every page it could be on);
- a page that does not reach the end has ``next`` (a cursor) and ``more``; a section that
  continues from the previous page says where it starts (``from``). The pages of a view put
  together are exactly the rows of the one big page.

A cursor is ``v1.<seq>.<section index>.<row offset>.<8 hex of the view hash and parameters>``:
the position of the next row, the graph state it was issued for and the view and parameters it
belongs to. A cursor of another state is ``view.cursor_stale`` (start again, or read the same
state with ``--as-of``), one of another view or other parameters is ``view.cursor_invalid``.
Same snapshot, same parameters, same page size: same bytes (the cache key of a consumer is
``(view hash, parameters, page bytes, cursor, graph.seq)``). Pure: the rows come through the
``Snapshot`` that is passed in, which is the reader's filtered one.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from typing import Final

from graph_cli.kernel.canonical import JsonValue, hash_value
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import TermRef
from graph_cli.kernel.ports.storage import Snapshot
from graph_cli.kernel.query_ast import Query
from graph_cli.kinetic.card import (
    ListSection,
    ListSpec,
    RowsSection,
    RowsSpec,
    Section,
    ViewModel,
    ViewSpec,
    dumps,
    rows_error,
)
from graph_cli.kinetic.view_format import PAGE_BYTES_MAX
from graph_cli.kinetic.view_rows import Loads, Row, RowBuilder, RowsResult
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.query.plan import MAX_LIMIT

PAGE_BYTES: Final = 12_288  # below the default spill limit: an agent's answer never spills
PAGE_LOADS: Final = 20_000  # revisions a page loads before it ends after the current row
CURSOR_VERSION: Final = "v1"
_CURSOR_RE: Final = re.compile(r"^v1\.(\d+)\.(\d+)\.(\d+)\.([0-9a-f]{8})$")
_ITEMS_KEY: Final = 12  # ``,"items":[]`` and the comma after a section
_UNBOUNDED: Final = 1 << 40  # a row's own neighbour reads are bounded by its groups, not here

type Placed = Row | Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True)
class Cursor:
    seq: int  # the graph state the cursor was issued for
    section: int  # the section of the next row
    offset: int  # the row offset of the next row inside that section
    key: str  # 8 hex digits of the view hash and the parameters

    def encode(self) -> str:
        return f"{CURSOR_VERSION}.{self.seq}.{self.section}.{self.offset}.{self.key}"


def cursor_key(view_hash: str, params: Mapping[str, JsonValue]) -> str:
    """The 8 hex digits that tie a cursor to a view definition and its parameter values."""
    digest = hash_value({"hash": view_hash, "params": dict(params)})
    return digest.removeprefix("sha256:")[:8]


def decode_cursor(text: str) -> Cursor:
    """The cursor ``text`` stands for; ``view.cursor_invalid`` when it is malformed."""
    found = _CURSOR_RE.match(text)
    if found is None:
        raise GraphCliError(
            "view.cursor_invalid",
            "the cursor is not one this program issues; start without --cursor",
            details={"cursor": text[:80]},
        )
    return Cursor(int(found[1]), int(found[2]), int(found[3]), found[4])


def check_cursor(
    cursor: Cursor, *, seq: int, key: str, sections: int, restart_only: bool = False
) -> None:
    """The cursor must belong to this view and these parameters (``view.cursor_invalid``), to
    this graph state (``view.cursor_stale``) and point into the view. ``restart_only``: the
    caller cannot read an earlier state (no ``--as-of``), so a stale cursor says to start again
    and names no sequence number."""
    if cursor.key != key or cursor.section >= sections:
        raise GraphCliError(
            "view.cursor_invalid",
            "the cursor belongs to another view or other parameters; start without --cursor",
            details={"reason": "view_or_parameters"},
        )
    if cursor.seq != seq and restart_only:
        raise GraphCliError(
            "view.cursor_stale",
            "the graph changed since the cursor was issued; start again without --cursor",
        )
    if cursor.seq != seq:
        raise GraphCliError(
            "view.cursor_stale",
            f"the graph changed since the cursor was issued (seq {cursor.seq}, now {seq}); "
            "start again without --cursor, or read the same state with --as-of tx:"
            f"{cursor.seq}",
            details={"cursor_seq": cursor.seq, "seq": seq},
        )


@dataclass(slots=True)
class _Block:
    """The rows of one section that are on the page."""

    index: int
    spec: ListSpec | RowsSpec
    total: int
    start: int
    items: list[Placed]

    def section(self, result: RowsResult | None) -> Section:
        items = tuple(self.items)
        if isinstance(self.spec, ListSpec):
            rows = tuple(i for i in items if isinstance(i, Row))
            return ListSection(
                self.spec.id, self.spec.types, rows, self.total, self.start, self.spec.title
            )
        function_hash = None if result is None else result.hash
        columns = () if result is None else result.columns
        cells = tuple(i for i in items if not isinstance(i, Row))
        return RowsSection(
            self.spec.id,
            self.spec.function,
            function_hash,
            columns,
            cells,
            self.total,
            title=self.spec.title,
            start=self.start,
            paged=True,
        )


def render_set(
    spec: ViewSpec,
    snapshot: Snapshot,
    model: CompiledModel,
    *,
    language: str | None = None,
    rows: Mapping[str, RowsResult] | None = None,
    page_bytes: int | None = None,
    cursor: Cursor | None = None,
    key: str = "",
    page_loads: int = PAGE_LOADS,
    restart_only: bool = False,
) -> ViewModel:
    """One page of the set view ``spec``: from the start, or from ``cursor`` (which is checked
    against ``snapshot.seq``, ``key`` and the view). ``rows`` are the results of the functions
    behind the ``rows`` sections, by section id, run by the caller."""
    limit = page_bytes if page_bytes is not None else spec.page_bytes or PAGE_BYTES
    if not 1 <= limit <= PAGE_BYTES_MAX:
        raise GraphCliError(
            "usage.invalid_argument",
            f"--page-bytes: expected 1..{PAGE_BYTES_MAX}",
            details={"argument": "--page-bytes", "actual": limit},
        )
    start_index, start_offset = 0, 0
    if cursor is not None:
        check_cursor(
            cursor,
            seq=snapshot.seq,
            key=key,
            sections=len(spec.sections),
            restart_only=restart_only,
        )
        start_index, start_offset = cursor.section, cursor.offset
    paged = _Paging(spec, snapshot, model, language, rows or {}, limit, page_loads, key)
    return paged.run(start_index, start_offset)


class _Paging:
    """The state of one page while it is filled."""

    def __init__(
        self,
        spec: ViewSpec,
        snapshot: Snapshot,
        model: CompiledModel,
        language: str | None,
        rows: Mapping[str, RowsResult],
        limit: int,
        page_loads: int,
        key: str,
    ) -> None:
        self.spec = spec
        self.snapshot = snapshot
        self.model = model
        self.language = language
        self.rows = rows
        self.limit = limit
        self.page_loads = page_loads
        self.key = key
        self.blocks: list[_Block] = []
        self.failed: dict[int, Section] = {}  # sections that answer with an error, by index
        self.placed = 0  # rows on the page
        self.used = 0  # revisions loaded for them
        self.stopped = False
        self.spent = self.overhead()  # a cautious estimate of the document so far

    def overhead(self) -> int:
        """The document without sections, with the longest ``next``, ``oversize`` and ``bytes``
        it can have."""
        worst = Cursor(self.snapshot.seq, len(self.spec.sections), 1 << 30, self.key.ljust(8, "0"))
        empty = ViewModel(
            self.spec.name,
            self.spec.origin,
            None,
            (),
            hash=self.spec.hash,
            next=worst.encode(),
            oversize=True,
        )
        return len(dumps(empty.to_json())) + 8

    def run(self, index: int, offset: int) -> ViewModel:
        position: tuple[int, int] | None = (index, offset)
        while position is not None and not self.stopped:
            position = self.fill(*position)
        return self.model_of(position)

    def fill(self, index: int, offset: int) -> tuple[int, int] | None:
        """Add the rows of one section from ``offset`` on. Returns where the page continues
        (``None``: the end of the view); ``stopped`` says the page is full."""
        section = self.spec.sections[index]
        if isinstance(section, RowsSpec):
            failed = rows_error(section, self.rows)
            if failed is not None:
                cost = len(dumps(failed.to_json())) + 1
                if self.placed and self.spent + cost > self.limit:
                    self.stopped = True
                    return index, 0
                self.failed[index] = failed
                self.spent += cost
                return self.after(index, offset, 0, 0)
            total, source = self.rows_source(section, offset)
        elif isinstance(section, ListSpec):
            total, source = self.list_source(section, offset)
        else:
            raise ValueError("an entity section belongs to an entity view")
        if offset > 0 and offset >= total:  # only a cursor can start inside a section
            raise GraphCliError(
                "view.cursor_invalid",
                "the cursor points past the end of its section; start without --cursor",
                details={"reason": "offset", "section": section.id, "count": total},
            )
        block = _Block(index, section, total, offset, [])
        result = self.rows.get(section.id) if isinstance(section, RowsSpec) else None
        skeleton = len(dumps(block.section(result).to_json())) + _ITEMS_KEY
        if total == 0 or offset >= total:  # an empty section is there, with its count of zero
            if self.placed and self.spent + skeleton > self.limit:
                self.stopped = True
                return index, 0
            self.blocks.append(block)
            self.spent += skeleton
            return self.after(index, offset, total, 0)
        opened = False
        for at, item, loads in source:
            size = len(dumps(item.to_json() if isinstance(item, Row) else dict(item))) + 1
            cost = size + (0 if opened else skeleton)
            if self.placed and self.spent + cost > self.limit:
                self.stopped = True
                return index, at
            if not opened:
                self.blocks.append(block)
                opened = True
            block.items.append(item)
            self.placed += 1
            self.used += loads
            self.spent += cost
            if self.spent > self.limit or self.used >= self.page_loads:
                self.stopped = True  # a lone oversize row, or the budget of loads is spent
                return self.after(index, at, total, 1)
        return self.after(index, offset, total, total - offset)

    def after(self, index: int, offset: int, total: int, taken: int) -> tuple[int, int] | None:
        """The position after ``taken`` rows from ``offset`` of section ``index``: the next row
        of the section, else the first of the next section, else the end."""
        following = offset + taken
        if taken and following < total:
            return index, following
        return (index + 1, 0) if index + 1 < len(self.spec.sections) else None

    # Sources: the rows of a section from an offset, with the loads each one costs

    def list_source(
        self, section: ListSpec, offset: int
    ) -> tuple[int, Iterator[tuple[int, Placed, int]]]:
        query = Query(
            types=tuple(TermRef(c) for c in section.classes),
            where=section.where,
            order=section.order,
            limit=MAX_LIMIT,
            offset=offset,
        )
        first = self.snapshot.query(query)

        def rows() -> Iterator[tuple[int, Placed, int]]:
            page, at = first, offset
            while True:
                for revision in page.items:
                    builder = RowBuilder(
                        self.snapshot, self.model, self.language, loads=Loads(_UNBOUNDED)
                    )
                    row = builder.row(
                        revision,
                        revision.id,
                        None,
                        section.fields,
                        section.relations,
                        section.with_type,
                    )
                    yield at, row, 1 + builder.loads.used
                    at += 1
                following = page.offset + len(page.items)
                if not page.items or following >= first.total:
                    return
                page = self.snapshot.query(replace(query, offset=following))

        return first.total, rows()

    def rows_source(
        self, section: RowsSpec, offset: int
    ) -> tuple[int, Iterator[tuple[int, Placed, int]]]:
        result = self.rows[section.id]

        def rows() -> Iterator[tuple[int, Placed, int]]:
            for at in range(offset, len(result.rows)):
                yield at, dict(result.rows[at]), 1

        return len(result.rows), rows()

    # The page

    def model_of(self, position: tuple[int, int] | None) -> ViewModel:
        blocks = list(self.blocks)
        page = self.assemble(blocks, position)
        # the estimate is cautious; the exact size has the last word (never below one row)
        while page.size() > self.limit and sum(len(b.items) for b in blocks) > 1:
            last = next(b for b in reversed(blocks) if b.items)
            last.items.pop()
            position = (last.index, last.start + len(last.items))
            blocks = blocks[: blocks.index(last) + 1]
            if not last.items:
                blocks.remove(last)
            page = self.assemble(blocks, position)
        if page.size() > self.limit:  # one row that is larger than the limit goes alone
            page = replace(page, oversize=True)
        return page

    def assemble(self, blocks: list[_Block], position: tuple[int, int] | None) -> ViewModel:
        done = {b.index: b.section(self.rows.get(b.spec.id)) for b in blocks}
        error = {i: s for i, s in self.failed.items() if position is None or i < position[0]}
        sections = [done[i] if i in done else error[i] for i in sorted({*done, *error})]
        nxt = None
        if position is not None:
            nxt = Cursor(self.snapshot.seq, position[0], position[1], self.key).encode()
        return ViewModel(
            self.spec.name, self.spec.origin, None, tuple(sections), hash=self.spec.hash, next=nxt
        )
