"""The shapes of ``graph-admin`` answers used by more than one command (contracts B9.3,
B11.10).

Each definition names the code that produces it. Commands declare their own ``data`` next
to their spec and point here with ``ref``. The definitions of the envelope and the
inventory come from ``cli_kit.output_schemas``, the program's own update from
``app_cli.output_schemas`` (both programs run ``catalog.dist.program_step``).
"""

from __future__ import annotations

from typing import Final

from graph_cli.access_control.documents import (
    ALLOW_KEYS,
    ASSERTED_KEYS,
    GRAPH_LAYER,
    INSTALLATION_LAYER,
    POLICY_SCHEMA,
    TYPE_LIMITS,
)
from graph_cli.app_cli.output_schemas import DEFS as APP_DEFS
from graph_cli.catalog.tokens import ADMIN_SCOPE, APP_SCOPE
from graph_cli.cli_kit import output_schemas as kit
from graph_cli.cli_kit.output_schemas import (
    BOOLEAN,
    INTEGER,
    OPEN,
    STRING,
    Schema,
    array,
    enum,
    nullable,
    obj,
    ref,
)
from graph_cli.kernel.skill_marker import GENERATED, STATIC
from graph_cli.ontology_layer.vocabulary import DIFF_SCHEMA

NULLABLE_STRING: Final = nullable(STRING)
STRINGS: Final = array(STRING)
LAYER: Final = enum(INSTALLATION_LAYER, GRAPH_LAYER)
SCOPE: Final = enum(APP_SCOPE, ADMIN_SCOPE)
SKILL_KIND: Final = enum(STATIC, GENERATED)
# Only in the admin API's answer: the files the client writes, base64 by name (B14.15).
RETURNED_FILES: Final = obj(extra=STRING)


def _diff() -> dict[str, Schema]:
    """``ontology_layer.diff.DiffReport.to_json`` (B7.12)."""
    change = obj(
        {
            "path": STRING,
            "kind": STRING,
            "level": enum("patch", "minor", "major"),
            "detail": OPEN,
        }
    )
    return {
        "schema": {"const": DIFF_SCHEMA},
        "from": STRING,
        "to": STRING,
        "level": enum("none", "patch", "minor", "major"),
        "changes": array(change),
    }


# The keys of an ontology diff, for answers that add their own (``ontology diff``).
DIFF: Final = _diff()

# The keys of ``services_admin.release.plan``'s data without ``graph_id``: what a release
# did; ``diff`` and ``alias_prefixes_added`` only when the pin changes.
RELEASE: Final = {
    "from": ref("ontology_pin"),
    "to": ref("ontology_pin"),
    "released": BOOLEAN,
    "pinned": BOOLEAN,
}
RELEASE_OPTIONAL: Final = {
    "diff": ref("ontology_diff"),
    "alias_prefixes_added": obj(extra=STRING),
}


def _policy_document() -> Schema:
    """``access_control.documents`` (B6.6): the grammar ``document_problems`` checks. A
    principal matcher maps a facet key to a value or a list of alternatives."""
    principal = obj(
        optional={key: {"anyOf": [STRING, STRINGS]} for key in (*ALLOW_KEYS, *ASSERTED_KEYS)}
    )
    resource = {"anyOf": [STRING, STRINGS]}
    rule = obj(
        {
            "id": STRING,
            "effect": enum("allow", "deny"),
            "action": STRINGS,
            "resource": resource,
        },
        {
            "principal": principal,
            "unless": array(obj(optional={"principal": principal, "resource": resource})),
        },
    )
    obligation = obj({"action": STRINGS}, {"require_reason": BOOLEAN, "max_batch": INTEGER})
    return obj(
        {"schema": {"const": POLICY_SCHEMA}, "id": STRING, "rules": array(rule)},
        {
            "layer": LAYER,
            "graph": STRING,
            "projects": STRINGS,
            "types": obj(extra=enum(*TYPE_LIMITS)),
            "obligations": array(obligation),
        },
    )


# ``services_admin.tokens._listed``: a token as listed; ``created_at`` is null for a token
# written before it was recorded.
TOKEN: Final = {
    "name": STRING,
    "scope": SCOPE,
    "project": NULLABLE_STRING,
    "expires": NULLABLE_STRING,
    "expired": BOOLEAN,
    "created_at": NULLABLE_STRING,
    "hash_prefix": STRING,
}

# ``services_admin.enroll._listed``: an enrollment code as listed, never the code.
ENROLLMENT: Final = {
    "id": STRING,
    "project": STRING,
    "scope": SCOPE,
    "expires_at": STRING,
    "expired": BOOLEAN,
    "created_at": STRING,
    "hash_prefix": STRING,
}

CATALOG_SUMMARY: Final[dict[str, Schema]] = {
    "count": INTEGER,
    "hash": STRING,
    "compiled_hash": STRING,
    "published_against": ref("ontology_pin"),
}
DEFS: Final[dict[str, Schema]] = kit.merge_defs(
    kit.DEFS,
    {"program_step": APP_DEFS["program_step"], "ontology_ref": APP_DEFS["ontology_ref"]},
    {
        "ontology_pin": obj(  # kernel.records.OntologyPin.to_json
            {"bundle": STRING, "version": STRING, "artifact_hash": STRING}
        ),
        "ontology_diff": obj(DIFF),
        "release": obj(RELEASE, RELEASE_OPTIONAL),
        "finding_counts": obj(  # services_admin.audit.counts_of
            {"error": INTEGER, "warning": INTEGER, "info": INTEGER}
        ),
        "skill_summary": obj(  # kernel.ports.catalog.SkillSlot.summary
            {"kind": SKILL_KIND, "hash": STRING, "published_against": ref("ontology_pin")}
        ),
        "function_problem": obj(  # graph_cli.kinetic.format.Problem.to_json
            {
                "function": STRING,
                "code": STRING,
                "message": STRING,
                "path": NULLABLE_STRING,
                "line": nullable(INTEGER),
                "details": OPEN,
            }
        ),
        "action_problem": obj(  # graph_cli.kinetic.params.Problem.to_json for actions
            {
                "action": STRING,
                "code": STRING,
                "message": STRING,
                "path": NULLABLE_STRING,
                "line": nullable(INTEGER),
                "details": OPEN,
            }
        ),
        "functions_summary": obj(CATALOG_SUMMARY),  # kernel.ports.catalog.KineticSlot.summary
        "actions_summary": obj(CATALOG_SUMMARY),  # the same for kind actions
        "source_skill": obj(  # services_admin.sources._skill_json
            {"name": STRING, "kind": SKILL_KIND, "hash": STRING}
        ),
        "source_functions": obj(  # services_admin.sources._catalog_json
            {"count": INTEGER, "hash": STRING}
        ),
        "source_actions": obj(  # the same for the actions
            {"count": INTEGER, "hash": STRING}
        ),
        "policy_document": _policy_document(),
        "token": obj(TOKEN),
        "enrollment": obj(ENROLLMENT),
    },
)
