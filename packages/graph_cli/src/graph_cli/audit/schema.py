"""Shape of ``graph_cli.audit/1`` events (contracts B5.2, B5.4-B5.6, B5.11, V.4, V.5).

``event_problems`` lists every way an event misses its shape: builders refuse
their own output on any problem, the verifier reports ``event_malformed``.
Unknown keys are allowed (V.4: preserved and ignored). Actions outside the
taxonomy are not a shape problem; ``unknown_actions`` lists them separately
(warning ``unknown_action``, V.5).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Final

from graph_cli.kernel.actions import MAINT_ACTIONS, Action, is_known_action
from graph_cli.kernel.actor import ActorContext, Assurance
from graph_cli.kernel.canonical import HASH_RE, JsonValue
from graph_cli.kernel.clock import format_timestamp, parse_timestamp
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import format_id, parse_id
from graph_cli.kernel.json_access import JsonShapeError

AUDIT_SCHEMA: Final = "graph_cli.audit/1"
INSTALLATION: Final = "installation"
RESULT_STATUSES: Final = ("ok", "denied", "invalid", "conflict", "error")
MAX_RESULT_FINDINGS: Final = 20
# Maintenance actions that change state; an ``ok`` event carrying one is an intent (B5.6).
MAINT_EFFECT_ACTIONS: Final = MAINT_ACTIONS - {
    Action("maint.audit_read"),
    Action("maint.audit_verify"),
    Action("maint.audit_anchor"),
}

RELEASE_ACTION: Final = "maint.ontology_release"
PIN_ACTION: Final = "maint.ontology_pin"
CONFIGURE_ACTION: Final = "maint.graph_configure"
REPAIR_ACTION: Final = "maint.repair"
REINDEX_ACTION: Final = "maint.reindex"
POLICY_ACTION: Final = "maint.policy_change"
TOKEN_ACTIONS: Final = ("maint.token_create", "maint.token_revoke")
_GRAPH_STREAM_RE: Final = re.compile(r"^graph:(.+)$")
_EXT_STATE_RE: Final = re.compile(r"^ext\.[a-z][a-z0-9_]{1,31}\.state$")
_TOKEN_PREFIX_RE: Final = re.compile(r"^sha256:[0-9a-f]{16}$")
_REQUIRED: Final = ("schema", "stream", "event_id", "ts", "actor", "actions", "authz")
_REQUIRED_TAIL: Final = ("result", "request")
_CHAIN: Final = ("seq", "prev_hash", "hash")


def graph_of_stream(stream: str) -> str | None:
    """The graph id of a ``graph:<id>`` stream, else ``None``."""
    match = _GRAPH_STREAM_RE.match(stream)
    return match.group(1) if match else None


def is_canonical_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return format_id(parse_id(value)) == value
    except (GraphCliError, ValueError):
        return False


def _status(event: Mapping[str, JsonValue]) -> object:
    result = event.get("result")
    return result.get("status") if isinstance(result, Mapping) else None


def _actions(event: Mapping[str, JsonValue]) -> list[str]:
    actions = event.get("actions")
    if not isinstance(actions, list):
        return []
    return [a for a in actions if isinstance(a, str)]


def is_ext_state_action(action: str) -> bool:
    """The implicit ``ext.<ns>.state`` action of every extension (B6.1)."""
    return _EXT_STATE_RE.match(action) is not None


def is_write_intent(event: Mapping[str, JsonValue]) -> bool:
    """An ``ok`` event with a ``write.*`` action: committed iff its tx exists (B5.6)."""
    return _status(event) == "ok" and any(a.startswith("write.") for a in _actions(event))


def is_intent(event: Mapping[str, JsonValue]) -> bool:
    """B5.6: ``ok`` with ``write.*``, ``ext.<ns>.state`` or a maintenance action with effect."""
    return _status(event) == "ok" and any(
        a.startswith("write.") or is_ext_state_action(a) or a in MAINT_EFFECT_ACTIONS
        for a in _actions(event)
    )


def unknown_actions(event: Mapping[str, JsonValue], ext_actions: Iterable[str] = ()) -> list[str]:
    """Actions outside the taxonomy known to this version (``ext.<ns>.state`` is implicit)."""
    known_ext = frozenset(ext_actions)
    return [
        a
        for a in _actions(event)
        if not is_known_action(a, known_ext) and not is_ext_state_action(a)
    ]


def event_problems(event: Mapping[str, JsonValue], *, sealed: bool = True) -> list[str]:
    """Every shape violation of a /1 event; ``sealed=False`` checks a draft body."""
    problems: list[str] = []
    for key in (*_REQUIRED, *_REQUIRED_TAIL, *(_CHAIN if sealed else ())):
        if key not in event:
            problems.append(f"missing key {key!r}")
    if problems:
        return problems
    if event["schema"] != AUDIT_SCHEMA:
        problems.append(f"schema is not {AUDIT_SCHEMA}")
    stream = event["stream"]
    graph_id = graph_of_stream(stream) if isinstance(stream, str) else None
    if stream != INSTALLATION and not is_canonical_uuid(graph_id):
        problems.append("stream is neither 'installation' nor 'graph:<id>'")
    if sealed:
        seq = event["seq"]
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            problems.append("seq is not a positive integer")
        for key in ("prev_hash", "hash"):
            if not _is_hash(event[key]):
                problems.append(f"{key} is not a sha256 hash")
    if not is_canonical_uuid(event["event_id"]):
        problems.append("event_id is not a canonical UUID")
    problems.extend(_ts_problems(event["ts"]))
    problems.extend(_actor_problems(event["actor"]))
    actions = event["actions"]
    if not isinstance(actions, list) or not actions:
        problems.append("actions is not a non-empty list")
        actions = []
    elif len(_actions(event)) != len(actions):
        problems.append("actions holds a non-string")
    elif _actions(event) != sorted(set(_actions(event))):
        problems.append("actions are not sorted and unique")
    status = _status(event)
    problems.extend(_result_problems(event["result"]))
    problems.extend(_authz_problems(event["authz"], _actions(event), status, event["result"]))
    problems.extend(_request_problems(event["request"]))
    problems.extend(_routing_problems(event, stream, graph_id, status))
    problems.extend(_optional_problems(event))
    return problems


def _is_hash(value: object) -> bool:
    return isinstance(value, str) and HASH_RE.match(value) is not None


def _ts_problems(ts: JsonValue) -> list[str]:
    if isinstance(ts, str):
        try:
            if format_timestamp(parse_timestamp(ts)) == ts:
                return []
        except (GraphCliError, ValueError):
            pass
    return ["ts is not a UTC millisecond timestamp"]


def _actor_problems(actor: JsonValue) -> list[str]:
    if not isinstance(actor, Mapping):
        return ["actor is not an object"]
    try:
        context = ActorContext.from_json(actor)
    except (GraphCliError, JsonShapeError, ValueError) as exc:
        return [f"actor: {exc}"]
    if context.principal.assurance is not Assurance.VERIFIED:
        return ["actor.principal is not verified"]
    return []


def _result_problems(result: JsonValue) -> list[str]:
    if not isinstance(result, Mapping):
        return ["result is not an object"]
    status = result.get("status")
    if status not in RESULT_STATUSES:
        return [f"result.status {status!r} is not one of {', '.join(RESULT_STATUSES)}"]
    problems = []
    code = result.get("code")
    if (status == "ok") != (code is None):
        problems.append("result.code must be present iff status is not ok")
    elif code is not None and not isinstance(code, str):
        problems.append("result.code is not a string")
    findings = result.get("findings")
    if findings is not None:
        if status != "invalid":
            problems.append("result.findings only belong to invalid events")
        elif not isinstance(findings, list) or len(findings) > MAX_RESULT_FINDINGS:
            problems.append(f"result.findings is not a list of at most {MAX_RESULT_FINDINGS}")
        elif not all(
            isinstance(f, Mapping)
            and set(f) == {"code", "target"}
            and isinstance(f["code"], str)
            and isinstance(f["target"], Mapping)
            for f in findings
        ):
            problems.append("result.findings entries are not {code, target}")
    return problems


def _authz_problems(
    authz: JsonValue, actions: Sequence[str], status: object, result: JsonValue
) -> list[str]:
    if not isinstance(authz, Mapping):
        return ["authz is not an object"]
    problems = []
    if not _is_hash(authz.get("policy_hash")):
        problems.append("authz.policy_hash is not a sha256 hash")
    decisions = authz.get("decisions")
    if not isinstance(decisions, list) or not all(_is_decision(d) for d in decisions):
        return [*problems, "authz.decisions are not {action, resource, effect, rule[, code]}"]
    entries = [d for d in decisions if isinstance(d, Mapping)]
    covered = {d["action"] for d in entries}
    missing = sorted(a for a in set(actions) if a not in covered)
    if missing:
        problems.append(f"authz.decisions do not cover {', '.join(missing)}")
    denials = [d for d in entries if d["effect"] == "deny"]
    if status == "denied":
        code = result.get("code") if isinstance(result, Mapping) else None
        if not any(d["code"] == code for d in denials):
            problems.append("a denied event needs a deny decision with its result.code")
    elif denials:
        problems.append("only denied events carry deny decisions")
    return problems


def _is_decision(decision: JsonValue) -> bool:
    if not isinstance(decision, Mapping):
        return False
    effect = decision.get("effect")
    keys = {"action", "resource", "effect", "rule"} | ({"code"} if effect == "deny" else set())
    return (
        set(decision) == keys
        and effect in ("allow", "deny")
        and all(isinstance(decision[k], str) for k in keys)
    )


def _request_problems(request: JsonValue) -> list[str]:
    if (
        isinstance(request, Mapping)
        and set(request) == {"command", "args_digest"}
        and isinstance(request["command"], str)
        and _is_hash(request["args_digest"])
    ):
        return []
    return ["request is not {command, args_digest}"]


def _routing_problems(
    event: Mapping[str, JsonValue], stream: JsonValue, graph_id: str | None, status: object
) -> list[str]:
    """B5.2 routing, B5.4 conditional keys that depend on it."""
    problems = []
    graph = event.get("graph")
    if graph is not None and not _is_graph(graph):
        problems.append("graph is not {id, name, ontology{bundle, version, artifact_hash}}")
    if graph_id is not None:
        if graph is None:
            problems.append("a graph-stream event needs graph")
        elif isinstance(graph, Mapping) and graph.get("id") != graph_id:
            problems.append("graph.id differs from the stream's graph")
        if status == "denied":
            problems.append("denials belong to the installation stream")
    elif is_write_intent(event) or (status in ("invalid", "conflict") and _writes(event)):
        problems.append("write events belong to the graph stream")
    change = event.get("change")
    if is_write_intent(event):
        if change is None:
            problems.append("a write intent needs change")
        else:
            problems.extend(_change_problems(change))
    elif change is not None:
        if status != "ok":
            problems.append("only ok events carry change")
        elif not isinstance(change, Mapping):
            problems.append("change is not an object")
    if graph_id is not None and status == "ok":
        problems.extend(_maint_change_problems(_actions(event), change))
    elif status == "ok" and isinstance(change, Mapping) and POLICY_ACTION in _actions(event):
        problems.extend(_maint_change_problems([POLICY_ACTION], change))  # installation policy
    if status == "ok" and any(a in TOKEN_ACTIONS for a in _actions(event)):
        problems.extend(_token_change_problems(_actions(event), change))
    anchors = event.get("anchors")
    if anchors is not None:
        if stream != INSTALLATION:
            problems.append("anchors belong to the installation stream")
        elif not isinstance(anchors, list) or not all(_is_anchor(a) for a in anchors):
            problems.append("anchors are not [{graph_id, seq, hash}]")
    return problems


def _writes(event: Mapping[str, JsonValue]) -> bool:
    return any(a.startswith("write.") for a in _actions(event))


def _is_graph(graph: JsonValue) -> bool:
    if not isinstance(graph, Mapping) or set(graph) != {"id", "name", "ontology"}:
        return False
    ontology = graph["ontology"]
    return (
        is_canonical_uuid(graph["id"])
        and isinstance(graph["name"], str)
        and isinstance(ontology, Mapping)
        and set(ontology) == {"bundle", "version", "artifact_hash"}
        and all(isinstance(v, str) for v in ontology.values())
    )


def _is_anchor(anchor: JsonValue) -> bool:
    if not isinstance(anchor, Mapping) or set(anchor) != {"graph_id", "seq", "hash"}:
        return False
    seq = anchor["seq"]
    return (
        is_canonical_uuid(anchor["graph_id"])
        and isinstance(seq, int)
        and not isinstance(seq, bool)
        and seq >= 1
        and _is_hash(anchor["hash"])
    )


def _change_problems(change: JsonValue) -> list[str]:
    shape = (
        "change is not {tx_id, tx_seq, changeset_hash, changeset_size?,"
        " entities[{id, rev, after, before?}], blobs?[{hash, size}]}"
    )
    keys = {"tx_id", "tx_seq", "changeset_hash", "entities"}
    if not isinstance(change, Mapping) or set(change) - {"blobs", "changeset_size"} != keys:
        return [shape]
    if "blobs" in change and not is_blob_list(change["blobs"]):
        return [shape]
    if "changeset_size" in change and not _is_size(change["changeset_size"]):
        return [shape]
    tx_seq, entities = change["tx_seq"], change["entities"]
    if not (
        is_canonical_uuid(change["tx_id"])
        and isinstance(tx_seq, int)
        and not isinstance(tx_seq, bool)
        and tx_seq >= 1
        and _is_hash(change["changeset_hash"])
        and isinstance(entities, list)
        and entities
    ):
        return [shape]
    for entry in entities:
        if not isinstance(entry, Mapping):
            return [shape]
        rev = entry.get("rev")
        if not isinstance(rev, int) or isinstance(rev, bool) or rev < 1:
            return [shape]
        keys = {"id", "rev", "after"} | ({"before"} if rev > 1 else set())
        if set(entry) != keys or not is_canonical_uuid(entry["id"]):
            return [shape]
        if not all(_is_hash(entry[k]) for k in keys - {"id", "rev"}):
            return [shape]
    return []


def is_blob_list(value: JsonValue) -> bool:
    """A non-empty ``[{hash, size}]`` in hash order, each hash once (B5.8: nothing else)."""
    if not isinstance(value, list) or not value:
        return False
    hashes = []
    for item in value:
        if not (isinstance(item, Mapping) and set(item) == {"hash", "size"}):
            return False
        if not (_is_hash(item["hash"]) and _is_size(item["size"])):
            return False
        hashes.append(item["hash"])
    return hashes == sorted(set(hashes))


def _is_size(value: JsonValue) -> bool:
    """A byte count: a non-negative integer."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _token_change_problems(actions: Sequence[str], change: JsonValue) -> list[str]:
    """B14.11, B14.21: a token event names the token and a 16-hex prefix of its hash; a
    created token also its project and expiry (each possibly null), an admin token its
    scope. An enrollment event names the code by id and hash prefix instead; a created
    code also its project, scope and expiry. An exchange creates a token and names the
    enrollment it used: both."""
    created = "maint.token_create" in actions
    entries = change if isinstance(change, Mapping) else {}
    token, enrollment = entries.get("token"), entries.get("enrollment")
    problems = []
    if (token is not None or enrollment is None) and not _is_token_change(token, created):
        shown = "name, hash_prefix, project, expires[, scope]" if created else "name, hash_prefix"
        problems.append(f"a token event needs change.token{{{shown}}}")
    if enrollment is not None and not _is_enrollment_change(enrollment, created and token is None):
        shown = (
            "id, hash_prefix, project, scope, expires_at"
            if created and token is None
            else "id, hash_prefix"
        )
        problems.append(f"an enrollment event needs change.enrollment{{{shown}}}")
    if token is not None and enrollment is not None and not created:
        problems.append("only an exchange (maint.token_create) names a token and an enrollment")
    return problems


