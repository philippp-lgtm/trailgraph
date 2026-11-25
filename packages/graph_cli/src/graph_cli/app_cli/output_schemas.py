"""The shapes of ``graph`` answers used by more than one command (contracts B9.3, B11.10).

Each definition names the code that produces it. Commands declare their own ``data`` next
to their spec and point here with ``ref``. Attribute values, qualifiers, query cells and
parameters depend on the graph's ontology or the query, so they stay any JSON value.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from graph_cli.app_cli import view_schemas
from graph_cli.cli_kit import output_schemas as kit
from graph_cli.cli_kit.output_schemas import (
    ANY,
    BOOLEAN,
    INTEGER,
    STRING,
    Schema,
    array,
    enum,
    nullable,
    obj,
    ref,
)
from graph_cli.kernel.canonical import JsonObject, JsonValue

NULLABLE_STRING: Final = nullable(STRING)
# ``request.source`` as ``history`` and ``changes`` show it (kernel.records.SourceRef)
SOURCE: Final = obj({"name": STRING}, {"version": STRING, "run": STRING, "kind": STRING})
STRINGS: Final = array(STRING)


def _entity() -> Schema:
    """``read.entity_json``: the content (validity only when set), every key with
    ``--full``, or with ``--fields`` the always shown ones, ``attrs`` and the named parts.
    ``names_mapped`` only with ``--full``, and only for a past revision shown under the
    pinned model's names (migration plan M7)."""
    return obj(
        {
            "id": STRING,
            "alias": NULLABLE_STRING,
            "type": STRING,
            "rev": INTEGER,
            "record_status": STRING,
            "attrs": obj(extra=ANY),
        },
        {
            "edges": array(ref("edge")),
            "valid_from": NULLABLE_STRING,
            "valid_to": NULLABLE_STRING,
            "created_at": STRING,
            "recorded_at": STRING,
            "tx_seq": INTEGER,
            "ontology_version": STRING,
            "content_hash": STRING,
            "names_mapped": BOOLEAN,
        },
    )


def _write_result() -> dict[str, Schema]:
    """``services.write.WriteResult.to_json`` (B3); ``entities`` is null with ``--summary``."""
    entity = obj(
        {
            "id": NULLABLE_STRING,  # null for a dry-run create
            "alias": NULLABLE_STRING,
            "rev": INTEGER,
            "type": STRING,
            "ops": STRINGS,
            "local": NULLABLE_STRING,
            "changed": BOOLEAN,
        }
    )
    return {
        "tx_id": NULLABLE_STRING,
        "seq": nullable(INTEGER),
        "event_id": NULLABLE_STRING,
        "dry_run": BOOLEAN,
        "idempotent_replay": BOOLEAN,
        "id": NULLABLE_STRING,
        "version": nullable(INTEGER),
        "entity_count": INTEGER,
        "entities": nullable(array(entity)),
        "findings": array(ref("finding")),
    }


# The keys of a write result, for answers that add their own (``file add``).
WRITE_RESULT: Final = _write_result()
# ``services.write.WriteResult.to_json`` of a reconcile document: the source and, per scope, what
# the plan did (``sources.reconcile.ScopeSummary``); ``retired_keys`` are item keys, in a write
# at most 20 of the ``retired`` entities, in a dry run all of them.
RECONCILE: Final = obj(
    {
        "source": SOURCE,
        "scopes": array(
            obj(
                {
                    "type": STRING,
                    "under": STRING,
                    "complete": BOOLEAN,
                    "created": INTEGER,
                    "updated": INTEGER,
                    "unchanged": INTEGER,
                    "moved": INTEGER,
                    "adopted": INTEGER,
                    "kept": INTEGER,
                    "retired": INTEGER,
                    "revived": INTEGER,
                    "retired_keys": STRINGS,
                }
            )
        ),
    }
)
WRITE_RESULT_OPTIONAL: Final = {"reconcile": RECONCILE}  # only for a reconcile document


def _facet() -> Schema:
    """``kernel.actor.Facet.to_json``: an absent facet has only its assurance."""
    return obj(
        {"assurance": enum("verified", "asserted", "absent")},
        {k: STRING for k in ("id", "name", "type", "parent", "source")},
    )


def _actor() -> Schema:
    """``kernel.actor.ActorContext.to_json``; ``roles`` only for a token that holds some."""
    facets = {name: ref("facet") for name in ("principal", "agent", "session", "device", "project")}
    client = obj({"name": STRING, "version": STRING, "entrypoint": STRING})
    return obj({**facets, "client": client}, {"pid": INTEGER, "roles": STRINGS})


def _program_step() -> Schema:
    """``catalog.dist.program_step`` and ``bootstrap``: the program's own update."""
    return obj(
        {
            "name": STRING,
            "action": enum("skipped", "current", "outdated", "updated", "installed", "self_update"),
        },
        {
            "build": NULLABLE_STRING,
            "server": NULLABLE_STRING,
            "reason": STRING,
            "previous": NULLABLE_STRING,
        },
    )


