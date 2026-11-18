"""What a release that renames terms does to the policy and the published catalogs (migration
plan M8): the deny rules that would stop applying, and where the old names are used.

A deny rule (or ``types: {T: none}``, which compiles to one) names a type or term by its
resource. After a release that renames or retypes that term the rule names a term that no
longer exists, so it applies to nothing: it fails open. ``hazards`` finds those rules, and
``ontology release`` refuses them (``admin.policy_renamed_term``) unless the installed policy
names the new term too (a policy may name terms that do not exist yet). Allow rules (``reads``,
``writes``) fail closed on a rename and keep only warning.

A rule is matched through its resource patterns, as the authorizer matches them: an exact
resource of a type, of an attribute or relation of a type, or a glob (``prefix:*``). A pattern
that matches the old term's resource is a hazard when no deny rule matches the new term's
resource for at least the same actions. Whether the principals of the two rules agree is the
maintainer's to judge: ``types: none`` entries always agree.

``references`` is the picture ``ontology diff --graph`` shows: per renamed term, the functions
and actions whose stored form names the old CURIE (``explicit`` when their own source does, so
the file needs a fix; otherwise the name is bare and the catalog is recompiled with the
release) and the policy rules that name it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from graph_cli.access_control.documents import glob
from graph_cli.access_control.terms import RuleEntry
from graph_cli.kernel.canonical import JsonValue, canonical_bytes
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ids import GraphId
from graph_cli.kernel.json_access import as_object, req_list, req_str
from graph_cli.kernel.ports.catalog import KineticSlot
from graph_cli.kinetic.format import FILE_SUFFIX
from graph_cli.ontology_layer.diff import Renames
from graph_cli.ontology_layer.migration import Retype, class_moves
from graph_cli.ontology_layer.model import CompiledModel

MAX_HAZARDS: Final = 20
Kind = Literal["class", "relation", "attribute"]


@dataclass(frozen=True, slots=True, order=True)
class Probe:
    """A rule resource that names a renamed term: ``source`` and ``target`` are the old and the
    new term (a class or relation CURIE, ``<class>.<attribute>``) and ``target_resource`` the
    resource a rule would need to name for the new term. ``covered``: a deny rule matches
    ``target_resource`` for the actions of this rule's entry (only judged for a deny entry)."""

    entry: RuleEntry
    kind: Kind
    source: str
    target: str
    target_resource: str
    covered: bool

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "layer": self.entry.layer,
            "rule": self.entry.rule,
            "effect": self.entry.effect,
            "resource": self.entry.resource,
            "kind": self.kind,
            "from": self.source,
            "to": self.target,
            "target_resource": self.target_resource,
            "covered": self.covered if self.entry.effect == "deny" else None,
        }


def probes(
    entries: Sequence[RuleEntry],
    graph: GraphId | str,
    renames: Renames,
    retypes: Sequence[Retype],
    new: CompiledModel,
) -> tuple[Probe, ...]:
    """The rule resources of ``entries`` that name a term the release renames or retypes, each
    with the resource of the new term and whether a deny rule covers it."""
    prefix = f"graph:{graph}/type:"
    class_map = class_moves(renames, retypes)
    attribute_maps = {r.from_: r.attributes for r in retypes}
    denies = [e for e in entries if e.effect == "deny"]
    found: list[Probe] = []

    def add(entry: RuleEntry, kind: Kind, source: str, target: str, resource: str) -> None:
        covered = any(
            glob(q.resource, resource)
            and all(any(glob(p, a) for p in q.actions) for a in entry.actions)
            for q in denies
        )
        found.append(Probe(entry, kind, source, target, resource, covered))

    for entry in entries:
        pattern = entry.resource
        rest = pattern[len(prefix) :] if pattern.startswith(prefix) else None
        if pattern.endswith("*") and (rest is None or "/" not in rest):
            # A glob of graphs or types: it hides a renamed type only while it still matches the
            # new name (``prefix:*`` of a type that moves to another prefix does not).
            for old, target in sorted(class_map.items()):
                if glob(pattern, f"{prefix}{old}") and not glob(pattern, f"{prefix}{target}"):
                    add(entry, "class", old, target, f"{prefix}{target}")
            continue
        if rest is None:
            continue
        curie, _, below = rest.partition("/")
        moved = class_map.get(curie, curie)
        if not below:
            if moved != curie:
                add(entry, "class", curie, moved, f"{prefix}{moved}")
            continue
        kind, _, name = below.partition(":")
        if kind == "attr":
            renamed = _attribute(new, renames, attribute_maps.get(curie), moved, name)
            resource = f"{prefix}{moved}/attr:{renamed}"
            if renamed != name:
                add(entry, "attribute", f"{curie}.{name}", f"{moved}.{renamed}", resource)
            elif moved != curie:  # only the class moved: its terms follow it
                add(entry, "class", curie, moved, resource)
        elif kind == "rel":
            renamed = renames.relations.get(name, name)
            resource = f"{prefix}{moved}/rel:{renamed}"
            if renamed != name:
                add(entry, "relation", name, renamed, resource)
            elif moved != curie:
                add(entry, "class", curie, moved, resource)
    return tuple(sorted(set(found)))


