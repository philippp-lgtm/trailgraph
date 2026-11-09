"""A store-free tx log for unit tests of the shared storage core.

Runs the commit checks and sealing of ``storage.common`` and replays the
parsed lines into a ``GraphView``, exactly like the backends do, without any
backend around them.
"""

from dataclasses import dataclass, field
from functools import cache

from graph_cli.kernel.authz import AuthorizedTx
from graph_cli.kernel.canonical import parse_json
from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.json_access import as_object
from graph_cli.kernel.ports.storage import EMPTY_HEAD, Head
from graph_cli.kernel.record_codec import RecordCodec
from graph_cli.kernel.records import TxRecord
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.storage.common.chain import ChainLink
from graph_cli.storage.common.commit import check_tx, seal_tx
from graph_cli.storage.common.view import GraphView, ViewAt
from support.clock import StepClock
from support.fixtures import compiled_model
from support.ids import SeededIdProvider
from support.txbuilder import FuncState, TxBuilder, TxDraft


@cache
def model(name: str) -> CompiledModel:
    return compiled_model(name)


def view_state(view: ViewAt) -> FuncState:
    return FuncState(view.current, view.entity_ids)


@dataclass
class MemLog:
    ontology: str
    seed: int = 0
    view: GraphView = field(default_factory=GraphView)
    head: Head = EMPTY_HEAD
    lines: list[bytes] = field(default_factory=list)
    blobs: set[str] = field(default_factory=set)  # stored blob hashes

    def __post_init__(self) -> None:
        clock = StepClock()
        ids = SeededIdProvider(clock, self.seed)
        self.graph_id = GraphId(ids.new_id())
        self.codec = RecordCodec(self.graph_id)
        self.builder = TxBuilder(
            model(self.ontology), graph_id=self.graph_id, ids=ids, clock=clock, seed=self.seed
        )

    def at(self, seq: int | None = None) -> ViewAt:
        return self.view.at(seq)

    def draft(self) -> TxDraft:
        at = self.view.at()
        return self.builder.begin(view_state(at), self.head, at.alias_counters())

    def check(self, tx: AuthorizedTx) -> None:
        check_tx(
            tx,
            graph_id=self.graph_id,
            head=self.head,
            view=self.view.at(),
            blob_exists=self.blobs.__contains__,
            codec=self.codec,
        )

    def commit(self, tx: AuthorizedTx) -> TxRecord:
        self.check(tx)
        record, line = seal_tx(tx, ChainLink(self.head.seq, self.head.hash), self.codec)
        parsed = self.codec.parse_tx(as_object(parse_json(line), "tx"))
        assert parsed == record
        self.view.apply(parsed)
        self.lines.append(line)
        self.head = Head(record.seq, record.tx_id, record.hash, record.recorded_at)
        return record
