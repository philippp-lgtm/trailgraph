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

``cross_check`` matches write and migration intents with the tx log (ids, changeset hash
and, when recorded, changeset size; B5.6, B5.12), ``check_migrations`` judges the
migration txs (kept identity, versions, digest, the pin that follows),
``check_maintenance`` judges maintenance intents by their effects and walks
the pin, graph policy, skill and function catalog and blob store events against the
manifest, the installed policy and the published slots, ``check_policy`` walks
the installation policy events (B5.15),
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
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from graph_cli.audit import finding_codes as codes
from graph_cli.audit.events import changeset_size, revisions_hash
from graph_cli.audit.schema import (
    AUDIT_SCHEMA,
    CONFIGURE_ACTION,
    MAINT_EFFECT_ACTIONS,
    PIN_ACTION,
    POLICY_ACTION,
    RELEASE_ACTION,
    event_problems,
    graph_of_stream,
    is_blob_store_change,
    is_migration_intent,
    is_tx_intent,
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
from graph_cli.kernel.ids import EntityId, GraphId
from graph_cli.kernel.migration_marker import MigrationMarker
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
class InstalledSlots:
    """The tree hashes of the published skill, function, action and view catalogs as found:
    ``None`` when the slot is empty, ``"unreadable"`` when it is damaged (K6).
    ``functions_compiled``, ``actions_compiled`` and ``views_compiled`` are the hash of the
    compiled catalog the slot names (``None`` when there is no slot)."""

    skill: str | None
    functions: str | None
    functions_compiled: str | None = None
    actions: str | None = None
    actions_compiled: str | None = None
    views: str | None = None
    views_compiled: str | None = None

    def compiled_of(self, key: str) -> tuple[str | None, str | None]:
        """The tree hash and the compiled hash of the catalog slot ``key``."""
        if key == "functions":
            return self.functions, self.functions_compiled
        if key == "views":
            return self.views, self.views_compiled
        return self.actions, self.actions_compiled


@dataclass(frozen=True, slots=True)
class MaintEffects:
    """The maintenance effects found on disk, for judging maintenance intents (B5.6).

    ``manifest_pin`` is ``None`` when it cannot be trusted (storage reported
    ``integrity.storage.pin_mismatch``); the pin walk is then skipped.
    ``anchored`` are the graph-stream seqs named by companion anchors
    (``effect_anchors``): a companion is appended only after its effect, so
    an anchored intent is committed (W.e). ``policy`` is the graph's installed
    policy; ``None`` skips the policy walk. ``slots`` are the published skill
    and function catalog; ``None`` skips the slot walks. ``migration_marker`` is the
    graph's migration marker (``None`` without one): a migration tx it names is in the
    incomplete state, a warning instead of ``migration_unpinned`` (plan M3, M5).
    ``blob_store`` is where the manifest says the graph keeps its stored files
    (``blob_store_state``); ``None`` (an unreadable manifest) skips the blob store walk.
    """

    manifest_pin: OntologyPin | None
    releases: Mapping[str, str]  # installed version -> artifact hash of its compiled.json
    anchored: frozenset[int] = frozenset()
    policy: InstalledPolicy | None = None
    slots: InstalledSlots | None = None
    migration_marker: MigrationMarker | None = None
    blob_store: Mapping[str, JsonValue] | None = None


def blob_store_state(location: str, store_id: str | None) -> dict[str, JsonValue]:
    """A graph's blob location as the blob store walk compares it: ``{"location":
    "local"}``, or ``{"location": "s3", "store_id": <the object store's id>}``."""
    if location == "local":
        return {"location": location}
    return {"location": location, "store_id": store_id}


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
    """Every tx has its ``ok`` intent; every intent has its tx or is benign.

    A write tx is served by a write intent, a migration tx (its changeset has a
    ``migration`` key) by a migration intent (``maint.migrate`` with ``change.migration``);
    an intent of the other kind with the tx's event id does not serve it. Each intent
    serves one tx. A retried request may repeat an event id; the tx then takes the latest
    unused intent and the earlier one stays an orphan.
    """
    stream = graph_stream(str(graph_id))
    intents = [event for event in events if is_tx_intent(event)]
    by_id: dict[JsonValue, list[int]] = {}
    for index, event in enumerate(intents):
        by_id.setdefault(event.get("event_id"), []).append(index)
    used: set[int] = set()
    findings = []
    for tx in txs:
        target: dict[str, JsonValue] = {"graph_id": str(graph_id), "tx_seq": tx.seq}
        migration_tx = _is_migration_tx(tx)
        kind = "migration" if migration_tx else "write"
        free = [i for i in by_id.get(str(tx.event_id), ()) if i not in used]
        if not free:
            findings.append(
                _finding(codes.TX_WITHOUT_INTENT, target, f"no {kind} intent {tx.event_id}")
            )
            continue
        fitting = [i for i in free if is_migration_intent(intents[i]) == migration_tx]
        if not fitting:
            other = "write" if migration_tx else "migration"
            findings.append(
                _finding(
                    codes.TX_WITHOUT_INTENT,
                    target,
                    f"{other} intent {tx.event_id} differs in kind (the tx is a {kind} tx)",
                    expected=kind,
                    actual=other,
                )
            )
            continue  # the intent serves no tx: it stays an orphan below
        used.add(fitting[-1])
        event = intents[fitting[-1]]
        change = event.get("change")
        if not isinstance(change, Mapping):
            change = {}  # a malformed intent: event_malformed already, and no match here
        if migration_tx:
            record = change.get("migration")
            change = record if isinstance(record, Mapping) else {}
        expected: dict[str, JsonValue] = {
            "tx_id": str(tx.tx_id),
            "tx_seq": tx.seq,
            "changeset_hash": tx.changeset_hash,
        }
        if migration_tx or "changeset_size" in change:  # old write intents lack the size
            expected["changeset_size"] = changeset_size(tx.changeset)
        recorded = {k: change.get(k) for k in expected}
        # the source run of a request, in the intent and in the tx record (reconcile plan R-K10)
        expected["request.source"] = (
            None if tx.request.source is None else tx.request.source.to_json()
        )
        recorded["request.source"] = _intent_source(event)
        if recorded != expected:
            differing = sorted(k for k in expected if recorded[k] != expected[k])
            findings.append(
                _finding(
                    codes.TX_WITHOUT_INTENT,
                    target,
                    f"{kind} intent {tx.event_id} differs in {', '.join(differing)}",
                    expected={k: expected[k] for k in differing},
                    actual={k: recorded[k] for k in differing},
                )
            )
    for index, event in enumerate(intents):
        if index not in used:
            what = (
                "migration intent without a committed tx (an interrupted migration)"
                if is_migration_intent(event)
                else "write intent without a committed tx (an interrupted write)"
            )
            findings.append(
                _finding(
                    codes.INTENT_WITHOUT_EFFECT,
                    {"stream": stream, "seq": event.get("seq")},
                    what,
                )
            )
    return findings


def _intent_source(event: Mapping[str, JsonValue]) -> JsonValue:
    """``request.source`` of an intent, ``None`` when it names none."""
    request = event.get("request")
    return request.get("source") if isinstance(request, Mapping) else None


def _is_migration_tx(tx: TxRecord) -> bool:
    return "migration" in tx.changeset


MAX_LISTED_REVISIONS: Final = 20


@dataclass(frozen=True, slots=True)
class _Identity:
    """What a migration may not change of an entity's previous revision."""

    alias: str | None
    record_status: str
    created_at: datetime
    valid_from: str | None
    valid_to: str | None


def check_migrations(
    graph_id: GraphId,
    events: Iterable[Mapping[str, JsonValue]],
    txs: Iterable[TxRecord],
    *,
    marker: MigrationMarker | None = None,
    check_pin: bool = True,
) -> list[Finding]:
    """Migration txs against their intents, their predecessors and the pin (plan M4, M5).

    Walking the txs in order with the latest revision of every entity: a migration revision
    keeps the previous revision's alias, record status, creation time and valid time
    (``migration_changed_identity``; a revision without a previous one is the same finding).
    Every revision's ``ontology_version``, the tx's pin and the changeset's ``migration.to``
    are one version, and the intent's ``from`` and ``to`` are the changeset's
    (``migration_version_mismatch``). The intent's ``revisions_hash`` and ``counts.entities``
    are those of the tx (``migration_revisions_mismatch``). A migration is followed by an
    ``ok`` pin intent to ``to`` in the stream; without one it is ``migration_incomplete``
    (warning) when ``marker`` names the tx (the crash between commit and pin switch), else
    ``migration_unpinned``. Judging the pin needs the marker, so ``check_pin=False`` skips it.
    A tx without its intent is ``tx_without_intent`` already: only the checks that need no
    intent run for it. Individual findings list at most ``MAX_LISTED_REVISIONS`` revisions.
    """
    ok = [e for e in events if _ok(e)]
    intents: dict[JsonValue, Mapping[str, JsonValue]] = {}
    for intent in ok:
        if is_migration_intent(intent):
            intents[intent.get("event_id")] = intent  # a retry's latest intent serves the tx
    pins = _effects(ok, PIN_ACTION, "pin")
    latest: dict[EntityId, _Identity] = {}
    findings: list[Finding] = []
    for tx in txs:
        migrating = _is_migration_tx(tx)
        target: dict[str, JsonValue] = {"graph_id": str(graph_id), "tx_seq": tx.seq}
        offending: list[JsonValue] = []
        total = 0
        for revision in tx.revisions:
            identity = _Identity(
                revision.alias,
                revision.record_status,
                revision.created_at,
                revision.valid_from,
                revision.valid_to,
            )
            previous = latest.get(revision.id)
            latest[revision.id] = identity
            if not migrating:
                continue
            if previous is None:
                fields = ["previous_revision"]
            else:
                fields = [
                    name
                    for name in _IDENTITY_FIELDS
                    if getattr(previous, name) != getattr(identity, name)
                ]
            if fields:
                total += 1
                if len(offending) < MAX_LISTED_REVISIONS:
                    offending.append(
                        {"id": str(revision.id), "rev": revision.rev, "fields": fields}
                    )
        if not migrating:
            continue
        if total:
            findings.append(
                _finding(
                    codes.MIGRATION_CHANGED_IDENTITY,
                    target,
                    f"{total} migrated revisions change what a migration keeps (alias, record "
                    "status, created_at, valid time) or have no previous revision",
                    actual=offending,
                )
            )
        findings += _migration_checks(
            tx, target, intents.get(str(tx.event_id)), pins, marker=marker, check_pin=check_pin
        )
    return findings


def _pinned_after(
    pins: Sequence[tuple[Mapping[str, JsonValue], Mapping[str, JsonValue]]],
    seq: JsonValue,
    version: str,
) -> bool:
    """Whether a pin intent to ``version`` follows the stream event ``seq``."""
    if not isinstance(seq, int):
        return False
    for event, pin in pins:
        later, new = event.get("seq"), pin.get("to")
        if (
            isinstance(later, int)
            and later > seq
            and isinstance(new, Mapping)
            and new.get("version") == version
        ):
            return True
    return False


_IDENTITY_FIELDS: Final = ("alias", "record_status", "created_at", "valid_from", "valid_to")


def _migration_checks(
    tx: TxRecord,
    target: Mapping[str, JsonValue],
    event: Mapping[str, JsonValue] | None,
    pins: Sequence[tuple[Mapping[str, JsonValue], Mapping[str, JsonValue]]],
    *,
    marker: MigrationMarker | None,
    check_pin: bool,
) -> list[Finding]:
    """The version, digest and pin checks of one migration tx."""
    findings: list[Finding] = []
    record = tx.changeset.get("migration")
    record = record if isinstance(record, Mapping) else {}
    to = record.get("to")
    intent_change = event.get("change") if event is not None else None
    stated = intent_change.get("migration") if isinstance(intent_change, Mapping) else None
    stated = stated if isinstance(stated, Mapping) else None

    expected: dict[str, JsonValue] = {"to": to}
    actual: dict[str, JsonValue] = {}
    if tx.ontology.version != to:
        actual["tx.ontology.version"] = tx.ontology.version
    wrong = sorted({r.ontology_version for r in tx.revisions if r.ontology_version != to})
    if wrong:
        actual["revisions.ontology_version"] = list(wrong)
    if stated is not None:
        for key in ("from", "to"):
            if stated.get(key) != record.get(key):
                actual[f"intent.{key}"] = stated.get(key)
                expected[f"intent.{key}"] = record.get(key)
    if not isinstance(to, str) or actual:
        findings.append(
            _finding(
                codes.MIGRATION_VERSION_MISMATCH,
                target,
                "the migration's target version differs between its changeset, tx pin, "
                "revisions and intent",
                expected=expected,
                actual=actual or {"changeset.migration.to": to},
            )
        )
    if stated is not None:
        counts = stated.get("counts")
        entities = counts.get("entities") if isinstance(counts, Mapping) else None
        differs: dict[str, JsonValue] = {}
        if stated.get("revisions_hash") != revisions_hash(tx.revisions):
            differs["revisions_hash"] = stated.get("revisions_hash")
        if entities != len(tx.revisions):
            differs["counts.entities"] = entities
        if differs:
            findings.append(
                _finding(
                    codes.MIGRATION_REVISIONS_MISMATCH,
                    target,
                    "the migration intent's revisions digest or entity count differs from its tx",
                    expected={
                        "revisions_hash": revisions_hash(tx.revisions),
                        "counts.entities": len(tx.revisions),
                    },
                    actual=differs,
                )
            )
    if check_pin and event is not None and isinstance(to, str):
        pinned = _pinned_after(pins, event.get("seq"), to)
        if not pinned:
            if marker is not None and marker.tx_id == tx.tx_id:
                findings.append(
                    _finding(
                        codes.MIGRATION_INCOMPLETE,
                        target,
                        f"the migration to {to} is committed but not pinned (an interrupted "
                        "release: run the release again to complete it)",
                    )
                )
            else:
                findings.append(
                    _finding(
                        codes.MIGRATION_UNPINNED,
                        target,
                        f"no pin intent to {to} follows the migration and no migration marker "
                        "names it",
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
    if effects.blob_store is not None:
        findings += _blob_store_walk(stream, ok, effects.blob_store, effects.anchored)
    if effects.slots is not None:
        findings += _slot_walk(stream, ok, "skill", effects.slots.skill, effects.anchored)
        for key in ("functions", "actions", "views"):
            tree, _ = effects.slots.compiled_of(key)
            walked = _slot_walk(stream, ok, key, tree, effects.anchored)
            if not any(f.code == codes.SLOT_MISMATCH for f in walked):
                walked += _compiled_check(stream, ok, key, effects.slots, effects.anchored)
            findings += walked
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
TOKEN_UPDATE: Final = "maint.token_update"


def _roles_of(entry: Mapping[str, JsonValue]) -> list[JsonValue]:
    """The ``roles`` of a file entry or an event's token or code; a missing key is none."""
    roles = entry.get("roles")
    return list(roles) if isinstance(roles, list) else []


def check_tokens(
    events: Iterable[Mapping[str, JsonValue]],
    installed: Mapping[str, Mapping[str, JsonValue]] | None,
) -> list[Finding]:
    """The token file against the installation stream's token events (B14.11, B5.12).

    Replaying ``maint.token_create``, ``maint.token_update`` (which assigns the token's
    roles) and ``maint.token_revoke`` in seq order gives the live tokens. ``installed``
    maps each name in the file to ``{hash_prefix, project, expires, scope, roles}``;
    ``None`` when the file cannot be read. An event without a scope created an app token,
    one without roles a token without roles. A file token that is not live,
    or differs, was added by hand or survived an interrupted revoke
    (``token_mismatch``); so does one whose roles were edited by hand. A live token
    missing from the file is an interrupted create.
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
        elif event.get("actions") == [TOKEN_UPDATE] and name in live:
            seq, created = live[name]
            live[name] = (seq, {**created, "roles": _roles_of(token)})
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
        actual["roles"] = _roles_of(entry)
        found = live.get(name)
        expected = None
        if found is not None:
            expected = {key: found[1].get(key) for key in actual}
            expected["scope"] = found[1].get("scope") or "app"
            expected["roles"] = _roles_of(found[1])
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


ENROLLMENTS_FILE: Final = "tokens/enrollments.json"


def check_enrollments(
    events: Iterable[Mapping[str, JsonValue]],
    installed: Mapping[str, Mapping[str, JsonValue]] | None,
) -> list[Finding]:
    """The enrollment file against the installation stream's enrollment events (B14.21).

    Replaying in seq order: ``maint.token_create`` with ``change.enrollment`` alone lists a
    code; ``maint.token_revoke`` with it, or an exchange (``maint.token_create`` with
    ``change.token`` too), removes it. ``installed`` maps each id in the file to
    ``{hash_prefix, project, scope, expires_at, roles}``; ``None`` when the file cannot be
    read. A listed code that is not live, or differs (its roles included), was added by
    hand or survived an interrupted exchange or revoke (``enrollment_mismatch``); so is an
    exchange of a code that was not live, of another project, scope or set of roles, or
    after the code expired. A live code missing from the file is an interrupted create.
    """
    live: dict[str, tuple[JsonValue, Mapping[str, JsonValue]]] = {}
    findings = []
    for event in events:
        change = event.get("change")
        entry = change.get("enrollment") if _ok(event) and isinstance(change, Mapping) else None
        if not isinstance(entry, Mapping) or not isinstance(entry.get("id"), str):
            continue
        assert isinstance(change, Mapping)
        enrollment_id = str(entry["id"])
        if event.get("actions") == [TOKEN_CREATE] and "token" not in change:
            live[enrollment_id] = (event.get("seq"), entry)
        elif event.get("actions") == [TOKEN_REVOKE]:
            live.pop(enrollment_id, None)
        elif event.get("actions") == [TOKEN_CREATE]:
            found = live.pop(enrollment_id, None)
            problem = _exchange_problem(event, change, found[1] if found else None)
            if problem is not None:
                findings.append(
                    _finding(
                        codes.ENROLLMENT_MISMATCH,
                        {"stream": INSTALLATION_STREAM, "seq": event.get("seq")},
                        f"the exchange of enrollment {enrollment_id} {problem}",
                    )
                )
    where: dict[str, JsonValue] = {"stream": INSTALLATION_STREAM, "path": ENROLLMENTS_FILE}
    if installed is None:
        return [
            *findings,
            _finding(
                codes.ENROLLMENT_MISMATCH,
                where,
                "the enrollment file cannot be read as an enrollment document",
                actual="unreadable",
            ),
        ]
    keys = ("hash_prefix", "project", "scope", "expires_at")
    for enrollment_id, item in sorted(installed.items()):
        actual = {key: item.get(key) for key in keys}
        actual["roles"] = _roles_of(item)
        found = live.get(enrollment_id)
        expected = None
        if found is not None:
            expected = {key: found[1].get(key) for key in keys}
            expected["roles"] = _roles_of(found[1])
        if expected != actual:
            findings.append(
                _finding(
                    codes.ENROLLMENT_MISMATCH,
                    {**where, "id": enrollment_id},
                    f"enrollment {enrollment_id} in the enrollment file differs from its events"
                    " (added by hand, or an exchange or revoke was interrupted: revoke it"
                    " again)",
                    expected=expected,
                    actual=actual,
                )
            )
    for enrollment_id, (seq, _) in sorted(live.items()):
        if enrollment_id not in installed:
            findings.append(
                _finding(
                    codes.INTENT_WITHOUT_EFFECT,
                    {"stream": INSTALLATION_STREAM, "seq": seq},
                    f"enrollment {enrollment_id} was created without reaching the enrollment"
                    " file (an interrupted create)",
                )
            )
    return findings


def _exchange_problem(
    event: Mapping[str, JsonValue],
    change: Mapping[str, JsonValue],
    code: Mapping[str, JsonValue] | None,
) -> str | None:
    """Why an exchange event does not follow from its code: the code was not live, or the
    token it created has another project, scope or set of roles, or it came after the code
    expired."""
    if code is None:
        return "used a code that was not live (never created, used or revoked before)"
    token = change.get("token")
    if not isinstance(token, Mapping):
        return "names no token"
    if token.get("project") != code.get("project") or (token.get("scope") or "app") != (
        code.get("scope") or "app"
    ):
        return "created a token of another project or scope than the code's"
    if _roles_of(token) != _roles_of(code):
        return "created a token with other roles than the code's"
    ts, expires = event.get("ts"), code.get("expires_at")
    if isinstance(ts, str) and isinstance(expires, str) and ts >= expires:
        return "came after the code expired"
    return None


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


def _slot_walk(
    stream: AuditStream,
    events: Sequence[Mapping[str, JsonValue]],
    key: str,
    installed: str | None,
    anchored: frozenset[int],
) -> list[Finding]:
    """From "no slot" (``None``) through the ``change.<key>`` intents to the published hash:
    the slot must be what the last committed publish recorded."""
    graph_id = graph_of_stream(stream)
    return _walk(
        stream,
        _effects(events, CONFIGURE_ACTION, key),
        start=None,
        strict_start=True,
        final=installed,
        final_target={"stream": stream, "path": f"graphs/{graph_id}/{key}"},
        anchored=anchored,
        code=codes.SLOT_MISMATCH,
        interrupted=f"{key} intent without its published slot (an interrupted publish)",
        changed=f"the published {key} changed outside the recorded {key} events",
    )


def _blob_store_walk(
    stream: AuditStream,
    events: Sequence[Mapping[str, JsonValue]],
    observed: Mapping[str, JsonValue],
    anchored: frozenset[int],
) -> list[Finding]:
    """From ``local`` (a graph is always created local) through the ``change.blob_store``
    intents to the manifest's blob location and store id (object storage O7). A switch
    whose manifest rename never happened leaves the graph where it was, with every file:
    an interrupted switch (``intent_without_effect``, info, like an interrupted pin)."""
    switches: list[tuple[Mapping[str, JsonValue], Mapping[str, JsonValue]]] = []
    for event, change in _effects(events, CONFIGURE_ACTION, "blob_store"):
        if not is_blob_store_change(change):
            continue  # event_malformed already
        store_id = change.get("store_id")
        store_id = store_id if isinstance(store_id, str) else None
        old, new = str(change.get("from")), str(change.get("to"))
        states = {"from": blob_store_state(old, store_id), "to": blob_store_state(new, store_id)}
        switches.append((event, states))
    graph_id = graph_of_stream(stream)
    return _walk(
        stream,
        switches,
        start=blob_store_state("local", None),
        strict_start=True,
        final=dict(observed),
        final_target={"stream": stream, "path": f"graphs/{graph_id}/manifest.toml"},
        anchored=anchored,
        code=codes.BLOB_STORE_MISMATCH,
        interrupted=lambda old, new: _interrupted_switch(old, new, graph_id),
        changed="the manifest's blob location differs from the recorded blob store switches",
    )


_WHERE: Final = {"local": "in the data root", "s3": "in the object store"}


def _interrupted_switch(old: JsonValue, new: JsonValue, graph_id: str | None) -> str:
    source = old.get("location") if isinstance(old, Mapping) else None
    target = new.get("location") if isinstance(new, Mapping) else None
    return (
        f"blob store switch intent from {source} to {target} without its manifest switch (an"
        f" interrupted switch): the graph still keeps its stored files {_WHERE.get(str(source))}"
        f" and lost none; to switch it, run graph-admin files move {graph_id or '<graph>'}"
        f" --to {target}"
    )


def _compiled_check(
    stream: AuditStream,
    events: Sequence[Mapping[str, JsonValue]],
    key: str,
    slots: InstalledSlots,
    anchored: frozenset[int],
) -> list[Finding]:
    """The compiled catalog the ``key`` slot (``functions``, ``actions`` or ``views``) names must be
    the one the last committed publish recorded in ``change.<key>.compiled_hash`` (the app
    reads the compiled file, not the sources); the caller skips it when the sources hash
    already mismatches."""
    installed, compiled = slots.compiled_of(key)
    if installed is None or installed == UNREADABLE or compiled is None:
        return []
    graph_id = graph_of_stream(stream)
    accepted: list[JsonValue] = []
    for event, change in reversed(_effects(events, CONFIGURE_ACTION, key)):
        if change.get("to") != installed:
            continue
        accepted.append(change.get("compiled_hash"))
        # An interrupted republish of the same sources leaves the previous compiled catalog.
        if not (change.get("from") == installed and event.get("seq") not in anchored):
            break
    if compiled in accepted or not accepted:
        return []
    return [
        _finding(
            codes.SLOT_MISMATCH,
            {"stream": stream, "path": f"graphs/{graph_id}/{key}"},
            f"the compiled catalog of the published {key} differs from the recorded "
            "compiled_hash (the sources hash matches)",
            expected=accepted[0],
            actual=compiled,
        )
    ]


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
    interrupted: str | Callable[[JsonValue, JsonValue], str],
    changed: str,
) -> list[Finding]:
    """Walk ``{from, to}`` intents from ``start`` to the ``final`` state on disk.

    An intent is committed if the next observed state (the next intent's
    ``from``, at the end ``final``) equals its ``to``; it is an interrupted op
    if that state is still its ``from`` and no anchor proves the effect
    (``interrupted`` is its message, or makes it from ``from`` and ``to``);
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
            message = interrupted if isinstance(interrupted, str) else interrupted(old, new)
            orphans.append(
                _finding(
                    codes.INTENT_WITHOUT_EFFECT,
                    {"stream": stream, "seq": event.get("seq")},
                    message,
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
        records = list(txs)
        extra += cross_check(graph_id, report.events, records)
        extra += check_migrations(
            graph_id,
            report.events,
            records,
            marker=effects.migration_marker if effects is not None else None,
            check_pin=effects is not None,
        )
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
