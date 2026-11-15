"""``graph-admin files move`` in the remote mode: short requests instead of one long one.

A move copies every stored file of a graph before it switches the graph, which can take far
longer than the client's timeout. So the client drives it (object storage O7):

1. a dry run of the command as given names the graphs, those already at the target and what
   each move covers (and is refused early for a target that is not configured);
2. for each graph to move, ``--step pass`` requests (each at most 30 s of copying on the
   server, answering what ``remaining``) until nothing remains;
3. then one ``--step switch`` request; when the copies under the graph lock ran out of time
   (files kept arriving), it switched nothing and answers ``copying``, and the client goes
   back to 2, at most ``MAX_SWITCHES`` times (then ``conflict.seq_mismatch``).

Passes and switches are safe to repeat: a pass only adds copies, a second switch finds the
graph at the target. The answer is one envelope like the local command's: the graphs with
their copies summed over every request. The first refused request ends the command with its
error and the graphs moved before it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final

from graph_cli.app_cli.remote import command_line
from graph_cli.catalog.remote import RemoteSession, Write
from graph_cli.cli_kit.argv import api_args
from graph_cli.cli_kit.envelope import Forwarded, Result
from graph_cli.cli_kit.spec import CommandSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError

PATH: Final = ("files", "move")
MAX_SWITCHES: Final = 10
MAX_IDLE_PASSES: Final = 3  # passes in a row that copy nothing although something remains

Send = Callable[[dict[str, JsonValue], bool], Mapping[str, JsonValue]]


def drives(spec: CommandSpec, args: Mapping[str, JsonValue]) -> bool:
    """Whether the client drives this request: a move that is no dry run and names no step."""
    return spec.path == PATH and args.get("--dry-run") is not True and "--step" not in args


def run(session: RemoteSession, spec: CommandSpec, args: dict[str, JsonValue]) -> Result:
    def send(request: dict[str, JsonValue], write: bool) -> Mapping[str, JsonValue]:
        repeat = Write(None, command_line(spec, request), repeatable=True) if write else None
        for attempt in range(1 if write else 2):  # the dry run is a read: sent again once
            try:
                document, _, _ = session.command(
                    spec.path, api_args(request), text=False, write=repeat, retry=False
                )
                return document
            except GraphCliError as exc:
                if exc.code != "remote.unreachable" or write or attempt:
                    raise
        raise GraphCliError("internal.error", "no attempt was made")

    return drive(send, args)


class _Refused(Exception):
    """A request of the move was refused; ``answer`` is its envelope."""

    def __init__(self, answer: Mapping[str, JsonValue]) -> None:
        super().__init__("refused")
        self.answer = answer


def drive(send: Send, args: Mapping[str, JsonValue]) -> Result:
    """The move of ``args`` (inventory names, no ``--dry-run``, no ``--step``) by requests."""
    plan = send({**args, "--dry-run": True}, False)
    if plan.get("error") is not None:
        return Result(forwarded=Forwarded(plan))
    base = {k: v for k, v in args.items() if k not in ("ref", "--all")}
    done: list[JsonValue] = []
    last = plan

    def data() -> dict[str, JsonValue]:
        return {"to": args.get("--to"), "dry_run": False, "step": None, "graphs": list(done)}

    for row in _graphs(plan):
        if row.get("status") != "planned":
            done.append(dict(row))
            continue
        try:
            moved, last = _move_one(send, {**base, "ref": [str(row["graph_id"])]})
        except _Refused as refused:
            return Result(forwarded=Forwarded({**refused.answer, "data": data()}))
        except GraphCliError as exc:
            return Result(data=data(), error=exc)
        done.append(moved)
    return Result(forwarded=Forwarded({**last, "data": data()}))


def _move_one(
    send: Send, args: dict[str, JsonValue]
) -> tuple[dict[str, JsonValue], Mapping[str, JsonValue]]:
    """Passes until nothing remains, then the switch, again while the switch ran out of
    time; the graph's row with the copies of every request, and the last answer."""
    copied = {"blobs": 0, "bytes": 0}
    passes = 0
    row: dict[str, JsonValue] = {}
    for _ in range(MAX_SWITCHES):
        idle = 0
        while True:
            answer = send({**args, "--step": "pass"}, True)
            row = _row(answer)
            _add(copied, row)
            passes += _int(row.get("passes"))
            if row.get("status") != "copying" or not _count(row, "remaining"):
                break
            idle = idle + 1 if not _count(row, "copied") else 0
            if idle >= MAX_IDLE_PASSES:
                raise _incomplete(row, "its copy passes make no progress")
        if row.get("status") == "copying":
            answer = send({**args, "--step": "switch"}, True)
            row = _row(answer)
            _add(copied, row)
        if row.get("status") in ("moved", "at_target"):
            return {**row, "copied": dict(copied), "passes": passes}, answer
    raise _incomplete(
        row,
        "files kept arriving faster than the move could copy them under the graph lock"
        f" ({MAX_SWITCHES} attempts)",
    )


def _incomplete(row: Mapping[str, JsonValue], why: str) -> GraphCliError:
    return GraphCliError(
        "conflict.seq_mismatch",
        f"graph {row.get('name')} was not moved: {why}; its stored files stay where they are"
        " and the copies made so far are kept, so run files move again",
        details={"graph_id": row.get("graph_id"), "remaining": row.get("remaining")},
    )


def _graphs(document: Mapping[str, JsonValue]) -> list[Mapping[str, JsonValue]]:
    data = document.get("data")
    graphs = data.get("graphs") if isinstance(data, Mapping) else None
    if not isinstance(graphs, list):
        return []
    return [row for row in graphs if isinstance(row, Mapping)]


def _row(document: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """The one graph a step request answers; ``_Refused`` for a refusal."""
    found = _graphs(document)
    if document.get("error") is not None or len(found) != 1:
        raise _Refused(document)
    return dict(found[0])


def _int(value: JsonValue) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _count(row: Mapping[str, JsonValue], key: str) -> int:
    value = row.get(key)
    return _int(value.get("blobs")) if isinstance(value, Mapping) else 0


def _add(total: dict[str, int], row: Mapping[str, JsonValue]) -> None:
    value = row.get("copied")
    if isinstance(value, Mapping):
        total["blobs"] += _int(value.get("blobs"))
        total["bytes"] += _int(value.get("bytes"))
