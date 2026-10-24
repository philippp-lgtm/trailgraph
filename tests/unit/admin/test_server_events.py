"""Events of the server entrypoint verify like app events (contracts B2.7, B5.12, B14.12).

A policy that grants the server is installed with ``policy apply``; a write by a
token principal and a denial of an ungranted token then pass ``audit verify``,
including the policy hash walk.
"""

import dataclasses
import json
from pathlib import Path
from typing import Any

from support.admin_context import admin_context
from support.roles import load_roles
from support.services import ServiceEnv

from graph_cli.catalog.identity.request import request_actor
from graph_cli.catalog.policy_source import FilePolicySource
from graph_cli.catalog.tokens import TokenRecord
from graph_cli.kernel.actor import ClientInfo, Entrypoint
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.findings import has_errors
from graph_cli.storage.files.audit_log import FileAuditLog
from graph_cli_admin.services_admin import audit, policy

POLICY = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {entrypoint: [app, server], principal: {id: "token:ci"}, graphs: "*", level: write}
  - {graphs: "*", level: write}
no_project: {"*": write}
"""
CLIENT = ClientInfo("graph-server", "0.1.0", Entrypoint.SERVER)


def test_server_writes_and_denials_verify(tmp_path: Path) -> None:
    svc = ServiceEnv("files", "inventory", tmp_path / "root")
    authored = tmp_path / "installation.yaml"
    authored.write_text(POLICY)
    policy.apply(admin_context(svc.root), file=authored, graph_ref=None, reason="grant the server")
    service = svc.make_service(policy=FilePolicySource(svc.root))
    token, _ = request_actor(
        {"agent": {"id": "agent-1"}}, token=TokenRecord("ci", "sha256:" + "0" * 64), client=CLIENT
    )
    cls = load_roles("inventory").term("lifecycle_type")
    result = service.apply(svc.request(*svc.factory.create(cls, "x"), actor=token))
    assert result.seq is not None

    stranger = dataclasses.replace(
        token, principal=dataclasses.replace(token.principal, id="token:other", name="other")
    )
    try:
        service.apply(svc.request(*svc.factory.create(cls, "y"), actor=stranger))
    except GraphCliError as exc:
        assert exc.code == "graph.not_found"  # invisible to it (B6.15), still audited
    else:
        raise AssertionError("an ungranted token wrote")

    log = FileAuditLog(svc.root)
    graph_events = [e for e in _events(log, f"graph:{svc.entry.id}") if "change" in e]
    assert graph_events[-1]["actor"]["client"]["entrypoint"] == "server"
    denials = [e for e in _events(log, "installation") if e["result"]["status"] == "denied"]
    assert denials[-1]["actor"]["principal"]["id"] == "token:other"

    verification = audit.verify(admin_context(svc.root))
    assert not has_errors(verification.findings), verification.findings


def _events(log: FileAuditLog, stream: str) -> list[dict[str, Any]]:
    return [json.loads(line.data) for line in log.read_lines(stream)]
