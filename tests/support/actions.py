"""The tracked action fixtures: a valid set and one broken file per rule.

They are written against the tracked example ontology ``examples/company/ontology/1.1.0``
(``example_model``): the fixture ontologies lack the employment lifecycle and the legal
module the actions need.
"""

import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from graph_cli.kernel.canonical import hash_value
from graph_cli.kernel.ports.catalog import KineticSlot, sources_hash
from graph_cli.kinetic.action_compile import compile_actions
from graph_cli.ontology_layer.model import CompiledModel
from graph_cli.services.actions import ActionResult, ActionService
from graph_cli_admin.services_admin import actions
from support.admin_context import admin_context
from support.fixtures import FIXTURES, example_model
from support.services import MemoryCatalog, ServiceEnv

ACTIONS = FIXTURES / "actions" / "company"
VALID = ACTIONS / "valid"
BROKEN_ACTIONS = ACTIONS / "broken"


def company_model() -> CompiledModel:
    return example_model("company", "1.1.0")


def read_dir(path: Path) -> dict[str, str]:
    """File name -> content of the action files in a folder."""
    return {p.name: p.read_text() for p in sorted(path.glob("*.yaml"))}


def valid_sources() -> dict[str, str]:
    return read_dir(VALID)


def edited(file_name: str, old: str, new: str) -> dict[str, str]:
    """The valid sources with one replacement in one file (``old`` must occur)."""
    sources = valid_sources()
    assert old in sources[file_name], old
    sources[file_name] = sources[file_name].replace(old, new)
    return sources


def publish_actions(svc: ServiceEnv, sources: Mapping[str, str]) -> None:
    """Publish ``sources`` as the action catalog of the service env's graph: through
    graph-admin on the files backend, as a slot of the memory catalog otherwise."""
    if isinstance(svc.catalog, MemoryCatalog):
        compiled, problems = compile_actions(sources, svc.model)
        assert [p.to_json() for p in problems] == []
        files = {name: text.encode() for name, text in sources.items()}
        svc.catalog.actions[svc.entry.id] = KineticSlot(
            sources_hash(files), hash_value(compiled), svc.entry.pin, compiled, {}, kind="actions"
        )
        return
    folder = svc.root.parent / "action_sources"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir()
    for name, text in sources.items():
        (folder / name).write_text(text)
    published = actions.publish(
        admin_context(svc.root), graph_ref=svc.name, source=folder, reason=None, dry_run=False
    )
    assert published.data["published"] is True


def action_service(svc: ServiceEnv, policy: Any = None) -> ActionService:
    """The action service over the env's catalog, with a reader and writer of the same policy."""
    reader = svc.make_reader(policy)
    writer = svc.make_service(policy) if policy is not None else svc.service
    return ActionService(catalog=svc.catalog, reader=reader, writer=writer)


def do(service: ActionService, svc: ServiceEnv, name: str, *params: str, **kw: Any) -> ActionResult:
    """``graph do name --param ...`` for the env's actor and graph."""
    pairs = [tuple(p.split("=", 1)) for p in params]
    kw.setdefault("dry_run", False)
    kw.setdefault("reason", None)
    kw.setdefault("idempotency_key", None)
    return service.do(svc.actor, svc.entry, name, pairs, **kw)  # type: ignore[arg-type]
