"""Fixed vocabulary of the IR ``graph_cli.ir/1`` and the compiled form (contracts B7).

Only generic schema words live here; no domain term may ever appear in this
package (vocabulary leak test).
"""

from __future__ import annotations

import re
from typing import Final

IR_FORMAT: Final = "graph_cli.ir/1"
COMPILED_SCHEMA: Final = "graph_cli.compiled_ontology/1"
# The compiled form of a model that uses a feature older programs must not ignore (markings):
# its ``requires`` lists them, and a reader refuses a feature it does not know. A model without
# such features keeps ``/1``, its bytes and its artifact hash.
COMPILED_SCHEMA_V2: Final = "graph_cli.compiled_ontology/2"
MARKINGS_FEATURE: Final = "markings"
COMPILED_FEATURES: Final = frozenset({MARKINGS_FEATURE})
DIFF_SCHEMA: Final = "graph_cli.ontology_diff/1"

CORE_MODULE: Final = "core"
# The modules shipped as package data (``ontology_layer/modules/``); a graph cannot shadow them.
BUILTIN_MODULES: Final = frozenset(
    {
        CORE_MODULE,
        "provenance",
        "files",
        "files_pdf",
        "files_image",
        "files_table",
        "files_sqlite",
    }
)
FILE_TYPE: Final = "files:File"  # the stored-file class of the standard module files (B7.8)
DEFAULT_NAMESPACE: Final = "urn:graph_cli:onto:{module}#"
# The migration file of a version directory (``migration.yaml``, format ``graph_cli.migration/1``)
# lives next to the module files, so its stem is no module id.
MIGRATION_FILE: Final = "migration.yaml"
MIGRATION_FORMAT: Final = "graph_cli.migration/1"
RESERVED_MODULE_IDS: Final = frozenset({"migration"})

BASE_TYPES: Final = frozenset(
    {
        "string",
        "text",
        "integer",
        "decimal",
        "boolean",
        "date",
        "datetime",
        "duration",
        "iri",
        "json",
        "external_ref",
        "blob",
        "local_path",
        "lang_string",
        "lang_text",
    }
)
# Language-tagged text {"lang": <BCP 47 tag>, "value": <text>} (rdf:langString): short
# and long, like string and text.
LANG_BASES: Final = frozenset({"lang_string", "lang_text"})
# Base types a label role may name: plain and language-tagged short text.
LABEL_BASES: Final = frozenset({"string", "lang_string"})
# Base types whose values are not scalars: unique and facets do not apply.
NON_SCALAR_BASES: Final = frozenset({"json", "external_ref", "local_path"})
# Base types for attributes only, never for qualifiers (a stored file is referenced
# by an entity, not by an edge).
ATTRIBUTE_ONLY_BASES: Final = frozenset({"blob", "local_path"})

FACETS_BY_BASE: Final[dict[str, frozenset[str]]] = {
    "string": frozenset({"min_length", "max_length", "pattern", "single_line"}),
    "text": frozenset({"min_length", "max_length", "pattern", "single_line"}),
    "lang_string": frozenset({"min_length", "max_length", "pattern", "single_line"}),
    "lang_text": frozenset({"min_length", "max_length", "pattern", "single_line"}),
    "iri": frozenset({"min_length", "max_length", "pattern"}),
    "integer": frozenset({"min", "max"}),
}
ALL_FACETS: Final = frozenset({"min", "max", "min_length", "max_length", "pattern", "single_line"})

MUTABILITY_CLASSES: Final = ("immutable", "lifecycle", "content", "operational")
# Strictness order for the classifier: a move to a higher rank is stricter.
MUTABILITY_RANK: Final[dict[str, int]] = {"operational": 0, "content": 1, "immutable": 2}

CHARACTERISTICS: Final = ("acyclic", "symmetric", "transitive")
# What a write that makes an edge's target inactive (retract, supersede) meets, per relation;
# the first is the default.
TARGET_INACTIVE_POLICIES: Final = ("allow", "warn", "refuse")
SEVERITIES: Final = ("error", "warning")
RULE_TIMINGS: Final = ("write", "batch")
MAPPING_KINDS: Final = ("exact", "close", "related", "broad", "narrow")
# SKOS label roles of an attribute, main name first (prefLabel, altLabel, hiddenLabel).
LABEL_ROLES: Final = ("label", "alt_label", "hidden_label")

# Reserved system field names (B7.8): never attribute names.
SYSTEM_FIELDS: Final = frozenset(
    {
        "id",
        "type",
        "rev",
        "record_status",
        "alias",
        "created_at",
        "recorded_at",
        "tx_id",
        "tx_seq",
        "valid_from",
        "valid_to",
        "content_hash",
        "ontology_version",
    }
)
# Reserved edge keys: never qualifier names.
EDGE_FIELDS: Final = frozenset({"edge_id", "rel", "target", "target_rev", "valid_from", "valid_to"})

MODULE_ID_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
PREFIX_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,15}$")
TYPE_NAME_RE: Final = re.compile(r"^[A-Z][A-Za-z0-9]{0,63}$")  # classes, datatypes, enums, ...
LOWER_NAME_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")  # relations, attributes, values, ids
NAMESPACE_NS_RE: Final = re.compile(r"^[a-z][a-z0-9_]{1,31}$")  # annotation namespaces
CURIE_RE: Final = re.compile(r"^([a-z][a-z0-9_]{0,15}):([A-Za-z][A-Za-z0-9_]{0,63})$")
DISPLAY_PREFIX_RE: Final = re.compile(r"^[A-Z][A-Z0-9]{0,7}$")
FILE_ROOT_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,62}$")  # root names of local_path values