def _skill_step() -> Schema:
    """``skills.install.Step.to_json``."""
    return obj(
        {
            "skill": STRING,
            "kind": enum("app", "graph"),
            "action": enum("create", "replace", "unchanged", "refused"),
            "installed": nullable(enum("intact", "legacy", "modified", "unexpected", "foreign")),
        }
    )


def _skill_refresh() -> Schema:
    """``app_cli.commands.self_update.refresh``: one scope through the installed program."""
    return obj(
        {"scope": enum("user", "project"), "dir": NULLABLE_STRING},
        {
            "skills": array(ref("skill_step")),
            "unavailable": array(ref("skill_unavailable")),
            "skipped_graphs": array(ref("graph_reason")),
            "counts": nullable(ref("skill_counts")),
        },
    )


def _model_header() -> dict[str, Schema]:
    """``services.describe._envelope``: the keys of every ``graph_cli.model/1`` document."""
    return {
        "schema": {"const": "graph_cli.model/1"},
        "graph": obj({"id": STRING, "name": STRING, "language": NULLABLE_STRING}),
        "ontology": ref("ontology_ref"),
    }


def _field() -> Schema:
    """``services.describe._fields``."""
    return obj(
        {
            "name": STRING,
            "type": STRING,
            "kind": STRING,
            "base": STRING,
            "required": BOOLEAN,
            "many": BOOLEAN,
            "unique": BOOLEAN,
            "default": ANY,
            "mutability": STRING,
            "role": nullable(enum("label", "alt_label", "hidden_label")),
            "limits": obj(extra=ANY),
            "values": nullable(STRINGS),
            "declared_in": STRING,
            "summary": STRING,
            "deprecated": BOOLEAN,
        },
        optional={"markings": STRINGS},
    )


def _lifecycle() -> Schema:
    """``services.describe._lifecycle``; ``attr`` is null for a lifecycle named by CURIE."""
    transition = obj(
        {
            "from": STRING,
            "to": STRING,
            "guards": array(obj({"id": STRING, "summary": STRING})),
        }
    )
    return obj(
        {
            "attr": NULLABLE_STRING,
            "lifecycle": STRING,
            "states": array(obj({"name": STRING, "summary": STRING})),
            "initial": STRINGS,
            "terminal": STRINGS,
            "transitions": array(transition),
            "summary": STRING,
            "deprecated": BOOLEAN,
        }
    )


DEFS: Final[dict[str, Schema]] = kit.merge_defs(
    kit.DEFS,
    view_schemas.DEFS,  # the view model and its sections
    {
        "entity": _entity(),
        "edge": obj(  # ``services.read.json_shapes.edge_json``: the id with --full only
            {"rel": STRING, "target": STRING},
            {"edge_id": STRING, "qualifiers": obj(extra=ANY), "target_rev": INTEGER},
        ),
        "write_result": obj(WRITE_RESULT, WRITE_RESULT_OPTIONAL),
        "facet": _facet(),
        "actor": _actor(),
        "program": obj({"name": STRING, "version": STRING}),
        "program_step": _program_step(),
        "skill_target": obj({"scope": enum("user", "project"), "path": STRING}, {"dir": STRING}),
        "skill_step": _skill_step(),
        "skill_unavailable": obj({"skill": STRING, "graph": STRING, "reason": STRING}),
        "skill_counts": obj(extra=INTEGER),
        "skill_refresh": _skill_refresh(),
        "graph_reason": obj({"graph": STRING, "reason": STRING}),
        "file_origin": obj({"root": STRING, "path": STRING, "resolved": NULLABLE_STRING}),
        "ontology_ref": obj({"bundle": STRING, "version": STRING}),
        "field": _field(),
        "lifecycle": _lifecycle(),
    },
)


def model_document(
    required: dict[str, Schema], optional: dict[str, Schema] | None = None
) -> Schema:
    """A ``graph_cli.model/1`` document (B11.5) with its own keys."""
    return obj({**_model_header(), **required}, optional)


def standalone(schema: Schema, defs: Mapping[str, Schema] = DEFS) -> JsonObject:
    """``schema`` with every ``$ref`` into ``defs`` replaced by the definition it names, so that it
    stands alone. A definition that refers to itself is a bug (``ValueError``)."""

    def inline(node: JsonValue, active: tuple[str, ...]) -> JsonValue:
        if isinstance(node, list):
            return [inline(item, active) for item in node]
        if not isinstance(node, Mapping):
            return node
        target = node.get("$ref")
        if isinstance(target, str):
            name = target.removeprefix("#/$defs/")
            if name in active:
                raise ValueError(f"output schema definition {name!r} refers to itself")
            return inline(defs[name], (*active, name))
        return {key: inline(value, active) for key, value in node.items()}

    found = inline(schema, ())
    assert isinstance(found, dict)
    return found