def _is_token_change(token: JsonValue, created: bool) -> bool:
    keys = {"name", "hash_prefix", *(("project", "expires") if created else ())}
    if created and isinstance(token, Mapping) and "scope" in token:
        keys.add("scope")
    return (
        isinstance(token, Mapping)
        and set(token) == keys
        and token.get("scope", "admin") == "admin"
        and isinstance(token["name"], str)
        and isinstance(token["hash_prefix"], str)
        and _TOKEN_PREFIX_RE.match(token["hash_prefix"]) is not None
        and all(token.get(k) is None or isinstance(token[k], str) for k in ("project", "expires"))
    )


def _is_enrollment_change(enrollment: JsonValue, created: bool) -> bool:
    keys = {"id", "hash_prefix", *(("project", "scope", "expires_at") if created else ())}
    return (
        isinstance(enrollment, Mapping)
        and set(enrollment) == keys
        and all(isinstance(enrollment[k], str) for k in keys)
        and _TOKEN_PREFIX_RE.match(str(enrollment["hash_prefix"])) is not None
        and enrollment.get("scope", "app") in ("app", "admin")
    )


def _maint_change_problems(actions: Sequence[str], change: JsonValue) -> list[str]:
    """Graph-stream maintenance intents name their effect (P1 item 5, plan M4)."""
    problems = []
    entries = change if isinstance(change, Mapping) else {}
    if RELEASE_ACTION in actions:
        release = entries.get("release")
        if not (
            isinstance(release, Mapping)
            and set(release) == {"version", "artifact_hash", "release_id"}
            and isinstance(release["version"], str)
            and _is_hash(release["artifact_hash"])
            and is_canonical_uuid(release["release_id"])
        ):
            problems.append(
                "a release intent needs change.release{version, artifact_hash, release_id}"
            )
    if PIN_ACTION in actions:
        pin = entries.get("pin")
        if not (
            isinstance(pin, Mapping)
            and set(pin) == {"from", "to"}
            and all(_is_pin(pin[k]) for k in ("from", "to"))
        ):
            problems.append("a pin intent needs change.pin{from, to}")
    if REPAIR_ACTION in actions and ("blobs_removed" in entries or "staging_removed" in entries):
        removed, staging = entries.get("blobs_removed"), entries.get("staging_removed")
        if not (
            (removed == [] or is_blob_list(removed))
            and isinstance(staging, int)
            and not isinstance(staging, bool)
            and staging >= 0
        ):
            problems.append(
                "a file removal needs change.blobs_removed[{hash, size}] and staging_removed"
            )
    if REINDEX_ACTION in actions and "reindex" in entries:
        reindex = entries["reindex"]
        if not (
            isinstance(reindex, Mapping) and set(reindex) == {"seq"} and _is_size(reindex["seq"])
        ):
            problems.append("a reindex intent needs change.reindex{seq}")
    for key in ("skill", "functions"):
        if CONFIGURE_ACTION in actions and key in entries:
            slot = entries[key]
            if not (
                isinstance(slot, Mapping)
                and set(slot) == {"from", "to"}
                and (slot["from"] is None or _is_hash(slot["from"]))
                and _is_hash(slot["to"])
            ):
                problems.append(
                    f"a {key} change needs change.{key}{{from: hash or null, to: hash}}"
                )
    for key in ("policy", "installation_policy"):
        if POLICY_ACTION in actions and key in entries:
            policy = entries[key]
            if not (
                isinstance(policy, Mapping)
                and set(policy) == {"from", "to"}
                and (policy["from"] is None or _is_hash(policy["from"]))
                and _is_hash(policy["to"])
            ):
                problems.append(
                    f"a policy change needs change.{key}{{from: hash or null, to: hash}}"
                )
    return problems


def _is_pin(pin: JsonValue) -> bool:
    return (
        isinstance(pin, Mapping)
        and set(pin) == {"bundle", "version", "artifact_hash"}
        and isinstance(pin["bundle"], str)
        and isinstance(pin["version"], str)
        and _is_hash(pin["artifact_hash"])
    )


def _optional_problems(event: Mapping[str, JsonValue]) -> list[str]:
    problems = []
    target = event.get("target")
    if target is not None and not (
        isinstance(target, Mapping)
        and set(target) <= {"entities", "types"}
        and all(isinstance(v, list) and all(isinstance(x, str) for x in v) for v in target.values())
    ):
        problems.append("target is not {entities?, types?} of strings")
    reason = event.get("reason")
    if reason is not None and not isinstance(reason, str):
        problems.append("reason is not a string")
    env = event.get("env")
    if env is not None and not (
        isinstance(env, Mapping)
        and "root_source" in env
        and set(env) <= {"root_source", "data_root"}
        and all(isinstance(v, str) for v in env.values())
    ):
        problems.append("env is not {root_source, data_root?}")
    return problems
