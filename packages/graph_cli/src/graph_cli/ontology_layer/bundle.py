"""Module sources and bundle loading: imports, cycles, ranges and name resolution (B7.1, B7.8).

A bundle is a main module plus everything it imports, transitively. ``core``
is imported implicitly by every module. Built-in modules (``vocabulary.BUILTIN_MODULES``: ``core``,
``provenance``, ``files`` and its kind modules) ship as package data and cannot be shadowed.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Literal, Protocol

from graph_cli.ontology_layer.ir import (
    IrAttribute,
    IrClass,
    IrModule,
    parse_module,
)
from graph_cli.ontology_layer.report import Report
from graph_cli.ontology_layer.semver import Version
from graph_cli.ontology_layer.vocabulary import (
    BASE_TYPES,
    CORE_MODULE,
    CURIE_RE,
    MODULE_ID_RE,
    RESERVED_MODULE_IDS,
)

TermKind = Literal["class", "datatype", "enum", "lifecycle", "relation", "marking"]


class ModuleSource(Protocol):
    def read(self, module_id: str) -> bytes | None: ...


class BuiltinModules:
    """``core`` and the opt-in standard modules, shipped inside the package."""

    def read(self, module_id: str) -> bytes | None:
        resource = resources.files("graph_cli.ontology_layer") / "modules" / f"{module_id}.yaml"
        return resource.read_bytes() if resource.is_file() else None


class MappingSource:
    """Modules given as ``{module_id: bytes}`` (tests, release sources)."""

    def __init__(self, files: Mapping[str, bytes]) -> None:
        self._files = dict(files)

    def read(self, module_id: str) -> bytes | None:
        return self._files.get(module_id)


class DirectorySource:
    """Modules as ``<dir>/<module_id>.yaml`` files; ``migration.yaml`` is no module."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def read(self, module_id: str) -> bytes | None:
        if not MODULE_ID_RE.match(module_id):  # never a path outside the directory
            return None
        if module_id in RESERVED_MODULE_IDS:
            return None
        path = self._directory / f"{module_id}.yaml"
        return path.read_bytes() if path.is_file() else None


class LayeredSource:
    """Built-ins first, then the given sources in order."""

    def __init__(self, *sources: ModuleSource) -> None:
        self._sources = (BuiltinModules(), *sources)

    def read(self, module_id: str) -> bytes | None:
        for source in self._sources:
            data = source.read(module_id)
            if data is not None:
                return data
        return None


@dataclass(frozen=True, slots=True)
class IrBundle:
    main: str
    modules: Mapping[str, IrModule]  # every loaded module
    order: tuple[str, ...]  # dependencies before dependents

    # ---- name resolution ---------------------------------------------------

    def visible(self, module_id: str) -> tuple[str, ...]:
        """The module itself, ``core`` and its direct imports."""
        module = self.modules[module_id]
        names = [module_id, CORE_MODULE, *(i.module for i in module.info.imports)]
        return tuple(dict.fromkeys(n for n in names if n in self.modules))

    def curie(self, module_id: str, name: str) -> str:
        return f"{self.modules[module_id].info.prefix}:{name}"

    def resolve(self, module_id: str, ref: str, kind: TermKind) -> str | None:
        """Resolve a local name or a CURIE of a visible module to a CURIE."""
        match = CURIE_RE.match(ref)
        if match is None:
            owner, name = module_id, ref
        else:
            prefix, name = match.groups()
            owners = [m for m in self.visible(module_id) if self.modules[m].info.prefix == prefix]
            if not owners:
                return None
            owner = owners[0]
        return self.curie(owner, name) if name in _terms(self.modules[owner], kind) else None

    def suggest(
        self, module_id: str, ref: str, kinds: Iterable[TermKind], extra: Iterable[str] = ()
    ) -> list[str]:
        """Up to three names ``ref`` may have meant, written as ``module_id`` would write them.

        The same name in another visible module comes first (a missing or wrong
        prefix, such as ``supported_by`` for ``provenance:supported_by``), then
        close spellings.
        """
        forms = list(extra)
        for owner in self.visible(module_id):
            for kind in kinds:
                for name in _terms(self.modules[owner], kind):
                    forms.append(name if owner == module_id else self.curie(owner, name))
        match = CURIE_RE.match(ref)
        name = ref if match is None else match.group(2)
        same_name = [form for form in forms if form != ref and form.rpartition(":")[2] == name]
        close = difflib.get_close_matches(ref, forms, n=3)
        return list(dict.fromkeys([*same_name, *close]))[:3]

    def resolve_type(self, module_id: str, ref: str) -> tuple[str, str] | None:
        """An attribute or qualifier type: ``("base"|"datatype"|"enum", type)``."""
        if ref in BASE_TYPES:
            return "base", ref
        for kind in ("datatype", "enum"):
            curie = self.resolve(module_id, ref, kind)
            if curie is not None:
                return kind, curie
        return None

    def term(self, curie: str, kind: TermKind) -> tuple[str, object] | None:
        """``(module_id, term)`` for a CURIE."""
        prefix, _, name = curie.partition(":")
        for module_id, module in self.modules.items():
            if module.info.prefix == prefix:
                term = _terms(module, kind).get(name)
                return None if term is None else (module_id, term)
        return None

    def classes(self) -> Iterator[tuple[str, str, IrClass]]:
        """``(module_id, curie, class)`` in bundle order."""
        for module_id in self.order:
            for name, cls in self.modules[module_id].classes.items():
                yield module_id, self.curie(module_id, name), cls

    def parent(self, module_id: str, cls: IrClass) -> str | None:
        return None if cls.is_a is None else self.resolve(module_id, cls.is_a, "class")

    def ancestors(self, curie: str) -> tuple[str, ...]:
        """Nearest first; stops at an unknown parent or a cycle."""
        result: list[str] = []
        current: str | None = curie
        seen = {curie}
        while current is not None:
            found = self.term(current, "class")
            if found is None:
                break
            module_id, cls = found
            assert isinstance(cls, IrClass)
            parent = self.parent(module_id, cls)
            if parent is None or parent in seen:
                break
            result.append(parent)
            seen.add(parent)
            current = parent
        return tuple(result)

    def effective_attributes(self, curie: str) -> dict[str, tuple[str, str, IrAttribute]]:
        """Attribute name -> ``(declaring class CURIE, module_id, attribute)``; inherited first."""
        result: dict[str, tuple[str, str, IrAttribute]] = {}
        for owner in (*reversed(self.ancestors(curie)), curie):
            found = self.term(owner, "class")
            if found is None:
                continue
            module_id, cls = found
            assert isinstance(cls, IrClass)
            for name, attribute in cls.attributes.items():
                result.setdefault(name, (owner, module_id, attribute))
        return result