def _attribute(
    new: CompiledModel,
    renames: Renames,
    retyped: Mapping[str, str] | None,
    curie: str,
    name: str,
) -> str:
    """The new name of attribute ``name`` of the old class now called ``curie``: a ``retype``'s
    attribute map, or the ``renamed_from`` of an attribute the class has now (declared on it
    or inherited)."""
    if name == "*":
        return name
    if retyped is not None and name in retyped:
        return retyped[name]
    cls = new.classes.get(curie)
    if cls is None or name in cls.attributes:
        return name
    for found, attribute in cls.attributes.items():
        if (
            attribute.renamed_from == name
            and renames.attributes.get((attribute.declared_in, name)) == found
        ):
            return found
    return name


def hazards(
    entries: Sequence[RuleEntry],
    graph: GraphId | str,
    renames: Renames,
    retypes: Sequence[Retype],
    new: CompiledModel,
) -> tuple[Probe, ...]:
    """The deny rules that would fail open: they name a renamed or retyped term and no deny
    rule names the new one."""
    return tuple(
        p
        for p in probes(entries, graph, renames, retypes, new)
        if p.entry.effect == "deny" and not p.covered
    )


def refusal(found: Sequence[Probe], bump: Mapping[str, JsonValue]) -> GraphCliError:
    """``admin.policy_renamed_term``: the release would leave these deny rules naming terms
    that no longer exist."""
    first = found[0]
    rules = sorted({p.entry.rule for p in found})
    shown = ", ".join(rules[:5]) + (", ..." if len(rules) > 5 else "")
    return GraphCliError(
        "admin.policy_renamed_term",
        f"{len(rules)} deny rules of the installed policy would stop applying under"
        f" {bump['to']}: they name {first.source}, which the release renames to {first.target},"
        f" and no deny rule names the new term ({shown}); apply a policy that names both the"
        " old and the new term, release, then remove the old names",
        details={
            **bump,
            "count": len(found),
            "rules": [p.to_json() for p in found[:MAX_HAZARDS]],
        },
    )


def warning(found: Sequence[Probe], version: str) -> tuple[WarningRecord, ...]:
    """``admin.policy_renamed_term_affected``, what ``ontology diff --graph`` shows before a
    release refuses with ``admin.policy_renamed_term``."""
    if not found:
        return ()
    rules = sorted({p.entry.rule for p in found})
    shown = ", ".join(rules[:5]) + (", ..." if len(rules) > 5 else "")
    return (
        WarningRecord(
            "admin.policy_renamed_term_affected",
            f"{len(rules)} deny rules of the installed policy name terms that {version} renames"
            f" and do not name the new ones ({shown}); the release is refused until the policy"
            " names both",
            {
                "to": version,
                "count": len(found),
                "rules": [p.to_json() for p in found[:MAX_HAZARDS]],
            },
        ),
    )


def references(
    entries: Sequence[RuleEntry],
    graph: GraphId | str,
    renames: Renames,
    new: CompiledModel,
    *,
    functions: KineticSlot | None,
    actions: KineticSlot | None,
    views: KineticSlot | None = None,
) -> list[JsonValue]:
    """Per renamed term, where the graph's policy, functions, actions and views name the old
    one; terms nobody names are left out. Classes and relations are found in the catalogs
    (their CURIEs appear in the stored form); attributes are bare names there and show through
    the ``admin.functions_affected`` / ``admin.actions_affected`` / ``admin.views_affected``
    warnings instead. The ``views`` key is there only when the graph has published views."""
    items: dict[tuple[str, str, str], dict[str, list[JsonValue]]] = {}

    def item(kind: str, source: str, target: str) -> dict[str, list[JsonValue]]:
        found: dict[str, list[JsonValue]] = {"functions": [], "actions": [], "policy": []}
        if views is not None:
            found["views"] = []
        return items.setdefault((kind, source, target), found)

    for old, target in renames.classes.items():
        _catalog_references(item("class", old, target), old, functions, actions, views)
    for old, target in renames.relations.items():
        _catalog_references(item("relation", old, target), old, functions, actions, views)
    for probe in probes(entries, graph, renames, (), new):
        item(probe.kind, probe.source, probe.target)["policy"].append(
            {
                "layer": probe.entry.layer,
                "rule": probe.entry.rule,
                "effect": probe.entry.effect,
                "resource": probe.entry.resource,
                "covered": probe.covered if probe.entry.effect == "deny" else None,
            }
        )
    listed: list[JsonValue] = []
    for (kind, source, target), found in sorted(items.items()):
        if not any(found.values()):
            continue
        listed.append({"kind": kind, "from": source, "to": target, **found})
    return listed


def _catalog_references(
    found: dict[str, list[JsonValue]],
    curie: str,
    functions: KineticSlot | None,
    actions: KineticSlot | None,
    views: KineticSlot | None,
) -> None:
    for key, slot in (("functions", functions), ("actions", actions), ("views", views)):
        if slot is not None:
            found[key].extend(_naming(slot, curie))


def _naming(slot: KineticSlot, curie: str) -> list[JsonValue]:
    """The entries of a published catalog whose stored form names ``curie``; ``explicit`` when
    the entry's own source file does as well."""
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(curie)}(?![A-Za-z0-9_])")
    named: list[JsonValue] = []
    key = slot.spec.list_key
    for entry in req_list(slot.compiled, key):
        item = as_object(entry, key)
        if not pattern.search(canonical_bytes(item).decode("utf-8")):
            continue
        name = req_str(item, "name")
        source = slot.files.get(f"{name}{FILE_SUFFIX}", b"").decode("utf-8", errors="replace")
        named.append({"name": name, "explicit": pattern.search(source) is not None})
    return named
