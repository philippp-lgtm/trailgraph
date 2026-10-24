"""Audit stream verification (contracts B5.3, B5.9, B5.12, V.5).

Pure: input are the raw lines of ``AuditSink.read_lines``. Damage is reported
as ``integrity.audit.*`` findings and verification goes on, like ``fsck``.
Every damaged line yields one chain finding at most, so one tampered line
does not cascade into findings for every later line:

- a torn or unparseable **last** line is a tail and no finding (B5.9);
- an unparseable interior line is ``torn_interior_line``; the next line may
  skip one seq and its ``prev_hash`` is not checked;
- ``seq_gap`` wins over ``chain_broken``; after either, the chain continues
  from the line as stored;
- ``hash_mismatch`` (content edited) is independent of the chain position;
  so are ``unknown_schema``, ``event_malformed`` and ``unknown_action``.

``cross_check`` matches write intents with the tx log (ids, changeset hash
and, when recorded, changeset size; B5.6, B5.12),
``check_maintenance`` judges maintenance intents by their effects and walks
the pin and graph policy events against the manifest and the installed
policy, ``check_policy`` walks the installation policy events (B5.15),
``check_policy_hashes`` checks every graph-stream decision against the
policy in effect, and ``check_anchors`` matches installation anchors with
graph streams (B5.11).
``verify_graph`` and ``verify_installation`` compose them for
``graph-admin audit verify``. The caller passes the txs only when storage
verified the tx log clean, so a damaged log does not cascade into
``tx_without_intent`` findings.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from graph_cli.audit import finding_codes as codes
from graph_cli.audit.events import changeset_size
from graph_cli.audit.schema import (
    AUDIT_SCHEMA,
    MAINT_EFFECT_ACTIONS,
    PIN_ACTION,
    POLICY_ACTION,
    RELEASE_ACTION,
    event_problems,
    graph_of_stream,
    is_write_intent,
    unknown_actions,
)
from graph_cli.kernel.canonical import (
    GENESIS_HASH,
    CanonicalError,
    JsonValue,
    hash_document,
    hash_value,
    jsonl_line,
    parse_json,
)
from graph_cli.kernel.findings import Finding
from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM, AuditLine, AuditStream, graph_stream
from graph_cli.kernel.records import OntologyPin, TxRecord


@dataclass(frozen=True, slots=True)
class InstalledPolicy:
    """An installed policy file as found: the C.2 hash of its document, ``None`` when there
    is no file, ``"unreadable"`` when it is not a JSON object (B5.15)."""

    observed: str | None

    @classmethod
    def of(cls, data: bytes | None) -> InstalledPolicy:
        if data is None:
            return cls(None)
        try:
            document = parse_json(data)
        except CanonicalError:
            return cls(UNREADABLE)
        return cls(hash_value(document) if isinstance(document, Mapping) else UNREADABLE)


UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class MaintEffects:
    """The maintenance effects found on disk, for judging maintenance intents (B5.6).

    ``manifest_pin`` is ``None`` when it cannot be trusted (storage reported
    ``integrity.storage.pin_mismatch``); the pin walk is then skipped.
    ``anchored`` are the graph-stream seqs named by companion anchors
    (``effect_anchors``): a companion is appended only after its effect, so
    an anchored intent is committed (W.e). ``policy`` is the graph's installed
    policy; ``None`` skips the policy walk.
    """

    manifest_pin: OntologyPin | None
    releases: Mapping[str, str]  # installed version -> artifact hash of its compiled.json
    anchored: frozenset[int] = frozenset()
    policy: InstalledPolicy | None = None


@dataclass(frozen=True, slots=True)
class StreamReport:
    stream: AuditStream
    findings: tuple[Finding, ...]
    events: tuple[Mapping[str, JsonValue], ...]  # every parseable event, in stream order
    head: tuple[int, str]  # seq and hash of the last event as stored


def verify_stream(
    lines: Iterable[AuditLine], *, stream: AuditStream, ext_actions: Iterable[str] = ()
) -> StreamReport:
    known_ext = tuple(ext_actions)
    findings: list[Finding] = []
    events: list[Mapping[str, JsonValue]] = []
    last_seq, last_hash = 0, GENESIS_HASH
    after_torn = False
    items = list(lines)

    def add(
        code: str,
        target: Mapping[str, JsonValue],
        message: str,
        *,
        expected: JsonValue | None = None,
        actual: JsonValue | None = None,
    ) -> None:
        findings.append(_finding(code, target, message, expected=expected, actual=actual))

    for index, line in enumerate(items):
        event = _parse(line.data)
        if event is None:
            if index == len(items) - 1:
                break  # torn or unreadable tail: readers ignore it, the next writer truncates
            add(
                codes.TORN_INTERIOR_LINE,
                {"stream": stream, "position": line.position},
                "line is not a complete JSON object",
            )
            after_torn = True
            continue
        seq = event.get("seq")
        expected_seq = last_seq + (2 if after_torn else 1)
        target: dict[str, JsonValue] = {"stream": stream, "position": line.position}
        if isinstance(seq, int) and not isinstance(seq, bool):
            target = {"stream": stream, "seq": seq}
            next_seq = seq
        else:
            next_seq = expected_seq
        if seq != expected_seq or "position" in target:
            add(
                codes.SEQ_GAP,
                target,
                "seq does not follow the previous event",
                expected=expected_seq,
                actual=seq,
            )
        elif not after_torn and event.get("prev_hash") != last_hash:
            add(
                codes.CHAIN_BROKEN,
                target,
                "prev_hash is not the previous event's hash",
                expected=last_hash,
                actual=event.get("prev_hash"),
            )
        elif event.get("stream") != stream:
            add(
                codes.CHAIN_BROKEN,
                target,
                "event belongs to another stream",
                expected=stream,
                actual=event.get("stream"),
            )
        stored_hash = event.get("hash")
        if hash_document(event) != stored_hash:
            add(codes.HASH_MISMATCH, target, "event content does not match its hash")
        elif jsonl_line(event) != line.data:
            add(codes.EVENT_MALFORMED, target, "line is not canonical JSON")
        if event.get("schema") != AUDIT_SCHEMA:
            add(codes.UNKNOWN_SCHEMA, target, "unknown event schema", actual=event.get("schema"))
        else:
            problems = event_problems(event)
            if problems:
                add(codes.EVENT_MALFORMED, target, "; ".join(problems))
            unknown = unknown_actions(event, known_ext)
            if unknown:
                add(codes.UNKNOWN_ACTION, target, f"unknown actions: {', '.join(unknown)}")
        last_seq = next_seq
        last_hash = stored_hash if isinstance(stored_hash, str) else ""
        after_torn = False
        events.append(event)
    return StreamReport(stream, tuple(findings), tuple(events), (last_seq, last_hash))


def cross_check(
    graph_id: GraphId, events: Iterable[Mapping[str, JsonValue]], txs: Iterable[TxRecord]
) -> list[Finding]:
    """Every tx has its ``ok`` write intent; every write intent has its tx or is benign.

    Each intent serves one tx. A retried request may repeat an event id; the
    tx then takes the latest unused intent and the earlier one stays an orphan.
    """
    stream = graph_stream(str(graph_id))
    intents = [event for event in events if is_write_intent(event)]
    by_id: dict[JsonValue, list[int]] = {}
    for index, event in enumerate(intents):
        by_id.setdefault(event.get("event_id"), []).append(index)
    used: set[int] = set()
    findings = []
    for tx in txs:
        target: dict[str, JsonValue] = {"graph_id": str(graph_id), "tx_seq": tx.seq}
        free = [i for i in by_id.get(str(tx.event_id), ()) if i not in used]
        if not free:
            findings.append(
                _finding(codes.TX_WITHOUT_INTENT, target, f"no write intent {tx.event_id}")
            )
            continue
        used.add(free[-1])
        event = intents[free[-1]]
        change = event.get("change")
        if not isinstance(change, Mapping):
            change = {}  # a malformed intent: event_malformed already, and no match here
        expected: dict[str, JsonValue] = {
            "tx_id": str(tx.tx_id),
            "tx_seq": tx.seq,
            "changeset_hash": tx.changeset_hash,
        }
        if "changeset_size" in change:  # intents before the size was recorded lack it
            expected["changeset_size"] = changeset_size(tx.changeset)
        recorded = {k: change.get(k) for k in expected}
        if recorded != expected:
            differing = sorted(k for k in expected if recorded[k] != expected[k])
            findings.append(
                _finding(
                    codes.TX_WITHOUT_INTENT,
                    target,
                    f"write intent {tx.event_id} differs in {', '.join(differing)}",
                    expected={k: expected[k] for k in differing},
                    actual={k: recorded[k] for k in differing},
                )
            )
    for index, event in enumerate(intents):
        if index not in used:
            findings.append(
                _finding(
                    codes.INTENT_WITHOUT_EFFECT,
                    {"stream": stream, "seq": event.get("seq")},
                    "write intent without a committed tx (an interrupted write)",
                )
            )
    return findings


def check_maintenance(
    graph_id: GraphId, events: Iterable[Mapping[str, JsonValue]], effects: MaintEffects
) -> list[Finding]:
    """Release intents need their release, pin intents their manifest (B5.6, B5.12).

    Pin intents record the manifest pin before (``from``, read under the
    lock) and after (``to``). Walking them from the genesis pin, an intent
    is committed if the next observed pin (the next intent's ``from``, at
    the end the manifest) equals its ``to``; it is an interrupted op if that
    pin is still its ``from`` and no anchor proves the effect; anything else
    means the manifest changed outside maintenance (``pin_mismatch``, after
    which no pin intent is judged: no cascades).
    """
    stream = graph_stream(str(graph_id))
    ok = [e for e in events if _ok(e)]
    findings = []
    for event, release in _effects(ok, RELEASE_ACTION, "release"):
        if event.get("seq") in effects.anchored:
            continue
        if effects.releases.get(str(release.get("version"))) != release.get("artifact_hash"):
            findings.append(
                _finding(
                    codes.INTENT_WITHOUT_EFFECT,
                    {"stream": stream, "seq": event.get("seq")},
                    "release intent without its installed release (an interrupted release)",
                )
            )
    if effects.manifest_pin is not None:
        findings += _pin_walk(stream, ok, effects)
    if effects.policy is not None:
        findings += _policy_walk(stream, ok, effects.policy, effects.anchored)
    return findings


def check_policy(
    events: Iterable[Mapping[str, JsonValue]], installed: InstalledPolicy
) -> list[Finding]:
    """The installation policy against the installation stream's policy events (B5.15).

    An intent is proven committed by its confirmation: the next policy event
    without ``change`` and without ``graph``, appended after the rename (the
    installation counterpart of a W.e companion). Without it, a deleted first
    policy would look like an interrupted apply.
    """
    ok = [e for e in events if _ok(e)]
    confirmed: set[int] = set()
    pending: int | None = None
    for event in ok:
        if event.get("actions") != [POLICY_ACTION] or "graph" in event:
            continue
        seq = event.get("seq")
        if isinstance(event.get("change"), Mapping):
            pending = seq if isinstance(seq, int) else None
        elif pending is not None:
            confirmed.add(pending)
            pending = None
    return _policy_walk(INSTALLATION_STREAM, ok, installed, frozenset(confirmed))


TOKENS_FILE: Final = "tokens/tokens.json"
TOKEN_CREATE: Final = "maint.token_create"
TOKEN_REVOKE: Final = "maint.token_revoke"


def check_tokens(
    events: Iterable[Mapping[str, JsonValue]],
    installed: Mapping[str, Mapping[str, JsonValue]] | None,
) -> list[Finding]:
    """The token file against the installation stream's token events (B14.11, B5.12).

    Replaying ``maint.token_create`` and ``maint.token_revoke`` in seq order gives the
    live tokens. ``installed`` maps each name in the file to ``{hash_prefix, project,
    expires, scope}``; ``None`` when the file cannot be read. An event without a scope
    created an app token. A file token that is not live,
    or differs, was added by hand or survived an interrupted revoke
    (``token_mismatch``); a live token missing from the file is an interrupted create.
    """
    live: dict[str, tuple[JsonValue, Mapping[str, JsonValue]]] = {}
    for event in events:
        change = event.get("change")
        token = change.get("token") if _ok(event) and isinstance(change, Mapping) else None
        if not isinstance(token, Mapping) or not isinstance(token.get("name"), str):
            continue
        name = str(token["name"])
        if event.get("actions") == [TOKEN_CREATE]:
            live[name] = (event.get("seq"), token)
        elif event.get("actions") == [TOKEN_REVOKE]:
            live.pop(name, None)
    where: dict[str, JsonValue] = {"stream": INSTALLATION_STREAM, "path": TOKENS_FILE}
    if installed is None:
        return [
            _finding(
                codes.TOKEN_MISMATCH,
                where,
                "the token file cannot be read as a token document",
                actual="unreadable",
            )
        ]
    findings = []
    for name, entry in sorted(installed.items()):
        actual = {key: entry.get(key) for key in ("hash_prefix", "project", "expires")}
        actual["scope"] = entry.get("scope") or "app"
        found = live.get(name)
        expected = None
        if found is not None:
            expected = {key: found[1].get(key) for key in actual}
            expected["scope"] = found[1].get("scope") or "app"
        if expected != actual:
            findings.append(
                _finding(
                    codes.TOKEN_MISMATCH,
                    {**where, "name": name},
                    f"token {name} in the token file differs from its events (added by hand,"
                    " or a revoke was interrupted: revoke it again)",
                    expected=expected,
                    actual=actual,
                )
            )
    for name, (seq, _) in sorted(live.items()):
        if name not in installed:
            findings.append(
                _finding(
                    codes.INTENT_WITHOUT_EFFECT,
                    {"stream": INSTALLATION_STREAM, "seq": seq},
                    f"token {name} was created without reaching the token file"
                    " (an interrupted create)",
                )
            )
    return findings


@dataclass(frozen=True, slots=True)
class InstallationChange:
    """One installation policy intent of the installation stream (B5.15).

    ``mirrored`` are the graphs its confirmation anchors (their mirror events);
    empty for a change applied before mirrors existed, ``None`` when it has no
    confirmation (an apply interrupted before it, committed or not).
    """

    seq: int
    old: str | None
    new: str
    committed: bool
    mirrored: frozenset[str] | None

    def expects_mirror(self, graph_id: str) -> bool:
        return self.committed and (self.mirrored is None or graph_id in self.mirrored)

    def unmirrored(self, graph_id: str) -> bool:
        return self.committed and self.mirrored is not None and graph_id not in self.mirrored


def installation_changes(
    events: Iterable[Mapping[str, JsonValue]], installed: InstalledPolicy
) -> list[InstallationChange]:
    """The installation policy history. An intent is committed when confirmed, or when it is
    the last one and the installed file is its result (an apply interrupted after the
    rename)."""
    found: list[InstallationChange] = []
    pending: InstallationChange | None = None
    for event in (e for e in events if _ok(e)):
        if event.get("actions") != [POLICY_ACTION] or "graph" in event:
            continue
        change, seq = event.get("change"), event.get("seq")
        policy = change.get("policy") if isinstance(change, Mapping) else None
        if isinstance(policy, Mapping) and isinstance(seq, int):
            if pending is not None:
                found.append(pending)
            old, new = policy.get("from"), policy.get("to")
            if isinstance(new, str) and (old is None or isinstance(old, str)):
                pending = InstallationChange(seq, old, new, False, None)
        elif pending is not None:
            anchors = event.get("anchors")
            graphs = frozenset(
                str(a.get("graph_id"))
                for a in (anchors if isinstance(anchors, list) else [])
                if isinstance(a, Mapping)
            )
            found.append(dataclasses.replace(pending, committed=True, mirrored=graphs))
            pending = None
    if pending is not None:
        found.append(dataclasses.replace(pending, committed=installed.observed == pending.new))
    return found


def graph_creations(installation_events: Iterable[Mapping[str, JsonValue]]) -> dict[str, int]:
    """Per graph id, the installation-stream seq of its ``maint.graph_create`` event."""
    found: dict[str, int] = {}
    for event in installation_events:
        anchors, seq = event.get("anchors"), event.get("seq")
        if event.get("actions") != ["maint.graph_create"] or not isinstance(anchors, list):
            continue
        for anchor in anchors:
            if isinstance(anchor, Mapping) and isinstance(seq, int):
                found.setdefault(str(anchor.get("graph_id")), seq)
    return found


def policy_set_hash(installation: str, graph: str | None) -> str:
    """The hash a decision records for these layers (B6.14)."""
    return (
        installation
        if graph is None
        else hash_value({"installation": installation, "graph": graph})
    )


def check_policy_hashes(
    graph_id: str,
    events: Sequence[Mapping[str, JsonValue]],
    changes: Sequence[InstallationChange],
    *,
    created: int | None,
    anchored: frozenset[int],
    builtin_hash: str,
) -> list[Finding]:
    """Every decision of one graph stream against the policy in effect (B5.15, B6.14).

    The installation layer in effect starts as the one committed before the
    graph was created and follows the stream's mirror events, which the
    installation stream's history must contain in order; the graph layer
    follows the stream's committed graph policy intents. A graph-admin
    decision without an installed layer (its fallback) is a warning. Before
    the first mirror of a graph that saw changes applied without mirrors,
    any policy of that time is accepted (info).
    """
    stream = graph_stream(graph_id)
    after = [c for c in changes if created is None or c.seq > created]
    before = [c for c in changes if c.committed and created is not None and c.seq < created]
    installation = before[-1].new if before else builtin_hash
    legacy = [c.new for c in after if c.unmirrored(graph_id)]
    loose = {installation, *legacy} if legacy else None
    queue = [c for c in after if c.expects_mirror(graph_id) or not c.committed]
    layer: str | None = None
    findings: list[Finding] = []
    unchecked = 0
    for event in events:
        target: dict[str, JsonValue] = {"stream": stream, "seq": event.get("seq")}
        authz = event.get("authz")
        actual = authz.get("policy_hash") if isinstance(authz, Mapping) else None
        expected = policy_set_hash(installation, layer)
        if isinstance(actual, str) and actual != expected:
            fallback = {policy_set_hash(builtin_hash, layer), installation, builtin_hash}
            if loose is not None and actual in {policy_set_hash(h, layer) for h in loose}:
                unchecked += 1
            elif _entrypoint(event) == "maint" and actual in fallback:
                findings.append(
                    _finding(
                        codes.POLICY_FALLBACK,
                        target,
                        "graph-admin decided without an installed policy layer",
                        expected=expected,
                        actual=actual,
                    )
                )
            else:
                findings.append(
                    _finding(
                        codes.POLICY_HASH_MISMATCH,
                        target,
                        "the decision was made under a policy that was not in effect",
                        expected=expected,
                        actual=actual,
                    )
                )
        mirror = _policy_change_of(event, "installation_policy")
        if mirror is not None:
            while queue and not queue[0].committed and (queue[0].old, queue[0].new) != mirror:
                queue.pop(0)  # an interrupted apply that did not reach this graph
            if not queue or (queue[0].old, queue[0].new) != mirror:
                findings.append(
                    _finding(
                        codes.POLICY_MISMATCH,
                        target,
                        "a mirrored installation policy change the installation stream lacks",
                        actual=list(mirror),
                    )
                )
                return findings  # the history is cut: judging further would cascade
            change = queue.pop(0)
            if change.committed:
                installation, loose = change.new, None
        graph_change = _policy_change_of(event, "policy")
        if graph_change is not None and event.get("seq") in anchored:
            layer = graph_change[1]
    for change in queue:
        if change.committed:
            findings.append(
                _finding(
                    codes.POLICY_MISMATCH,
                    {"stream": INSTALLATION_STREAM, "seq": change.seq},
                    f"graph stream {graph_id} lacks the mirror of this installation policy change",
                )
            )
    if unchecked:
        findings.append(
            _finding(
                codes.POLICY_HASH_UNCHECKED,
                {"stream": stream},
                f"{unchecked} decisions before the first mirrored installation policy change "
                "match a policy of that time",
            )
        )
    return findings


def _policy_change_of(event: Mapping[str, JsonValue], key: str) -> tuple[str | None, str] | None:
    if event.get("actions") != [POLICY_ACTION] or not _ok(event):
        return None
    change = event.get("change")
    policy = change.get(key) if isinstance(change, Mapping) else None
    if not isinstance(policy, Mapping):
        return None
    old, new = policy.get("from"), policy.get("to")
    if not isinstance(new, str) or not (old is None or isinstance(old, str)):
        return None
    return old, new


def _entrypoint(event: Mapping[str, JsonValue]) -> JsonValue:
    actor = event.get("actor")
    client = actor.get("client") if isinstance(actor, Mapping) else None
    return client.get("entrypoint") if isinstance(client, Mapping) else None


def _ok(event: Mapping[str, JsonValue]) -> bool:
    result = event.get("result")
    return isinstance(result, Mapping) and result.get("status") == "ok"


def _effects(
    events: Iterable[Mapping[str, JsonValue]], action: str, key: str
) -> list[tuple[Mapping[str, JsonValue], Mapping[str, JsonValue]]]:
    """Intents carrying ``action`` with a well-formed ``change.<key>``; malformed ones are
    ``event_malformed`` already and not judged again."""
    found = []
    for event in events:
        actions, change = event.get("actions"), event.get("change")
        if not (isinstance(actions, list) and action in actions and isinstance(change, Mapping)):
            continue
        effect = change.get(key)
        if isinstance(effect, Mapping):
            found.append((event, effect))
    return found


def _pin_walk(
    stream: AuditStream, events: Sequence[Mapping[str, JsonValue]], effects: MaintEffects
) -> list[Finding]:
    assert effects.manifest_pin is not None
    genesis: JsonValue = None
    for event in events:
        if event.get("seq") == 1 and event.get("actions") == ["maint.graph_create"]:
            graph = event.get("graph")
            genesis = graph.get("ontology") if isinstance(graph, Mapping) else None
    return _walk(
        stream,
        _effects(events, PIN_ACTION, "pin"),
        start=genesis,
        strict_start=False,  # an unknown genesis pin accepts the first observed one
        final=effects.manifest_pin.to_json(),
        final_target={"stream": stream, "path": "manifest.toml"},
        anchored=effects.anchored,
        code=codes.PIN_MISMATCH,
        interrupted="pin intent without its manifest switch (an interrupted pin)",
        changed="the manifest pin changed outside the recorded pin events",
    )


def _policy_walk(
    stream: AuditStream,
    events: Sequence[Mapping[str, JsonValue]],
    installed: InstalledPolicy,
    anchored: frozenset[int],
) -> list[Finding]:
    """From "no policy" (``None``) through the policy intents of ``stream`` to the file."""
    graph_id = graph_of_stream(stream)
    path = "policy/installation.json" if graph_id is None else f"graphs/{graph_id}/policy.json"
    return _walk(
        stream,
        _effects(events, POLICY_ACTION, "policy"),
        start=None,
        strict_start=True,
        final=installed.observed,
        final_target={"stream": stream, "path": path},
        anchored=anchored,
        code=codes.POLICY_MISMATCH,
        interrupted="policy intent without its installed policy (an interrupted apply)",
        changed="the installed policy changed outside the recorded policy events",
    )


def _walk(
    stream: AuditStream,
    intents: Sequence[tuple[Mapping[str, JsonValue], Mapping[str, JsonValue]]],
    *,
    start: JsonValue,
    strict_start: bool,
    final: JsonValue,
    final_target: Mapping[str, JsonValue],
    anchored: frozenset[int],
    code: str,
    interrupted: str,
    changed: str,
) -> list[Finding]:
    """Walk ``{from, to}`` intents from ``start`` to the ``final`` state on disk.

    An intent is committed if the next observed state (the next intent's
    ``from``, at the end ``final``) equals its ``to``; it is an interrupted op
    if that state is still its ``from`` and no anchor proves the effect;
    anything else means the state changed outside maintenance (``code``),
    after which nothing more is judged: no cascades.
    """
    state = start
    orphans: list[Finding] = []
    pending: tuple[Mapping[str, JsonValue], JsonValue, JsonValue] | None = None

    def mismatch(
        target: Mapping[str, JsonValue], expected: JsonValue, actual: JsonValue
    ) -> list[Finding]:
        return [_finding(code, target, changed, expected=expected, actual=actual)]

    def settle(observed: JsonValue) -> bool:
        """Judge ``pending`` by the state observed after it; False on a mismatch."""
        nonlocal state
        if pending is None:
            return (state is None and not strict_start) or observed == state
        event, old, new = pending
        if observed == new:
            state = new
            return True
        if observed == old and event.get("seq") not in anchored:
            orphans.append(
                _finding(
                    codes.INTENT_WITHOUT_EFFECT,
                    {"stream": stream, "seq": event.get("seq")},
                    interrupted,
                )
            )
            state = old
            return True
        return False

    for event, change in intents:
        old, new = change.get("from"), change.get("to")
        if not settle(old):
            expected = pending[2] if pending is not None else state
            return mismatch({"stream": stream, "seq": event.get("seq")}, expected, old)
        pending = (event, old, new)
    if not settle(final):
        expected = pending[2] if pending is not None else state
        return mismatch(final_target, expected, final)
    return orphans


def effect_anchors(
    installation_events: Iterable[Mapping[str, JsonValue]],
) -> dict[str, frozenset[int]]:
    """Per graph id, the graph-stream seqs anchored by companion events (W.e).

    Only a companion, an installation event carrying a maintenance action
    with effect, proves its intent committed: it is appended after the
    rename. Anchors of ``audit verify`` and ``audit anchor`` record stream
    heads and prove nothing about the anchored event's effect.
    """
    found: dict[str, set[int]] = {}
    for event in installation_events:
        anchors, actions = event.get("anchors"), event.get("actions")
        if not isinstance(anchors, list) or not isinstance(actions, list):
            continue
        if not any(a in MAINT_EFFECT_ACTIONS for a in actions):
            continue
        for anchor in anchors:
            seq = anchor.get("seq") if isinstance(anchor, Mapping) else None
            if isinstance(anchor, Mapping) and isinstance(seq, int):
                found.setdefault(str(anchor.get("graph_id")), set()).add(seq)
    return {graph_id: frozenset(seqs) for graph_id, seqs in found.items()}


def check_anchors(
    installation_events: Iterable[Mapping[str, JsonValue]],
    graph_events: Mapping[str, Sequence[Mapping[str, JsonValue]]],
) -> list[Finding]:
    """Every anchor names an event that is still in its graph stream with that hash."""
    hashes = {
        graph_id: {event.get("seq"): event.get("hash") for event in events}
        for graph_id, events in graph_events.items()
    }
    findings = []
    for event in installation_events:
        anchors = event.get("anchors")
        if not isinstance(anchors, list):
            continue
        target: dict[str, JsonValue] = {"stream": INSTALLATION_STREAM, "seq": event.get("seq")}
        for anchor in anchors:
            if not isinstance(anchor, Mapping):
                continue  # a shape problem, reported as event_malformed
            graph_id, seq = anchor.get("graph_id"), anchor.get("seq")
            stream_hashes = hashes.get(str(graph_id))
            if stream_hashes is None and event.get("actions") == ["maint.graph_create"]:
                # W.f appends this event before publishing the graph: a crash in
                # between leaves exactly this (the staging dir is ignored).
                findings.append(
                    _finding(
                        codes.INTENT_WITHOUT_EFFECT,
                        target,
                        f"graph create intent without the graph {graph_id} (an interrupted create)",
                    )
                )
                continue
            if stream_hashes is None:
                message = f"anchored graph stream {graph_id} is missing"
                actual: JsonValue = None
            elif seq not in stream_hashes:
                message = f"graph stream {graph_id} ends before the anchored seq {seq}"
                actual = None
            elif stream_hashes[seq] != anchor.get("hash"):
                message = f"graph stream {graph_id} event {seq} differs from its anchor"
                actual = stream_hashes[seq]
            else:
                continue
            findings.append(
                _finding(
                    codes.ANCHOR_MISMATCH,
                    target,
                    message,
                    expected=dict(anchor),
                    actual=actual,
                )
            )
    return findings


def verify_graph(
    lines: Iterable[AuditLine],
    *,
    graph_id: GraphId,
    txs: Iterable[TxRecord] | None,
    effects: MaintEffects | None = None,
    ext_actions: Iterable[str] = (),
) -> StreamReport:
    """One graph stream plus the cross-checks: ``txs=None`` skips the tx check (damaged
    tx log), ``effects=None`` the maintenance check."""
    report = verify_stream(lines, stream=graph_stream(str(graph_id)), ext_actions=ext_actions)
    extra = []
    if txs is not None:
        extra += cross_check(graph_id, report.events, txs)
    if effects is not None:
        extra += check_maintenance(graph_id, report.events, effects)
    return dataclasses.replace(report, findings=(*report.findings, *extra))


def verify_installation(
    lines: Iterable[AuditLine],
    *,
    graphs: Mapping[str, StreamReport],
    ext_actions: Iterable[str] = (),
) -> StreamReport:
    """The installation stream plus its anchors into the given graph reports."""
    report = verify_stream(lines, stream=INSTALLATION_STREAM, ext_actions=ext_actions)
    extra = check_anchors(report.events, {g: r.events for g, r in graphs.items()})
    return dataclasses.replace(report, findings=(*report.findings, *extra))


def _finding(
    code: str,
    target: Mapping[str, JsonValue],
    message: str,
    *,
    expected: JsonValue | None = None,
    actual: JsonValue | None = None,
) -> Finding:
    return Finding(_SEVERITY[code], code, target, message, expected, actual)


_SEVERITY = {spec.code: spec.default_severity for spec in codes.FINDING_CODES}


def _parse(data: bytes) -> Mapping[str, JsonValue] | None:
    if not data.endswith(b"\n"):
        return None
    try:
        event = parse_json(data[:-1])
    except CanonicalError:
        return None
    return event if isinstance(event, Mapping) else None
