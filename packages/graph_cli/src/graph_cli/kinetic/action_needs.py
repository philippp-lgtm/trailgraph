"""What an action's ops need to be allowed, on concrete types (actions plan A6; write allow
lists plan W9).

The published ``needs`` of an action (``ActionEntry.needs``) name a write action and the
declared class of the entity it is needed for, which may be abstract or a superclass.
``action_needs`` derives, against the graph's pinned model and without any republish, what a
policy can actually decide:

- the **options** of a need: the declared class and its subclasses that are not abstract (a
  create, and an ``@local``, name exactly one type). The need is met when one option is
  allowed, because the entity the action meets decides which concrete type is written;
- the **fine terms** of each option: the attributes and relations the ops name below the
  type (plan W4), as ``(action, kind, term)``; an inline edge of a create is decided as
  ``write.create``. A transition without ``attr`` moves the lifecycle attribute of the
  option's type (none when it has zero or several), so the terms differ per option. An
  optional parameter's entry counts as written.

Ops are grouped by (action, declared class) with the union of their fine terms. That is
slightly conservative (two ops on the same declared class are met by one concrete type that
allows both) and accepted. A dynamic ``unlink ... all: true`` with ``direction: in`` removes
edges at their sources, so it needs the relation's domain classes, each expanded to its
concrete types; a relation without a domain can be owned by any class, so its need is
``write.unlink`` with every concrete class of the model as an option (declared type
``None``). A declared class with no concrete subtype keeps no options: the caller decides on
the declared type itself, as before.

Pure and total: no storage, no policy. Kinetic may import the ontology layer, never access
control; ``services.action_rights`` joins the options with the policy.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.changeset import OP_ACTIONS, op_attribute_names
from graph_cli.kinetic.action_entries import ActionEntry
from graph_cli.kinetic.action_format import is_dynamic_unlink, placeholder_name
from graph_cli.ontology_layer.model import CompiledModel

FineNeed = tuple[str, str, str]  # (write action, "attr" | "rel", attribute name or relation CURIE)


@dataclass(frozen=True, slots=True)
class NeedOptions:
    """One write action needed on one declared class; met when some option is allowed."""

    action: str
    type: str | None  # the declared class; None: any class (see the module docstring)
    options: tuple[tuple[str, tuple[FineNeed, ...]], ...]  # (concrete type, fine terms), sorted


@dataclass(frozen=True, slots=True)
class _Subject:
    """The entity an op names: the class it is declared as and the classes it can have."""

    declared: str | None
    classes: tuple[str, ...]


def action_needs(entry: ActionEntry, model: CompiledModel) -> tuple[NeedOptions, ...]:
    """The needs of ``entry`` against ``model``, sorted by (action, declared class)."""
    return _Derive(entry, model).run()


class _Derive:
    def __init__(self, entry: ActionEntry, model: CompiledModel) -> None:
        self.entry = entry
        self.model = model
        self.params = {p.name: p for p in entry.defn.params}
        self.locals: dict[str, str] = {}  # local name -> create type
        for op in entry.ops:
            local, kind = op.get("local"), op.get("type")
            if op.get("op") == "create" and isinstance(local, str) and isinstance(kind, str):
                self.locals[local] = kind
        # (action, declared class) -> concrete type -> fine terms
        self.groups: dict[tuple[str, str | None], dict[str, set[FineNeed]]] = {}

    def run(self) -> tuple[NeedOptions, ...]:
        for op in self.entry.ops:
            self.op(op)
        found = []
        for (action, declared), by_type in self.groups.items():
            options = tuple((name, tuple(sorted(terms))) for name, terms in sorted(by_type.items()))
            found.append(NeedOptions(action, declared, options))
        return tuple(sorted(found, key=lambda n: (n.action, n.type or "")))

    # Subjects

    def subject(self, value: JsonValue | None) -> _Subject | None:
        """The class a ``$parameter`` or an ``@local`` stands for (``None``: unknown)."""
        if not isinstance(value, str):
            return None
        if value.startswith("@"):
            kind = self.locals.get(value[1:])
            return (
                None if kind is None or kind not in self.model.classes else _Subject(kind, (kind,))
            )
        if value.startswith("$"):
            param = self.params.get(placeholder_name(value))
            declared = None if param is None else param.of_class
            if param is None or param.type != "ref" or declared not in self.model.classes:
                return None
            assert declared is not None
            return _Subject(declared, self.model.subtypes(declared))
        return None

    def subjects(self, value: JsonValue | None) -> list[_Subject]:
        """A list of references, or one parameter that takes a list."""
        found = [self.subject(item) for item in (value if isinstance(value, list) else [value])]
        return [s for s in found if s is not None]

    def need(
        self,
        action: str,
        subject: _Subject | None,
        fixed: Iterable[FineNeed] = (),
        per_type: Callable[[str], Iterable[FineNeed]] | None = None,
    ) -> None:
        """Add the need of ``action`` on ``subject`` with the ``fixed`` fine terms and those
        ``per_type`` gives for each concrete class."""
        if subject is None:
            return
        fixed = tuple(fixed)
        by_type = self.groups.setdefault((action, subject.declared), {})
        for name in subject.classes:
            if not self.model.classes[name].abstract:
                found = by_type.setdefault(name, set())
                found.update(fixed)
                if per_type is not None:
                    found.update(per_type(name))

    # Ops

    def op(self, op: Mapping[str, JsonValue]) -> None:
        kind = str(op.get("op"))
        action = OP_ACTIONS[kind]
        attributes = tuple((action, "attr", name) for name in op_attribute_names(op))
        if kind == "create":
            created = op.get("type")
            if isinstance(created, str) and created in self.model.classes:
                edges = op.get("edges")
                inline = tuple(
                    (action, "rel", str(e["rel"]))
                    for e in (edges if isinstance(edges, list) else [])
                    if isinstance(e, Mapping) and isinstance(e.get("rel"), str)
                )
                self.need(action, _Subject(created, (created,)), (*attributes, *inline))
        elif kind == "update":
            self.need(action, self.subject(op.get("id")), attributes)
        elif kind == "link":
            self.need(action, self.subject(op.get("id")), self.relation(action, op))
        elif kind == "unlink":
            self.unlink(action, op)
        elif kind == "transition":
            self.need(
                action,
                self.subject(op.get("id")),
                per_type=lambda name: self.lifecycle(op, name),
            )
        elif kind == "supersede":
            for subject in (self.subject(op.get("by")), *self.subjects(op.get("targets"))):
                self.need(action, subject)
        elif kind == "retract":
            for subject in (self.subject(op.get("id")), self.subject(op.get("by"))):
                self.need(action, subject)
        elif kind == "set_alias":
            self.need(action, self.subject(op.get("id")))

    def relation(self, action: str, op: Mapping[str, JsonValue]) -> tuple[FineNeed, ...]:
        rel = op.get("rel")
        return ((action, "rel", rel),) if isinstance(rel, str) else ()

    def lifecycle(self, op: Mapping[str, JsonValue], name: str) -> tuple[FineNeed, ...]:
        action = OP_ACTIONS["transition"]
        attr = op.get("attr")
        if not isinstance(attr, str):
            attr = self.model.lifecycle_attribute(name)
        return () if attr is None else ((action, "attr", attr),)

    def unlink(self, action: str, op: Mapping[str, JsonValue]) -> None:
        subject = self.subject(op.get("id"))
        terms = self.relation(action, op)
        rel = op.get("rel")
        if not (is_dynamic_unlink(op) and op.get("direction", "out") == "in"):
            self.need(action, subject, terms)
            return
        if not isinstance(rel, str) or rel not in self.model.relations:
            return
        domain = self.model.relations[rel].domain
        if not domain:  # any class can own the edges: every concrete class is an option
            every = _Subject(None, tuple(sorted(self.model.classes)))
            self.need(action, every, terms)
            return
        for name in domain:  # the edges are removed at their sources
            self.need(action, _Subject(name, self.model.subtypes(name)), terms)
