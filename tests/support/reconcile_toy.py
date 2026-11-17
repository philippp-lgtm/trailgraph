"""A toy ontology and a memory graph for the reconcile tests: installations of packages on
hosts (a unique ``source_key`` of a ``Line`` datatype, a lifecycle, a list, a decimal, language
tagged text, two relations), abstract items with two concrete types, rows whose key is immutable,
not unique or a number, a marked attribute, and the callers of the access tests.

The planner tests (``tests/unit/sources``) and the write path tests (``tests/unit/services``)
share it, so both read the same graph.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.changeset import ChangeSet, parse_changeset
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import EntityId, TermRef
from graph_cli.kernel.query_ast import Cmp, Query
from graph_cli.kernel.records import RequestMeta, Revision
from graph_cli.ontology_layer.bundle import LayeredSource, MappingSource
from graph_cli.ontology_layer.compiler import compile_bundle
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.services.write import WriteRequest, WriteResult
from graph_cli.sources.reconcile import ReconcilePlan, expanded, plan_reconcile
from support.services import ServiceEnv

REQUEST = RequestMeta("apply", "sha256:" + "0" * 64)
ID = "0192f3c0-11aa-7e01-b2c3-d4e5f6a7b8c9"

_MODULE = """\
format: graph_cli.ir/1
module: {id: example_module, prefix: ex, version: 1.0.0, summary: Installed packages.}
markings:
  personal: {summary: Personal data.}
datatypes:
  Line: {summary: One line of text., base: string, single_line: true}
lifecycles:
  RowState:
    summary: Whether a row is present.
    states:
      present: {summary: Present., initial: true}
      removed: {summary: Removed.}
    transitions:
      - {from: present, to: removed, summary: Remove it.}
      - {from: removed, to: present, summary: Bring it back.}
classes:
  Package:
    summary: A package.
    display_prefix: PKG
    attributes:
      name: {summary: Name., type: string, required: true, unique: true}
      source_key: {summary: Key., type: Line, unique: true}
  Host:
    summary: A host.
    attributes:
      hostname: {summary: Host name., type: string, required: true, unique: true}
  Installation:
    summary: A package installed on a host.
    attributes:
      source_key: {summary: Key., type: Line, unique: true}
      package: {summary: Package name., type: string}
      version: {summary: Version., type: string}
      count: {summary: Count., type: integer}
      ratio: {summary: Ratio., type: decimal}
      title: {summary: Title., type: lang_string}
      tags: {summary: Tags., type: string, many: true}
      state: {summary: State., lifecycle: RowState, required: true}
      owner: {summary: Who installed it., type: string, markings: [personal]}
  Folder:
    summary: A folder of a host that a scan found or expects.
    attributes:
      source_key: {summary: Key., type: Line, unique: true}
      path: {summary: Path., type: string}
      present: {summary: Whether the scan found it., type: boolean}
  Pinned:
    summary: A row whose key never changes.
    attributes:
      source_key: {summary: Key., type: Line, unique: true, mutability: immutable}
  Item:
    summary: Something that is kept.
    abstract: true
    attributes:
      source_key: {summary: Key., type: Line, unique: true}
      label: {summary: Label., type: string}
  Tool:
    summary: A tool.
    is_a: Item
  Gadget:
    summary: A gadget.
    is_a: Item
  Loose:
    summary: A row whose key is not unique.
    attributes:
      source_key: {summary: Key., type: Line}
  Serial:
    summary: A row with a number as its unique value.
    attributes:
      source_key: {summary: Key., type: integer, unique: true}
  Plain:
    summary: A row without a lifecycle.
    attributes:
      source_key: {summary: Key., type: Line, unique: true}
      label: {summary: Label., type: string}
relations:
  installation_of:
    summary: The package of an installation.
    domain: Installation
    range: Package
    on_target_inactive: refuse
    qualifiers:
      pinned: {summary: Whether the version is pinned., type: boolean}
  installed_on:
    summary: The host of an installation.
    domain: Installation
    range: Host
  folder_on:
    summary: The host of a folder.
    domain: Folder
    range: Host