def _terms(module: IrModule, kind: TermKind) -> Mapping[str, object]:
    sections: dict[TermKind, Mapping[str, object]] = {
        "class": module.classes,
        "datatype": module.datatypes,
        "enum": module.enums,
        "lifecycle": module.lifecycles,
        "relation": module.relations,
        "marking": module.markings,
    }
    return sections[kind]


def load_bundle(main: str, source: ModuleSource, report: Report) -> IrBundle | None:
    """Load ``main`` and its imports. Returns ``None`` if any module failed to load."""
    modules: dict[str, IrModule] = {}
    order: list[str] = []
    failed = False

    def load(module_id: str) -> IrModule | None:
        nonlocal failed
        if module_id in modules:
            return modules[module_id]
        data = source.read(module_id)
        if data is None:
            return None
        module = parse_module(data, module_id, report)
        if module is None:
            failed = True
            return None
        modules[module_id] = module
        return module

    def visit(module_id: str, stack: tuple[str, ...]) -> None:
        nonlocal failed
        module = modules[module_id]
        imports = [i for i in module.info.imports]
        for item in imports:
            if item.module in stack or item.module == module_id:
                report.add(
                    "model.referential.import_cycle",
                    module_id,
                    item.path,
                    f"import cycle: {' -> '.join((*stack, module_id, item.module))}",
                )
                failed = True
                continue
            target = load(item.module)
            if target is None:
                if item.module not in modules and source.read(item.module) is None:
                    report.add(
                        "model.referential.unknown_import",
                        module_id,
                        item.path,
                        f"module {item.module!r} does not exist",
                        actual=item.module,
                    )
                failed = True
                continue
            if not item.range.matches(Version.parse(target.info.version)):
                report.add(
                    "model.referential.import_version_mismatch",
                    module_id,
                    item.path,
                    f"module {item.module!r} is {target.info.version}, "
                    f"which is outside {item.range.text!r}",
                    expected=item.range.text,
                    actual=target.info.version,
                )
                failed = True
            if item.module not in order:
                visit(item.module, (*stack, module_id))
        if module_id not in order:
            order.append(module_id)

    core = load(CORE_MODULE)
    root = load(main)
    if core is None or root is None:
        if root is None and not failed:
            report.add("model.referential.unknown_import", main, "", f"module {main!r} not found")
        return None
    visit(CORE_MODULE, ())
    visit(main, ())
    if failed:
        return None
    _check_prefixes(modules, order, report)
    return IrBundle(main=main, modules=modules, order=tuple(order))


def _check_prefixes(modules: Mapping[str, IrModule], order: list[str], report: Report) -> None:
    seen: dict[str, str] = {}
    for module_id in order:
        prefix = modules[module_id].info.prefix
        if prefix in seen:
            report.add(
                "model.structural.duplicate_term",
                module_id,
                "module.prefix",
                f"prefix {prefix!r} is already used by module {seen[prefix]!r}",
            )
        seen.setdefault(prefix, module_id)
