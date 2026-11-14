"""Single-op write commands: each builds a one-op change set for the WriteService (contracts B3).

References in arguments may be full ids, aliases or id suffixes (B1.9).
``--set attr=value`` goes to ``attrs_text`` and is coerced by the services
per datatype (B3.16); ``--set-json attr=<json>`` gives a typed value.
Common options: ``--dry-run``, ``--reason``, ``--idempotency-key``, and
``--expect REV`` on single-entity ops (B3.11).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from graph_cli.app_cli.commands.common import GRAPH, context
from graph_cli.app_cli.commands.meta import GRAPH_ERRORS
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.output_schemas import ref
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.changeset import (
    IDEMPOTENCY_KEY_RE,
    MAX_REASON,
    NAME_RE,
    ChangeSet,
    CreateOp,
    EdgeSpec,
    LinkOp,
    Op,
    RetractOp,
    SupersedeOp,
    TransitionOp,
    UnlinkOp,
    UpdateOp,
    load_document,
    normalize_valid_time,
)
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import TERM_REF_RE, EdgeId, EntityRef, TermRef, parse_entity_ref, parse_id
from graph_cli.kernel.records import RequestMeta
from graph_cli.query.plan import split_assignment
from graph_cli.services.read import graph_json

WRITE_ERRORS: Final = (
    *GRAPH_ERRORS,
    "graph.archived",
    "graph.readonly",
    "entity.not_found",
    "entity.ambiguous_ref",
    "changeset.too_large",
    "write.invalid",
    "conflict.idempotency_key_reuse",
    "access.reason_required",
    "ontology.pin_mismatch",
    "backend.lock_timeout",
    "write.migration_incomplete",
    "backend.corrupt",
    "audit.unavailable",
    "skills.outdated",
)
EXPECT_ERRORS: Final = (*WRITE_ERRORS, "conflict.version_mismatch")


def usage(argument: str, message: str, actual: JsonValue = None) -> GraphCliError:
    return GraphCliError(
        "usage.invalid_argument",
        f"{argument}: {message}",
        details={"argument": argument, "actual": actual},
    )


def _ref(text: str, argument: str) -> EntityRef:
    ref = parse_entity_ref(text, allow_abbrev=True)
    if ref is None:
        raise usage(argument, "expected a full id, an alias or 8-32 trailing hex digits", text)
    return ref


def _term(text: str, argument: str) -> TermRef:
    if not TERM_REF_RE.match(text):
        raise usage(argument, "expected a CURIE <prefix>:<Term>", text)
    return TermRef(text)


def _name(text: str, argument: str) -> str:
    if not NAME_RE.match(text):
        raise usage(argument, "expected a lowercase name", text)
    return text


def parse_texts(values: Sequence[str], argument: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        name, text = split_assignment(value, argument)
        if _name(name, argument) in result:
            raise usage(argument, f"{name!r} is given twice", value)
        result[name] = text
    return result


def parse_json_values(values: Sequence[str], argument: str) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for value in values:
        name, text = split_assignment(value, argument)
        if _name(name, argument) in result:
            raise usage(argument, f"{name!r} is given twice", value)
        try:
            result[name] = load_document(text)
        except GraphCliError as exc:
            raise usage(argument, f"{name!r} is not valid JSON: {exc.message}", text) from None
    return result


def parse_text_lists(values: Sequence[str], argument: str) -> dict[str, tuple[str, ...]]:
    """``attr=value`` repeated: the values of each attribute, in the given order."""
    result: dict[str, tuple[str, ...]] = {}
    for value in values:
        name, text = split_assignment(value, argument)
        result[_name(name, argument)] = (*result.get(name, ()), text)
    return result


def parse_json_lists(values: Sequence[str], argument: str) -> dict[str, tuple[JsonValue, ...]]:
    """``attr=<json>`` repeated: one value per flag, an array included."""
    result: dict[str, tuple[JsonValue, ...]] = {}
    for value in values:
        name, text = split_assignment(value, argument)
        try:
            item = load_document(text)
        except GraphCliError as exc:
            raise usage(argument, f"{name!r} is not valid JSON: {exc.message}", text) from None
        result[_name(name, argument)] = (*result.get(name, ()), item)
    return result


def merge_values(texts: dict[str, str], values: dict[str, JsonValue], argument: str) -> None:
    for name in texts:
        if name in values:
            raise usage(argument, f"{name!r} is given with --set and {argument}", name)


def _time(text: str | None, argument: str) -> str | None:
    if text is None:
        return None
    try:
        return normalize_valid_time(text)
    except GraphCliError as exc:
        raise usage(argument, exc.message, text) from None


def check_reason_and_key(reason: str | None, idempotency_key: str | None) -> None:
    if reason is not None and (not reason.strip() or len(reason) > MAX_REASON):
        raise usage("--reason", f"expected 1 to {MAX_REASON} characters")
    if idempotency_key is not None and not IDEMPOTENCY_KEY_RE.match(idempotency_key):
        raise usage("--idempotency-key", "expected [A-Za-z0-9._:-]{1,128}", idempotency_key)


def _changeset(op: Op, reason: str | None, idempotency_key: str | None) -> ChangeSet:
    check_reason_and_key(reason, idempotency_key)
    return ChangeSet(ops=(op,), reason=reason, idempotency_key=idempotency_key)


def _apply(
    state: CommandState,
    command: str,
    op: Op,
    *,
    graph: str | None,
    dry_run: bool,
    reason: str | None,
    idempotency_key: str | None,
    args: dict[str, JsonValue],
) -> Result:
    from graph_cli.services.write import WriteRequest  # loaded by write commands only

    changeset = _changeset(op, reason, idempotency_key)
    ctx = context(state)
    entry = ctx.select(graph)
    meta = RequestMeta.of(
        command,
        {
            "graph": entry.name,
            "dry_run": dry_run,
            "reason": reason,
            "idempotency_key": idempotency_key,
            **args,
        },
    )
    result = ctx.writer.apply(
        WriteRequest(
            actor=ctx.actor,
            graph=str(entry.id),
            changeset=changeset,
            dry_run=dry_run,
            request=meta,
            allow_abbrev=True,
        )
    )
    state.warnings += result.warnings
    return Result(data=result.to_json(), graph=graph_json(entry, result.seq))


def create(
    state: CommandState,
    type: str,
    set: tuple[str, ...],
    set_json: tuple[str, ...],
    edge: tuple[str, ...],
    valid_from: str | None,
    valid_to: str | None,
    graph: str | None,
    dry_run: bool,
    reason: str | None,
    idempotency_key: str | None,
) -> Result:
    texts, values = parse_texts(set, "--set"), parse_json_values(set_json, "--set-json")
    merge_values(texts, values, "--set-json")
    edges = []
    for value in edge:
        rel, target = split_assignment(value, "--edge")
        edges.append(EdgeSpec(_term(rel, "--edge"), _ref(target, "--edge")))
    op = CreateOp(
        type=_term(type, "type"),
        attrs=values,
        attrs_text=texts,
        edges=tuple(edges),
        valid_from=_time(valid_from, "--valid-from"),
        valid_to=_time(valid_to, "--valid-to"),
    )
    args: dict[str, JsonValue] = {
        "type": type,
        "set": list(set),
        "set_json": list(set_json),
        "edge": list(edge),
        "valid_from": valid_from,
        "valid_to": valid_to,
    }
    return _apply(
        state,
        "create",
        op,
        graph=graph,
        dry_run=dry_run,
        reason=reason,
        idempotency_key=idempotency_key,
        args=args,
    )


def update(
    state: CommandState,
    ref: str,
    set: tuple[str, ...],
    set_json: tuple[str, ...],
    unset: tuple[str, ...],
    add: tuple[str, ...],
    add_json: tuple[str, ...],
    remove: tuple[str, ...],
    remove_json: tuple[str, ...],
    expect: int | None,
    graph: str | None,
    dry_run: bool,
    reason: str | None,
    idempotency_key: str | None,
) -> Result:
    texts, values = parse_texts(set, "--set"), parse_json_values(set_json, "--set-json")
    merge_values(texts, values, "--set-json")
    names = [_name(name, "--unset") for name in unset]
    for name in names:
        if name in texts or name in values:
            raise usage("--unset", f"{name!r} is both set and unset", name)
    add_texts, add_values = parse_text_lists(add, "--add"), parse_json_lists(add_json, "--add-json")
    remove_texts = parse_text_lists(remove, "--remove")
    remove_values = parse_json_lists(remove_json, "--remove-json")
    seen: dict[str, str] = {name: "--set" for name in texts} | {n: "--set-json" for n in values}
    seen |= {name: "--unset" for name in names}
    for argument, given in (
        ("--add", add_texts),
        ("--add-json", add_values),
        ("--remove", remove_texts),
        ("--remove-json", remove_values),
    ):
        for name in given:
            if name in seen:
                raise usage(argument, f"{name!r} is given with {seen[name]} and {argument}", name)
            seen[name] = argument
    if not seen:
        raise GraphCliError(
            "usage.missing_argument", "update needs --set, --set-json, --unset, --add or --remove"
        )
    op = UpdateOp(
        _ref(ref, "ref"),
        values,
        texts,
        tuple(dict.fromkeys(names)),
        expect,
        add=add_values,
        add_text=add_texts,
        remove=remove_values,
        remove_text=remove_texts,
    )
    args: dict[str, JsonValue] = {
        "ref": ref,
        "set": list(set),
        "set_json": list(set_json),
        "unset": list(unset),
        "add": list(add),
        "add_json": list(add_json),
        "remove": list(remove),
        "remove_json": list(remove_json),
        "expect": expect,
    }
    return _apply(
        state,
        "update",
        op,
        graph=graph,
        dry_run=dry_run,
        reason=reason,
        idempotency_key=idempotency_key,
        args=args,
    )


def link(
    state: CommandState,
    ref: str,
    rel: str,
    target: str,
    qualifier: tuple[str, ...],
    qualifier_json: tuple[str, ...],
    target_rev: int | None,
    expect: int | None,
    graph: str | None,
    dry_run: bool,
    reason: str | None,
    idempotency_key: str | None,
) -> Result:
    texts = parse_texts(qualifier, "--qualifier")
    values = parse_json_values(qualifier_json, "--qualifier-json")
    merge_values(texts, values, "--qualifier-json")
    if target_rev is not None and target_rev < 1:
        raise usage("--target-rev", "expected 1 or more", target_rev)
    spec = EdgeSpec(_term(rel, "rel"), _ref(target, "target"), {**texts, **values}, target_rev)
    op = LinkOp(_ref(ref, "ref"), spec, expect)
    args: dict[str, JsonValue] = {
        "ref": ref,
        "rel": rel,
        "target": target,
        "qualifier": list(qualifier),
        "qualifier_json": list(qualifier_json),
        "target_rev": target_rev,
        "expect": expect,
    }
    return _apply(
        state,
        "link",
        op,
        graph=graph,
        dry_run=dry_run,
        reason=reason,
        idempotency_key=idempotency_key,
        args=args,
    )


def unlink(
    state: CommandState,
    ref: str,
    rel: str | None,
    target: str | None,
    edge_id: str | None,
    expect: int | None,
    graph: str | None,
    dry_run: bool,
    reason: str | None,
    idempotency_key: str | None,
) -> Result:
    if (edge_id is None) == (rel is None or target is None) or (edge_id and (rel or target)):
        raise GraphCliError("usage.missing_argument", "unlink needs REL and TARGET, or --edge-id")
    if edge_id is not None:
        try:
            op = UnlinkOp(_ref(ref, "ref"), edge_id=EdgeId(parse_id(edge_id)), expect_rev=expect)
        except GraphCliError:
            raise usage("--edge-id", "expected a full edge id", edge_id) from None
    else:
        assert rel is not None and target is not None
        op = UnlinkOp(
            _ref(ref, "ref"),
            rel=_term(rel, "rel"),
            target=_ref(target, "target"),
            expect_rev=expect,
        )
    args: dict[str, JsonValue] = {
        "ref": ref,
        "rel": rel,
        "target": target,
        "edge_id": edge_id,
        "expect": expect,
    }
    return _apply(
        state,
        "unlink",
        op,
        graph=graph,
        dry_run=dry_run,
        reason=reason,
        idempotency_key=idempotency_key,
        args=args,
    )


def transition(
    state: CommandState,
    ref: str,
    state_name: str,
    attr: str | None,
    expect: int | None,
    graph: str | None,
    dry_run: bool,
    reason: str | None,
    idempotency_key: str | None,
) -> Result:
    op = TransitionOp(
        _ref(ref, "ref"),
        _name(state_name, "state"),
        None if attr is None else _name(attr, "--attr"),
        expect,
    )
    args: dict[str, JsonValue] = {"ref": ref, "state": state_name, "attr": attr, "expect": expect}
    return _apply(
        state,
        "transition",
        op,
        graph=graph,
        dry_run=dry_run,
        reason=reason,
        idempotency_key=idempotency_key,
        args=args,
    )


def supersede(
    state: CommandState,
    by: str,
    targets: tuple[str, ...],
    graph: str | None,
    dry_run: bool,
    reason: str | None,
    idempotency_key: str | None,
) -> Result:
    refs = [_ref(t, "targets") for t in targets]
    if len(set(refs)) != len(refs):
        raise usage("targets", "a target is listed twice")
    op = SupersedeOp(_ref(by, "by"), tuple(refs))
    args: dict[str, JsonValue] = {"by": by, "targets": list(targets)}
    return _apply(
        state,
        "supersede",
        op,
        graph=graph,
        dry_run=dry_run,
        reason=reason,
        idempotency_key=idempotency_key,
        args=args,
    )


def retract(
    state: CommandState,
    ref: str,
    by: str | None,
    expect: int | None,
    graph: str | None,
    dry_run: bool,
    reason: str | None,
    idempotency_key: str | None,
) -> Result:
    op = RetractOp(_ref(ref, "ref"), None if by is None else _ref(by, "--by"), expect)
    args: dict[str, JsonValue] = {"ref": ref, "by": by, "expect": expect}
    return _apply(
        state,
        "retract",
        op,
        graph=graph,
        dry_run=dry_run,
        reason=reason,
        idempotency_key=idempotency_key,
        args=args,
    )


REF: Final = ParamSpec(
    "ref", "entity_ref", "Full id, alias or trailing hex of the id.", required=True, positional=True
)
COMMON: Final = (
    GRAPH,
    ParamSpec("--dry-run", "flag", "Validate and show the result without writing."),
    ParamSpec("--reason", "string", "Why this change is made (required for supersede, retract)."),
    ParamSpec("--idempotency-key", "string", "Key that makes a retry safe."),
)
EXPECT: Final = ParamSpec("--expect", "integer", "Refuse unless the entity is at this revision.")
SET: Final = ParamSpec(
    "--set",
    "assignment",
    "attr=value, coerced by the attribute's datatype; <value>@en names a language.",
    repeatable=True,
    pairs="map",
)
SET_JSON: Final = ParamSpec(
    "--set-json", "json_assignment", "attr=<json>, a typed value.", repeatable=True, pairs="map"
)


def _spec(
    name: str,
    action: str,
    summary: str,
    params: tuple[ParamSpec, ...],
    examples: tuple[Example, ...],
    handler: object,
    errors: tuple[str, ...] = WRITE_ERRORS,
) -> CommandSpec:
    return CommandSpec(
        path=(name,),
        kind="write",
        surface="act",
        action=action,
        summary=summary,
        params=(*params, *COMMON),
        examples=examples,
        errors=errors,
        handler=handler,  # type: ignore[arg-type]
        output=ref("write_result"),
    )


CREATE: Final = _spec(
    "create",
    "write.create",
    "Create one entity of a type.",
    (
        ParamSpec("type", "term", "Type CURIE.", required=True, positional=True),
        SET,
        SET_JSON,
        ParamSpec(
            "--edge",
            "assignment",
            "relation=<id>: an outgoing edge.",
            repeatable=True,
            pairs="multimap",
        ),
        ParamSpec("--valid-from", "string", "Start of validity: date or RFC 3339 timestamp."),
        ParamSpec("--valid-to", "string", "End of validity: date or RFC 3339 timestamp."),
    ),
    (
        Example("graph create <type> --set <attr>=<value>", "Create with one attribute."),
        Example(
            "graph create <type> --set <attr>=<value> --edge <relation>=<id> --dry-run --json",
            "Preview the result.",
        ),
    ),
    create,
)
UPDATE: Final = _spec(
    "update",
    "write.update",
    "Set, unset, add to or remove from attributes of one entity.",
    (
        REF,
        SET,
        SET_JSON,
        ParamSpec("--unset", "string", "Attribute to remove.", repeatable=True),
        ParamSpec(
            "--add",
            "assignment",
            "attr=value appended to a list attribute unless present; coerced by datatype.",
            repeatable=True,
            pairs="multimap",
        ),
        ParamSpec(
            "--add-json",
            "json_assignment",
            "attr=<json>, one typed value to add.",
            repeatable=True,
            pairs="multimap",
        ),
        ParamSpec(
            "--remove",
            "assignment",
            "attr=value dropped from a list attribute; coerced by datatype.",
            repeatable=True,
            pairs="multimap",
        ),
        ParamSpec(
            "--remove-json",
            "json_assignment",
            "attr=<json>, one typed value to remove.",
            repeatable=True,
            pairs="multimap",
        ),
        EXPECT,
    ),
    (
        Example("graph update <id> --set <attr>=<value>", "Change one attribute."),
        Example(
            "graph update <id> --add <attr>=<value> --dry-run --json",
            "Add one value to a list, no read needed.",
        ),
        Example(
            "graph update <id> --unset <attr> --expect 3 --json", "Remove one, guarded by revision."
        ),
    ),
    update,
    EXPECT_ERRORS,
)
LINK: Final = _spec(
    "link",
    "write.link",
    "Add an edge from one entity to another.",
    (
        REF,
        ParamSpec("rel", "term", "Relation CURIE.", required=True, positional=True),
        ParamSpec(
            "target", "entity_ref", "Target id, alias or id suffix.", required=True, positional=True
        ),
        ParamSpec(
            "--qualifier",
            "assignment",
            "qualifier=value on the edge.",
            repeatable=True,
            pairs="map",
        ),
        ParamSpec(
            "--qualifier-json",
            "json_assignment",
            "qualifier=<json> on the edge.",
            repeatable=True,
            pairs="map",
        ),
        ParamSpec("--target-rev", "integer", "The target revision the edge refers to."),
        EXPECT,
    ),
    (
        Example("graph link <id> <relation> <id>", "Link two entities."),
        Example(
            "graph link <id> <relation> <id> --qualifier <attr>=<value> --json",
            "Link with a qualifier.",
        ),
    ),
    link,
    EXPECT_ERRORS,
)
UNLINK: Final = _spec(
    "unlink",
    "write.unlink",
    "Remove an edge by relation and target, or by edge id.",
    (
        REF,
        ParamSpec("rel", "term", "Relation CURIE.", positional=True),
        ParamSpec("target", "entity_ref", "Target id, alias or id suffix.", positional=True),
        ParamSpec("--edge-id", "string", "The edge id instead of REL and TARGET."),
        EXPECT,
    ),
    (
        Example("graph unlink <id> <relation> <id>", "Remove the edge to a target."),
        Example("graph unlink <id> --edge-id <id> --json", "Remove one edge by id."),
    ),
    unlink,
    EXPECT_ERRORS,
)
TRANSITION: Final = _spec(
    "transition",
    "write.transition",
    "Move an entity's lifecycle to another state.",
    (
        REF,
        ParamSpec(
            "state", "string", "Target state.", required=True, positional=True, keyword="state_name"
        ),
        ParamSpec("--attr", "string", "Lifecycle attribute, if the type has several."),
        EXPECT,
    ),
    (
        Example("graph transition <id> <state>", "Change the state."),
        Example("graph transition <id> <state> --dry-run --json", "Check guards without writing."),
    ),
    transition,
    EXPECT_ERRORS,
)
SUPERSEDE: Final = _spec(
    "supersede",
    "write.supersede",
    "Replace entities by another one; needs --reason.",
    (
        ParamSpec("by", "entity_ref", "The replacement.", required=True, positional=True),
        ParamSpec(
            "targets",
            "entity_ref",
            "Entities it replaces.",
            required=True,
            repeatable=True,
            positional=True,
        ),
    ),
    (
        Example("graph supersede <id> <id> --reason <value>", "One replaces another."),
        Example("graph supersede <id> <id> <id> --reason <value> --json", "One replaces two."),
    ),
    supersede,
)
RETRACT: Final = _spec(
    "retract",
    "write.retract",
    "Withdraw an entity; needs --reason.",
    (REF, ParamSpec("--by", "entity_ref", "The entity that retracts it."), EXPECT),
    (
        Example("graph retract <id> --reason <value>", "Withdraw an entity."),
        Example(
            "graph retract <id> --by <id> --reason <value> --json", "Withdraw, citing a correction."
        ),
    ),
    retract,
    EXPECT_ERRORS,
)

SPECS: Final = (CREATE, UPDATE, LINK, UNLINK, TRANSITION, SUPERSEDE, RETRACT)
