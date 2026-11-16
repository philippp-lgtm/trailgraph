"""Reused names of marked attributes across a release (field visibility plan F2, amended by R2).

Stored revisions keep the names they were written with; reads show old revisions under the
current names (``temporal_knowledge.name_map``), and a name the map does not change stays as
stored. So a release that declares an attribute without the markings a stored value had under
the same (mapped) name would show that value to callers the marking kept it from. Whether that
happens is decided by the name map, not by the bare name:

For every version V of the graph's pin chain, every concrete class C and every attribute ``a``
of C with markings M at V, a probe revision ``{type: C, attrs: {a}}`` written under V is mapped
step by step through the chain and the release being made (``NameMap.extended``), as reads map
old revisions. At each step the value is either inside a declaration of its new name or not
declared at all; it reaches a declaration **with an identity** when the diff compares the two
(the same declaring class, also renamed, the ``renamed_from`` of the attribute, or an explicit
``retype`` attribute map) and **without** one otherwise (a name dropped or removed and declared
again, a ``retype`` that carries the attribute to a same-named one of the new class, a parent's
attribute redeclared lower in the hierarchy). Where the value lands, the markings of that
declaration govern it from then on.

Only the last step, the release being made, is judged (the earlier ones were judged by their
own release):

- the value reaches a declaration whose markings do not include all of those that guarded it,
  without an identity: refused, ``ontology.marking_name_reused``;
- the same with an identity: a deliberate downgrade, visible in the diff as ``remove_marking``
  or as the retype map; warning ``admin.marking_removed`` (details ``via``: ``in_place``,
  ``rename``, ``retype_map``);
- a marked name of C returns unmarked on C in this release while every value stored under it
  maps away by rename: warning ``ontology.marking_name_returned`` (maintainer surfaces such as
  ``graph-admin view`` and transaction bodies still show the old names).

When markings are in play (the release uses them, a readable release of the chain does, or
the raw file of an unreadable one shows the ``/2`` schema, ``requires`` or ``markings``), a
release of the chain that cannot be read, or a pin history or migration file that cannot be
read, refuses the release (fail closed): it may have marked what the release reuses. When the
release and the chain show no trace of markings, nothing is judged: markings need a program of
0.23.0 or later, which always writes ``/2``, so such a chain cannot hide a marked name, and a
graph without markings releases as before. The check runs
in ``ontology release`` (next to the policy guard, skipped when an incomplete migration is
finished), ``graph push`` (through it), ``graph check --ontology`` and ``ontology diff
--graph``.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Literal

from graph_cli.kernel.canonical import CanonicalError, JsonValue, parse_json
from graph_cli.kernel.errors import GraphCliError, WarningRecord
from graph_cli.kernel.ids import EntityId, TermRef, TxId
from graph_cli.kernel.ports.catalog import GraphEntry
from graph_cli.kernel.records import Revision
from graph_cli.ontology_layer.model import AttributeDef, CompiledModel
from graph_cli.ontology_layer.vocabulary import COMPILED_SCHEMA_V2
from graph_cli.services.models import COMPILED_FILE, ModelCache
from graph_cli.services.names import NameMaps
from graph_cli.temporal_knowledge.name_map import Hop
from graph_cli_admin.services_admin.context import AdminContext

MAX_ITEMS: Final = 20
Via = Literal["in_place", "rename", "retype_map"]
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_NIL: Final = uuid.UUID(int=0)


@dataclass(frozen=True, slots=True, order=True)
class Reuse:
    """A value stored under ``attribute`` of ``type`` in ``version`` with ``markings`` that the
    release puts under ``target_attribute`` of ``target_type``, declared with
    ``target_markings``, which do not include them, without a diff identity."""

    type: str
    attribute: str
    version: str
    markings: tuple[str, ...]
    target_type: str
    target_attribute: str
    target_markings: tuple[str, ...]

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "version": self.version,
            "type": self.type,
            "attribute": self.attribute,
            "markings": list(self.markings),
            "to": {
                "type": self.target_type,
                "attribute": self.target_attribute,
                "markings": list(self.target_markings),
            },
        }


@dataclass(frozen=True, slots=True, order=True)
class Downgrade:
    """The declaration of ``attribute`` on ``type`` (its declaring class) loses ``removed``
    in the release, through ``via``; ``remaining`` are the markings it keeps."""

    type: str
    attribute: str
    via: str
    removed: tuple[str, ...]
    remaining: tuple[str, ...]


@dataclass(frozen=True, slots=True, order=True)
class Returned:
    """``attribute`` of ``type``, marked with ``markings`` in ``version``, is declared again
    with weaker markings; the values stored under it map to ``maps_to``."""

    type: str
    attribute: str
    version: str
    markings: tuple[str, ...]
    maps_to: str


@dataclass(frozen=True, slots=True)
class Judgement:
    reuses: tuple[Reuse, ...] = ()
    downgrades: tuple[Downgrade, ...] = ()
    returned: tuple[Returned, ...] = ()


@dataclass(slots=True)
class _Probe:
    type: str
    attribute: str
    required: frozenset[str]
    declared: bool = True


@dataclass(slots=True)
class _Findings:
    # by (type, attribute, target type, target attribute): the earliest version is reported
    reuses: dict[tuple[str, str, str, str], Reuse] = field(default_factory=dict)
    removed: dict[tuple[str, str, str], tuple[set[str], tuple[str, ...]]] = field(
        default_factory=dict
    )
    returned: dict[tuple[str, str], Returned] = field(default_factory=dict)


def judge(
    chain: Sequence[str], models: Sequence[CompiledModel], hops: Mapping[int, Hop]
) -> Judgement:
    """The reuses, downgrades and returned names of the last step of ``chain`` (the release
    being made). ``models[i]`` is the model of ``chain[i]``; ``hops[i]`` the hop from
    ``chain[i]`` to ``chain[i + 1]`` where names can change (a major step)."""
    last = len(chain) - 1
    found = _Findings()
    if last < 1:
        return Judgement()
    starts = {version: index for index, version in enumerate(chain[:last])}  # last place
    for start in sorted(starts.values()):
        for curie, cls in sorted(models[start].classes.items()):
            if cls.abstract:
                continue
            for name, attribute in sorted(cls.attributes.items()):
                if attribute.markings:
                    _follow(chain, models, hops, start, curie, name, attribute.markings, found)
    downgrades = tuple(
        Downgrade(t, a, via, tuple(sorted(removed)), remaining)
        for (t, a, via), (removed, remaining) in sorted(found.removed.items())
    )
    return Judgement(
        tuple(sorted(found.reuses.values())), downgrades, tuple(sorted(found.returned.values()))
    )


def _follow(
    chain: Sequence[str],
    models: Sequence[CompiledModel],
    hops: Mapping[int, Hop],
    start: int,
    curie: str,
    name: str,
    markings: tuple[str, ...],
    found: _Findings,
) -> None:
    last = len(chain) - 1
    probe = _Probe(curie, name, frozenset(markings))
    via: Via | None = None
    for index in range(start, last):
        via = _step(probe, models[index], models[index + 1], hops.get(index), chain[index])
        if index == last - 1:
            break
        target = _declared(models[index + 1], probe.type, probe.attribute)
        if target is not None:
            probe.required = frozenset(target.markings)  # past steps are as released
    target = _declared(models[last], probe.type, probe.attribute)
    if target is not None and not frozenset(target.markings) >= probe.required:
        if via is None:
            item = Reuse(
                curie, name, chain[start], markings, probe.type, probe.attribute, target.markings
            )
            found.reuses.setdefault((curie, name, probe.type, probe.attribute), item)
        else:
            key = (target.declared_in, probe.attribute, via)
            removed, _ = found.removed.setdefault(key, (set(), target.markings))
            removed.update(probe.required - frozenset(target.markings))
    if (probe.type, probe.attribute) != (curie, name):
        _returned(chain, models, start, curie, name, markings, probe, found)


def _returned(
    chain: Sequence[str],
    models: Sequence[CompiledModel],
    start: int,
    curie: str,
    name: str,
    markings: tuple[str, ...],
    probe: _Probe,
    found: _Findings,
) -> None:
    """The name the values left returns in this release with weaker markings."""
    again = _declared(models[-1], curie, name)
    if again is None or frozenset(again.markings) >= frozenset(markings):
        return
    if _declared(models[-2], curie, name) is not None:
        return  # not new in this release: warned when it came back
    item = Returned(curie, name, chain[start], markings, f"{probe.type}.{probe.attribute}")
    found.returned.setdefault((curie, name), item)


def _step(
    probe: _Probe, old: CompiledModel, new: CompiledModel, hop: Hop | None, version: str
) -> Via | None:
    """Move ``probe`` one pin step and say how it reached its new declaration (``None``:
    without an identity, or not declared)."""
    old_type, old_name = probe.type, probe.attribute
    if hop is not None and hop.mapping.changes_names:
        mapped = hop.mapping.apply(_revision(old_type, old_name, version))
        probe.type = str(mapped.type)
        probe.attribute = next(iter(mapped.attrs), old_name)
    was_declared = probe.declared and _declared(old, old_type, old_name) is not None
    target = _declared(new, probe.type, probe.attribute)
    probe.declared = target is not None
    if target is None or not was_declared:
        return None
    retype = None if hop is None else next((r for r in hop.retypes if r.from_ == old_type), None)
    if retype is not None and old_name in retype.attributes:
        return "retype_map"
    if probe.attribute != old_name:
        return "rename"
    before = _declared(old, old_type, old_name)
    assert before is not None
    owner = before.declared_in
    if hop is not None:
        owner = hop.renames.classes.get(owner, owner)  # the same declaration, moved
    return "in_place" if target.declared_in == owner else None


def _declared(model: CompiledModel, type_: str, name: str) -> AttributeDef | None:
    cls = model.classes.get(type_)
    return None if cls is None else cls.attributes.get(name)


def _revision(type_: str, name: str, version: str) -> Revision:
    return Revision(
        id=EntityId(_NIL),
        rev=1,
        prev_rev=None,
        type=TermRef(type_),
        record_status="active",
        attrs={name: True},
        edges=(),
        alias=None,
        valid_from=None,
        valid_to=None,
        created_at=_EPOCH,
        recorded_at=_EPOCH,
        tx_id=TxId(_NIL),
        tx_seq=0,
        ontology_version=version,
        content_hash="",
    )


# ---- the graph's chain ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Chain:
    """The pin chain with the release on top, the model of every version and the hops."""

    versions: tuple[str, ...]
    models: tuple[CompiledModel, ...]
    hops: Mapping[int, Hop]


@dataclass(frozen=True, slots=True)
class _Unreadable:
    version: str | None  # ``None``: the pin history or a migration file
    why: str


def traces_markings(raw: bytes) -> bool:
    """Whether the raw bytes of a compiled document that cannot be loaded show markings: the
    ``/2`` schema or a ``requires`` / ``markings`` key. Bytes that are no JSON object are
    searched for the schema string."""
    try:
        document = parse_json(raw)
    except CanonicalError:
        return COMPILED_SCHEMA_V2.encode() in raw
    if not isinstance(document, Mapping):
        return False
    return (
        document.get("schema") == COMPILED_SCHEMA_V2
        or "requires" in document
        or "markings" in document
    )


def _chain(ctx: AdminContext, entry: GraphEntry, hop: Hop) -> _Chain | _Unreadable | None:
    """The chain to judge; ``None`` when markings cannot be in play (the release and every
    release of the chain show no trace of them, so nothing can reuse a marked name: markings
    need a program of 0.23.0 or later, which writes ``/2``); ``_Unreadable`` when they may be
    and something cannot be read (fail closed). Each release file is loaded once; the name
    map, which reads the migration files, is built only when markings are in play."""
    models = ModelCache(ctx.catalog)
    maps = NameMaps(ctx.catalog, ctx.audit, models)
    versions, complete = maps.chain(entry)
    loaded: dict[str, CompiledModel] = {hop.old_version: hop.old, hop.new_version: hop.new}
    unreadable: _Unreadable | None = None
    traced = False
    for version in dict.fromkeys(versions):
        if version in loaded:
            continue
        try:
            loaded[version] = models.load_release(entry.id, version)
        except GraphCliError as exc:
            unreadable = unreadable or _Unreadable(version, exc.message)
            try:
                raw = ctx.catalog.read_release_file(entry.id, version, COMPILED_FILE)
            except GraphCliError:
                continue
            traced = traced or traces_markings(raw)
    marked = traced or any(m.features() for m in loaded.values())
    if not marked:
        return None
    if unreadable is not None:
        return unreadable
    name_map, complete_map = maps.build(entry)
    if not (complete and complete_map):
        return _Unreadable(None, "its pin history or a migration file cannot be read")
    extended = name_map.extended(hop)
    found = (*(loaded[v] for v in extended.chain[:-1]), hop.new)
    return _Chain(extended.chain, found, extended.hops)


def check(
    ctx: AdminContext, entry: GraphEntry, hop: Hop, bump: Mapping[str, JsonValue]
) -> tuple[WarningRecord, ...]:
    """The warnings of releasing ``hop.new`` on ``entry``; raises
    ``ontology.marking_name_reused`` for a reuse, or for something unreadable while markings
    are in play. A graph whose history and release show no markings is not judged."""
    found = _chain(ctx, entry, hop)
    if found is None:
        return ()
    if isinstance(found, _Unreadable):
        raise unreadable(entry, found.version, found.why, bump)
    judgement = judge(found.versions, found.models, found.hops)
    if judgement.reuses:
        raise refusal(entry, judgement.reuses, bump)
    return warnings(entry, judgement, hop.new_version)


def refusal(
    entry: GraphEntry, reuses: Sequence[Reuse], bump: Mapping[str, JsonValue]
) -> GraphCliError:
    first = reuses[0]
    missing = sorted(set(first.markings) - set(first.target_markings))
    return GraphCliError(
        "ontology.marking_name_reused",
        f"release {bump['to']} would show values of {first.type}.{first.attribute} that"
        f" {', '.join(missing)} guarded in {first.version} under"
        f" {first.target_type}.{first.target_attribute}, which is declared without that marking"
        f" ({len(reuses)} attribute{'s' if len(reuses) != 1 else ''} in graph {entry.name});"
        " stored values keep their names, so give the attribute the markings or another name",
        details={
            **bump,
            "graph": entry.name,
            "count": len(reuses),
            "reuses": [r.to_json() for r in reuses[:MAX_ITEMS]],
        },
    )


def unreadable(
    entry: GraphEntry, version: str | None, why: str, bump: Mapping[str, JsonValue]
) -> GraphCliError:
    what = f"release {version}" if version is not None else "the release history"
    return GraphCliError(
        "ontology.marking_name_reused",
        f"{what} of graph {entry.name} cannot be read ({why}), so whether release"
        f" {bump['to']} reuses the name of a marked attribute cannot be decided (fail closed)",
        details={**bump, "graph": entry.name, "unreadable": version},
    )


def warnings(entry: GraphEntry, judgement: Judgement, version: str) -> tuple[WarningRecord, ...]:
    found: list[WarningRecord] = []
    how = {
        "in_place": "in place",
        "rename": "with its rename",
        "retype_map": "through the retype's attribute map",
    }
    for item in judgement.downgrades:
        keeps = (
            f"; it keeps {', '.join(item.remaining)}" if item.remaining else "; it keeps no marking"
        )
        found.append(
            WarningRecord(
                "admin.marking_removed",
                f"release {version} removes {', '.join(item.removed)} from {item.type}."
                f"{item.attribute} ({how[item.via]}): its values, including history, become"
                f" readable for every caller that sees {item.type}{keeps}",
                {
                    "graph": entry.name,
                    "type": item.type,
                    "attribute": item.attribute,
                    "markings": list(item.removed),
                    "remaining": list(item.remaining),
                    "via": item.via,
                },
            )
        )
    for returned in judgement.returned:
        found.append(
            WarningRecord(
                "ontology.marking_name_returned",
                f"release {version} declares {returned.type}.{returned.attribute} again without"
                f" {', '.join(returned.markings)}, which marked it in {returned.version}; its"
                f" stored values read as {returned.maps_to}, but maintainer surfaces (graph-admin"
                " view, transaction bodies, usage logs) still show them under the old name",
                {
                    "graph": entry.name,
                    "type": returned.type,
                    "attribute": returned.attribute,
                    "version": returned.version,
                    "markings": list(returned.markings),
                    "maps_to": returned.maps_to,
                },
            )
        )
    return tuple(found)