"""


def _model() -> CompiledModel:
    outcome = compile_bundle(
        "example_module", LayeredSource(MappingSource({"example_module": _MODULE.encode()}))
    )
    assert outcome.model is not None, [f.to_json() for f in outcome.findings]
    return outcome.model


MODEL = _model()
SOURCE = {"name": "inventory", "version": "collector 3", "run": "run-1"}


def doc(*scopes: dict[str, Any], **top: Any) -> dict[str, Any]:
    return {"source": SOURCE, "reconcile": list(scopes), **top}


def scope(**extra: Any) -> dict[str, Any]:
    return {
        "type": "ex:Installation",
        "key": "source_key",
        "fields": ["package", "version"],
        "items": [],
        **extra,
    }


def row(key: str, version: str = "1", **extra: Any) -> dict[str, Any]:
    return {"key": key, "attrs": {"package": key.rsplit("/", 1)[-1], "version": version}, **extra}


def rows(*names: str, prefix: str = "host_a/", version: str = "1") -> list[dict[str, Any]]:
    return [row(prefix + name, version) for name in names]


class World:
    """A memory graph on the model above, with the planner and the real write service."""

    def __init__(self, tmp_path: Path, **kw: Any) -> None:
        self.env = ServiceEnv(
            "memory", "inventory", tmp_path, model_override=MODEL, language="en", **kw
        )

    @classmethod
    def on_files(cls, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> "World":
        """The same graph on the files backend: the toy module is written to a scratch
        ontologies folder, which the fixtures and the service environment read instead of the
        tracked one."""
        import support.fixtures
        import support.services

        folder = tmp_path / "ontologies" / "example_module" / "1.0.0"
        folder.mkdir(parents=True)
        (folder / "example_module.yaml").write_text(_MODULE)
        monkeypatch.setattr(support.fixtures, "ONTOLOGIES", tmp_path / "ontologies")
        monkeypatch.setattr(support.services, "ONTOLOGIES", tmp_path / "ontologies")
        found = cls.__new__(cls)
        found.env = ServiceEnv("files", "example_module", tmp_path / "root", language="en")
        return found

    # ---- planning and applying ----

    def plan(
        self, document: Mapping[str, JsonValue], bindings: Mapping[str, EntityId] | None = None
    ) -> ReconcilePlan:
        cs = parse_changeset(dict(document))
        assert cs.source is not None
        return plan_reconcile(
            cs.source,
            cs.reconcile,
            self.env.snapshot(),
            MODEL,
            frame_ops=cs.ops,
            bindings=bindings,
            language="en",
        )

    def run(self, document: Mapping[str, JsonValue]) -> tuple[ReconcilePlan, WriteResult | None]:
        """Plan, expand and write; then the same document must plan to nothing."""
        cs = parse_changeset(dict(document))
        plan = self.plan(document)
        result = None
        if plan.ops or cs.ops:
            result = self.env.service.apply(
                WriteRequest(self.env.actor, self.env.name, expanded(cs, plan), False, REQUEST)
            )
        again = self.plan(document)
        if not cs.ops:  # the document's own ops would be applied again by a real second run
            assert again.ops == (), again.ops
        assert all(
            s.unchanged == len(c.items) for s, c in zip(again.scopes, cs.reconcile, strict=True)
        )
        return plan, result

    def plain(self, *ops: dict[str, Any], reason: str = "test") -> dict[str, EntityId]:
        """Plain ops written as they are (the locals they bind, by name)."""
        result = self.env.service.apply(
            WriteRequest(
                self.env.actor,
                self.env.name,
                ChangeSet(ops=parse_changeset({"ops": list(ops)}).ops, reason=reason),
                False,
                REQUEST,
            )
        )
        return {e.local: e.id for e in result.entities if e.local and e.id is not None}

    def refused(self, document: Mapping[str, JsonValue]) -> GraphCliError:
        with pytest.raises(GraphCliError) as info:
            self.plan(document)
        return info.value

    # ---- reading ----

    def find(self, key: str, *, type_curie: str = "ex:Installation") -> Revision | None:
        types = tuple(TermRef(t) for t in MODEL.subtypes(type_curie))
        page = self.env.snapshot().query(Query(types=types, where=Cmp("source_key", "eq", key)))
        return page.items[0] if page.items else None

    def keys(self, type_curie: str = "ex:Installation") -> list[str]:
        types = tuple(TermRef(t) for t in MODEL.subtypes(type_curie))
        page = self.env.snapshot().query(Query(types=types, limit=500))
        return sorted(str(r.attrs["source_key"]) for r in page.items if "source_key" in r.attrs)

    def count(self, type_curie: str) -> int:
        return self.env.snapshot().query(Query(types=(TermRef(type_curie),), limit=500)).total
