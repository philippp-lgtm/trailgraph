"""``files extract`` is authorized as ``maint.reindex`` through the admin context, so a denial
is an audited ``denied`` event of the installation stream and nothing is read or written (file
metadata plan M12, step 6)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from support.admin_context import admin_context
from support.file_metadata import Graph
from support.services import ServiceEnv

from graph_cli.access_control.compiler import compile_policy
from graph_cli.access_control.policy import policy_set
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ports.audit import INSTALLATION_STREAM
from graph_cli.kernel.ports.policy import PolicySet
from graph_cli_admin.services_admin import file_extract

DENY = """\
schema: graph_cli.policy/1
id: installation
grants:
  - {entrypoint: app, graphs: "*", level: write}
deny:
  - {id: no_reindex, action: ["maint.reindex"]}
no_project: {"*": write}
"""


class _Policy:
    def __init__(self, policy: PolicySet) -> None:
        self.policy = policy

    def current(self, graph: object) -> PolicySet:
        return self.policy

    def refresh(self) -> None:
        pass


def test_a_denied_extraction_is_audited_and_writes_nothing(tmp_path: Path) -> None:
    env = ServiceEnv("files", "notes_and_sources", tmp_path / "root", version="1.3.0")
    Graph(env).add("one_page.pdf")
    document = compile_policy(
        DENY.encode(), file="installation.yaml", graphs={}, graph=None, roles=frozenset()
    ).document
    ctx = dataclasses.replace(admin_context(env.root), policy=_Policy(policy_set(document)))
    head = env.head_seq()
    with pytest.raises(GraphCliError) as caught:
        file_extract.extract(
            ctx, graph_ref=env.name, files=(), extractors=(), retry_failed=False, dry_run=False
        )
    assert caught.value.code == "access.denied"
    assert env.head_seq() == head
    lines = [line.data for line in ctx.audit.read_lines(INSTALLATION_STREAM)]
    denied = [line for line in lines if b'"denied"' in line and b"files extract" in line]
    assert len(denied) == 1, "the denial is one event of the installation stream"
