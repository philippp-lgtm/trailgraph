"""The ``data/head.json`` checkpoint (``graph_cli.head/1``, contracts L.3). Derived and disposable.

It is written only after every revision of every tx up to ``seq`` is
materialized, so readers may trust revision files up to it and overlay the
tx-log tail after it (B4.10, L.2).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from graph_cli.kernel.canonical import CanonicalError, JsonValue, parse_json, pretty_bytes
from graph_cli.kernel.clock import format_timestamp, parse_timestamp
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import TxId, parse_id
from graph_cli.kernel.json_access import (
    JsonShapeError,
    as_object,
    req_int,
    req_obj,
    req_str,
)
from graph_cli.kernel.ports.storage import Head
from graph_cli.storage.common.chain import ChainLink
from graph_cli.storage.files.segments import LogPosition

HEAD_SCHEMA: Final = "graph_cli.head/1"


@dataclass(frozen=True, slots=True)
class HeadFile:
    seq: int
    tx_id: TxId
    hash: str
    recorded_at: datetime
    txlog: LogPosition
    audit_link: ChainLink
    audit: LogPosition | None
    alias_counters: Mapping[str, int]

    @property
    def link(self) -> ChainLink:
        return ChainLink(self.seq, self.hash)

    def head(self) -> Head:
        return Head(self.seq, self.tx_id, self.hash, self.recorded_at)

    def to_bytes(self) -> bytes:
        audit: dict[str, JsonValue] = {"seq": self.audit_link.seq, "hash": self.audit_link.hash}
        if self.audit is not None:
            audit.update(segment=self.audit.segment, offset=self.audit.offset)
        document: dict[str, JsonValue] = {
            "schema": HEAD_SCHEMA,
            "seq": self.seq,
            "tx_id": str(self.tx_id),
            "hash": self.hash,
            "recorded_at": format_timestamp(self.recorded_at),
            "txlog": {"segment": self.txlog.segment, "offset": self.txlog.offset},
            "audit": audit,
            "alias_counters": dict(self.alias_counters),
        }
        return pretty_bytes(document)


def parse_head_file(data: bytes) -> HeadFile:
    """Parse ``head.json``; an unknown major is ``format.unsupported_version`` (V.3).

    Any other problem raises ``ValueError``: the checkpoint is disposable, so
    callers fall back to the tx log.
    """
    try:
        document = as_object(parse_json(data), "head")
        schema = req_str(document, "schema")
        if schema != HEAD_SCHEMA:
            raise GraphCliError(
                "format.unsupported_version",
                f"unsupported head schema {schema!r}",
                details={"expected": HEAD_SCHEMA, "actual": schema},
            )
        txlog = req_obj(document, "txlog")
        audit = req_obj(document, "audit")
        counters = req_obj(document, "alias_counters")
        audit_position = None
        if "segment" in audit:
            audit_position = LogPosition(
                req_str(audit, "segment", "audit."), req_int(audit, "offset", "audit.")
            )
        return HeadFile(
            seq=req_int(document, "seq"),
            tx_id=TxId(parse_id(req_str(document, "tx_id"))),
            hash=req_str(document, "hash"),
            recorded_at=parse_timestamp(req_str(document, "recorded_at")),
            txlog=LogPosition(
                req_str(txlog, "segment", "txlog."), req_int(txlog, "offset", "txlog.")
            ),
            audit_link=ChainLink(req_int(audit, "seq", "audit."), req_str(audit, "hash", "audit.")),
            audit=audit_position,
            alias_counters={key: req_int(counters, key, "alias_counters.") for key in counters},
        )
    except GraphCliError as exc:
        if exc.code == "format.unsupported_version":
            raise
        raise ValueError(f"invalid head.json: {exc.message}") from None
    except (CanonicalError, JsonShapeError) as exc:
        raise ValueError(f"invalid head.json: {exc}") from None
