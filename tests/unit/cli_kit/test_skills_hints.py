"""Hints of the ``skills.*`` codes name the same target again (D15 Q7, B9.9)."""

from graph_cli.cli_kit.hints import hint_for


def test_force_hints_repeat_the_target_and_graphs() -> None:
    details = {"scope": "project", "dir": "/tmp/a project", "graphs": ["example_graph"]}
    for code in ("skills.not_owned", "skills.modified"):
        assert hint_for(code, details, program="graph") == (
            "graph skills install --scope project --dir '/tmp/a project'"
            " --graph example_graph --force"
        )
    assert hint_for("skills.modified", {"scope": "user"}, program="graph") == (
        "graph skills install --scope user --force"
    )


def test_check_failed_hint_refreshes_without_force() -> None:
    assert hint_for("skills.check_failed", {"scope": "user"}, program="graph") == (
        "graph skills install --scope user"
    )


def test_target_hint_only_for_a_missing_directory() -> None:
    details = {"scope": "project", "path": "/tmp/p/.claude", "cause": "missing"}
    assert hint_for("skills.target_invalid", details, program="graph") == "mkdir /tmp/p/.claude"
    assert hint_for("skills.target_invalid", {**details, "cause": "io"}, program="graph") is None


def test_source_missing_points_at_publish() -> None:
    assert hint_for("skills.source_missing", {"graph": "example_graph"}, program="graph") == (
        "graph-admin graph skill publish example_graph <dir>"
    )
    assert hint_for("skills.source_missing", {}, program="graph") is None
    assert hint_for("skills.source_invalid", {"graph": "example_graph"}, program="graph") is None


def test_outdated_hints_name_self_update() -> None:
    """B14.18: a refused client and a stale skill catch up with ``self update``."""
    details = {"scope": "project", "dir": "/tmp/a project"}
    assert hint_for("skills.outdated", details, program="graph") == (
        "graph self update --dir '/tmp/a project'"
    )
    assert hint_for("skills.outdated", {"scope": "user"}, program="graph") == "graph self update"
    outdated = {"program": "graph-admin", "client": "x", "server": "y"}
    assert hint_for("remote.client_outdated", outdated, program="graph-admin") == (
        "graph-admin self update"
    )
    command = "uv tool install --reinstall <url>"
    assert hint_for("update.unsupported_install", {"command": command}, program="graph") == (
        command
    )
    assert hint_for("update.unsupported_install", {"command": None}, program="graph") is None
