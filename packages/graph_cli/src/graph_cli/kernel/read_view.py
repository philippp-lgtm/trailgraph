"""What one caller may read of one graph: the read view key (field visibility plan F4, R3).

A ``ReadView`` has two halves, each ``None`` for "no limit":

- ``types``: the concrete types the caller sees (type read limits, stage 3).
- ``attributes``: per concrete type of the pinned model, sorted by type, the names of the
  attributes the caller sees there (an allow list; markings, stage 4). An entity whose type
  has no entry keeps its id, type and edges and loses every attribute (fail closed).

Both ``None`` is no filter at all: every answer is byte-identical to a read without a view.
The view is hashable, so the caches of the read services key on it, and its ``digest`` goes
into the claim of an installed graph skill: without an attribute half it equals the stage 3
digest ``hash_value(sorted(types))``, so claims of type-limited readers stay fresh.

``limited`` (either half set) and ``types_limited`` are two different questions: a surface
that hides types asks the second, one that must treat any limited reader apart the first.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from graph_cli.kernel.canonical import JsonValue, hash_value

# Per concrete type, sorted by type: the attribute names a caller sees there.
AttributeMask = tuple[tuple[str, frozenset[str]], ...]


@dataclass(frozen=True)
class ReadView:
    types: frozenset[str] | None = None
    attributes: AttributeMask | None = None
    _keep: Mapping[str, frozenset[str]] = field(
        init=False, repr=False, compare=False, hash=False, default_factory=dict
    )

    def __post_init__(self) -> None:
        if self.attributes is not None:
            ordered = tuple(sorted(self.attributes, key=lambda pair: pair[0]))
            keep = dict(ordered)
            if len(keep) != len(ordered):
                raise ValueError("a read view names a type twice in its attribute half")
            object.__setattr__(self, "attributes", ordered)
            object.__setattr__(self, "_keep", keep)

    @classmethod
    def of(
        cls,
        types: Iterable[str] | None = None,
        attributes: Mapping[str, Iterable[str]] | None = None,
    ) -> ReadView:
        """A view from plain collections (``None`` for a half without a limit)."""
        return cls(
            None if types is None else frozenset(types),
            None
            if attributes is None
            else tuple((t, frozenset(names)) for t, names in attributes.items()),
        )

    @property
    def limited(self) -> bool:
        """Whether the caller reads anything less than the whole graph (either half set)."""
        return self.types is not None or self.attributes is not None

    @property
    def types_limited(self) -> bool:
        """Whether some types are hidden from the caller (the stage 3 half)."""
        return self.types is not None

    @property
    def attributes_limited(self) -> bool:
        """Whether some attributes are hidden from the caller (the markings half)."""
        return self.attributes is not None

    def shows_type(self, type_: str) -> bool:
        """Whether entities of ``type_`` are visible (by type; attributes aside)."""
        return self.types is None or type_ in self.types

    def keeps(self, type_: str) -> frozenset[str] | None:
        """The attribute names an entity of ``type_`` keeps, ``None`` for all of them. A type
        without an entry in a set attribute half keeps none."""
        if self.attributes is None:
            return None
        return self._keep.get(type_, frozenset())

    def types_only(self) -> ReadView:
        """This view without its attribute half."""
        return self if self.attributes is None else ReadView(self.types)

    @property
    def digest(self) -> str | None:
        """A digest of what the caller sees, ``None`` without any limit. Without an attribute
        half it is the stage 3 digest of the visible types."""
        if self.attributes is None:
            return None if self.types is None else hash_value(sorted(self.types))
        attributes: dict[str, JsonValue] = {t: sorted(names) for t, names in self.attributes}
        types: JsonValue = None if self.types is None else sorted(self.types)
        return hash_value({"attributes": attributes, "types": types})


UNLIMITED = ReadView()
