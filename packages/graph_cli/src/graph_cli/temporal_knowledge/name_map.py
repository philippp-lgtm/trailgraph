"""Old revisions under the names of the pinned model (migration plan M7).

A revision keeps the names of the ontology version it was written under
(``Revision.ontology_version``); a major release can rename classes, relations and attributes
(``renamed_from``) and its migration can retype classes and re-key values. Reads show old
revisions under the current names instead, so a reader never meets a name the model does not
know. The log is not touched: revisions are mapped when they are read.

``NameMap`` is built from the chain of pinned versions (``audit.pins``) and one ``Hop`` per
consecutive pair across which names change: the renames ``diff.derive_renames`` finds between
the two compiled models, and the steps of the installed ``migration.yaml`` that rename or
re-key (``migrate.MAPPED_STEPS``). A revision is mapped from the last place of its version in
the chain to its end, hop by hop, with the engine the migration itself uses
(``migrate.RevisionTransform``). ``convert``, ``fill`` and the ``drop`` steps are not mapped:
an old revision keeps what it stored.

Renames need a major release (``diff.classify`` rates every rename major, and a migration file
is refused on a minor or patch release), so only hops between majors are built, and a revision
of the pinned major is never mapped. A version that is not in the chain (a stream that does
not reach back to it) maps to itself, and so does a revision behind a hop whose release files
could not be read; ``unmapped`` tells both apart from a revision whose names are known to fit
the pin (field visibility plan R3: a reader limited to some attributes must not trust its
names).

``hints`` answers the other side: the current name of an old one, for the "renamed to" hint of
``term.unknown`` (old names are never accepted on input).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from typing import Final, Literal

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.records import Revision
from graph_cli.ontology_layer.diff import Renames
from graph_cli.ontology_layer.migration import (
    MapQualifierValues,
    MapValues,
    MigrationDoc,
    RenameQualifier,
    Retype,
    class_moves,
)
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.ontology_layer.semver import Version
from graph_cli.temporal_knowledge.migrate import (
    MAPPED_STEPS,
    RevisionTransform,
    with_content_hash,
)

TermKind = Literal["class", "relation", "attribute"]
# The ``kind`` of a ``term.unknown`` (and of the ``action.unknown_*`` codes) by what it names.
KINDS: Final[Mapping[str, TermKind]] = {
    "type": "class",
    "class": "class",
    "label": "class",
    "relation": "relation",
    "relationship type": "relation",
    "field": "attribute",
    "attribute": "attribute",
    "text field": "attribute",
    "property": "attribute",
}


def other_major(version: str, pin: str) -> bool:
    """Whether ``version`` and ``pin`` differ in their major (a version that does not parse
    counts as different)."""
    if version == pin:
        return False
    try:
        return Version.parse(version).major != Version.parse(pin).major
    except ValueError:
        return True


@dataclass(frozen=True, slots=True)
class Hop:
    """The pin switch ``old_version`` -> ``new_version`` and how names change across it.

    ``migration`` is the installed migration file of ``new_version`` when its ``from`` is
    ``old_version``; ``language`` is the graph language (for ``applied`` on a ``convert``)."""

    old_version: str
    new_version: str
    old: CompiledModel
    new: CompiledModel
    renames: Renames
    migration: MigrationDoc | None
    language: str | None = None
    _cache: dict[str, RevisionTransform] = field(default_factory=dict, compare=False, repr=False)

    @property
    def retypes(self) -> tuple[Retype, ...]:
        steps = () if self.migration is None else self.migration.steps
        return tuple(s for s in steps if isinstance(s, Retype))

    @property
    def mapping(self) -> RevisionTransform:
        """The renames and the mapped steps: what an old revision goes through."""
        found = self._cache.get("mapping")
        if found is None:
            steps = () if self.migration is None else self.migration.steps
            mapped = [s for s in steps if isinstance(s, MAPPED_STEPS)]
            found = self._cache["mapping"] = RevisionTransform(
                self.old, self.new, self.renames, mapped
            )
        return found

    @property
    def migrating(self) -> RevisionTransform:
        """The renames and every step: what the migration tx did to a current revision."""
        found = self._cache.get("migrating")
        if found is None:
            steps = () if self.migration is None else self.migration.steps
            found = self._cache["migrating"] = RevisionTransform(
                self.old, self.new, self.renames, steps, language=self.language
            )
        return found

    def class_map(self) -> dict[str, str]:
        """Old class CURIE to new: renames and retypes."""
        return class_moves(self.renames, self.retypes)


@dataclass(frozen=True, slots=True)
class RenameStep:
    """One step of an old name to its current one: ``to`` since ``version``; ``how`` is
    ``renamed`` or ``retyped``; ``owner`` the class of an attribute."""

    to: str
    version: str
    how: str = "renamed"
    owner: str | None = None

    def to_json(self) -> dict[str, JsonValue]:
        data: dict[str, JsonValue] = {"to": self.to, "in": self.version}
        if self.how != "renamed":
            data["how"] = self.how
        if self.owner is not None:
            data["class"] = self.owner
        return data


@dataclass(frozen=True, slots=True)
class RenameHint:
    """An old name and the steps to its current name (the last ``to``)."""

    term: str
    steps: tuple[RenameStep, ...]

    @property
    def current(self) -> str:
        return self.steps[-1].to

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "from": self.term,
            "to": self.current,
            "steps": [step.to_json() for step in self.steps],
        }

    def text(self) -> str:
        parts = []
        for index, step in enumerate(self.steps):
            what = f"{step.how} to {step.to}" if index == 0 else f"then to {step.to}"
            if step.owner is not None:
                what += f" (on {step.owner})"
            parts.append(f"{what} in {step.version}")
        return f"{self.term} was " + ", ".join(parts)


class NameMap:
    """The chain of pinned versions (the last is the pin) and the hops between them that
    change names, keyed by the index of their old version in ``chain``. ``unreadable`` holds
    the indices of the major switches whose hop could not be built (a release file or the
    migration file did not read)."""

    def __init__(
        self, chain: Sequence[str], hops: Mapping[int, Hop], unreadable: Iterable[int] = ()
    ) -> None:
        self.chain = tuple(chain)
        self.hops = dict(hops)
        self.unreadable = frozenset(unreadable)

    @property
    def pin(self) -> str | None:
        return self.chain[-1] if self.chain else None

    @property
    def empty(self) -> bool:
        return not self.hops

    @cached_property
    def _last_index(self) -> dict[str, int]:
        return {version: index for index, version in enumerate(self.chain)}

    def _hops_from(self, version: str) -> list[Hop]:
        """The hops a revision of ``version`` is mapped through (only those that map)."""
        start = self._last_index.get(version)
        if start is None:
            return []
        return [
            self.hops[i]
            for i in sorted(self.hops)
            if i >= start and self.hops[i].mapping.changes_names
        ]

    def extended(self, hop: Hop) -> NameMap:
        """This map with one more pin switch at its end (a release that is being made)."""
        chain = (
            self.chain
            if self.chain and self.chain[-1] == hop.old_version
            else (
                *self.chain,
                hop.old_version,
            )
        )
        return NameMap(
            (*chain, hop.new_version), {**self.hops, len(chain) - 1: hop}, self.unreadable
        )

    def changes(
        self, version: str, known: Callable[[TermKind, RenameHint], bool] | None = None
    ) -> bool:
        """Whether revisions written under ``version`` can have names that changed since.
        With ``known`` (a test of the current name a caller may see, as for ``hints``), only
        changes of terms that test keeps count: a rename, retype, attribute map, value map or
        qualifier step of a hop the revision is mapped through, followed to its current name
        (field visibility plan R4: the ``--as-of`` warning of a limited reader)."""
        if known is None:
            return bool(self._hops_from(version))
        return any(known(kind, hint) for kind, hint in self._changed(version))

    def _changed(self, version: str) -> Iterator[tuple[TermKind, RenameHint]]:
        start = self._last_index.get(version)
        if start is None:
            return
        ordered = sorted(self.hops)
        for position, index in enumerate(ordered):
            hop = self.hops[index]
            if index < start or not hop.mapping.changes_names:
                continue
            later = [self.hops[i] for i in ordered[position + 1 :]]
            for kind, source, first in _hop_terms(hop):
                steps = [first]
                self._follow(kind, steps, later)
                yield kind, RenameHint(source, tuple(steps))

    def unmapped(self, version: str) -> bool:
        """Whether revisions written under ``version`` reach the pin under names the map
        cannot vouch for: ``version`` is of another major than the pin and either not in the
        chain or behind an unreadable hop. ``map`` hands such a revision out as stored."""
        pin = self.pin
        if pin is None or not other_major(version, pin):
            return False
        start = self._last_index.get(version)
        return start is None or any(i >= start for i in self.unreadable)

    def map(self, revision: Revision) -> Revision:
        """``revision`` under the current names with its content hash recomputed; the same
        object when no name changes. Identity fields and ``ontology_version`` are kept."""
        mapped = revision
        for hop in self._hops_from(revision.ontology_version):
            mapped = hop.mapping.apply(mapped)
        return revision if mapped is revision else with_content_hash(mapped)

    def own_class(self, revision: Revision) -> str:
        """The class ``revision`` was stored under, under the current names: followed through
        the class renames of the hops it is mapped through, never through a ``retype`` (the
        class an entity had before a retype is another class than the one it has now, and
        stays the name the stored class is judged by). ``map`` gives the retyped name."""
        name: str = revision.type
        for hop in self._hops_from(revision.ontology_version):
            name = hop.renames.classes.get(name, name)
        return name

    def hop(self, old_version: str, new_version: str) -> Hop | None:
        """The hop of the pin switch ``old_version`` -> ``new_version`` (the latest one)."""
        found = [
            h
            for _, h in sorted(self.hops.items())
            if (h.old_version, h.new_version) == (old_version, new_version)
        ]
        return found[-1] if found else None

    # ---- hints -----------------------------------------------------------------------

    def hints(
        self, kind: TermKind, term: str, *, known: Callable[[RenameHint], bool] | None = None
    ) -> tuple[RenameHint, ...]:
        """The current names of the old ``term`` (a CURIE, or a local name for classes and
        relations; a name for attributes), one per old term it can mean. ``known`` keeps only
        hints whose current name the caller may see."""
        ordered = [self.hops[i] for i in sorted(self.hops)]
        found: list[RenameHint] = []
        for index, hop in enumerate(ordered):
            for source, step in self._sources(kind, term, hop):
                steps = [step]
                self._follow(kind, steps, ordered[index + 1 :])
                hint = RenameHint(source, tuple(steps))
                if hint not in found and (known is None or known(hint)):
                    found.append(hint)
        return tuple(found)

    @staticmethod
    def _sources(kind: TermKind, term: str, hop: Hop) -> list[tuple[str, RenameStep]]:
        version = hop.new_version
        if kind == "attribute":
            found = [
                (old, RenameStep(new, version, owner=owner))
                for (owner, old), new in sorted(hop.renames.attributes.items())
                if old == term
            ]
            found += [
                (old, RenameStep(new, version, owner=r.to))
                for r in hop.retypes
                for old, new in sorted(r.attributes.items())
                if old == term
            ]
            return found
        retyped = {r.from_: r.to for r in hop.retypes} if kind == "class" else {}
        renamed = dict(hop.renames.classes if kind == "class" else hop.renames.relations)
        found = []
        for old, new in sorted({**renamed, **retyped}.items()):
            if term in (old, old.partition(":")[2]):
                how = "retyped" if old in retyped and old not in renamed else "renamed"
                found.append((old, RenameStep(new, version, how)))
        return found

    @staticmethod
    def _follow(kind: TermKind, steps: list[RenameStep], later: Sequence[Hop]) -> None:
        """Extend ``steps`` through the renames of the later hops."""
        for hop in later:
            last = steps[-1]
            if kind == "attribute":
                owner = hop.class_map().get(last.owner or "", last.owner)
                new = hop.renames.attributes.get((owner or "", last.to))
                if new is not None:
                    steps.append(RenameStep(new, hop.new_version, owner=owner))
                continue
            names = hop.class_map() if kind == "class" else dict(hop.renames.relations)
            if last.to in names:
                how = "renamed" if last.to in hop.renames.classes or kind != "class" else "retyped"
                steps.append(RenameStep(names[last.to], hop.new_version, how))


def _hop_terms(hop: Hop) -> Iterator[tuple[TermKind, str, RenameStep]]:
    """``(kind, old name, step)`` of every term whose name or values change across ``hop``
    for a revision mapped through it (a value map or a qualifier step keeps the name)."""
    version = hop.new_version
    for old, new in sorted(hop.renames.classes.items()):
        yield "class", old, RenameStep(new, version)
    for old, new in sorted(hop.renames.relations.items()):
        yield "relation", old, RenameStep(new, version)
    for (owner, old), new in sorted(hop.renames.attributes.items()):
        yield "attribute", old, RenameStep(new, version, owner=owner)
    for step in () if hop.migration is None else hop.migration.steps:
        if isinstance(step, Retype):
            yield "class", step.from_, RenameStep(step.to, version, "retyped")
            for old, new in sorted(step.attributes.items()):
                yield "attribute", old, RenameStep(new, version, owner=step.to)
        elif isinstance(step, MapValues):
            yield "attribute", step.attribute, RenameStep(step.attribute, version, owner=step.type)
        elif isinstance(step, MapQualifierValues | RenameQualifier):
            yield "relation", step.relation, RenameStep(step.relation, version)
