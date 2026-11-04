"""``graph-admin init``."""

from __future__ import annotations

from typing import Final, get_args

from graph_cli.catalog.resolve import RootSource
from graph_cli.cli_kit.envelope import Result
from graph_cli.cli_kit.output_schemas import BOOLEAN, STRING, enum, nullable, obj
from graph_cli.cli_kit.runner import CommandState
from graph_cli.cli_kit.spec import CommandSpec, Example, ParamSpec
from graph_cli_admin.maintenance_cli.commands.common import DATA_ROOT, ROOT_ERRORS, admin_context
from graph_cli_admin.services_admin.init import init


def init_command(state: CommandState, device_name: str | None, data_root: str | None) -> Result:
    ctx = admin_context(state, data_root)
    return Result(data=init(ctx, device_name=device_name))


OUTPUT: Final = obj(  # services_admin.init.init
    {
        "data_root": STRING,
        "root_source": enum(*get_args(RootSource)),
        "config_file": STRING,
        "device_id": nullable(STRING),
        "kek_file": nullable(STRING),
        "created": obj({"data_root": BOOLEAN, "config": BOOLEAN, "kek": BOOLEAN}),
    }
)


SPECS: Final = (
    CommandSpec(
        path=("init",),
        kind="write",
        surface="act",
        action=None,
        summary="Create the data root and, if missing, the config file and the key-encryption key.",
        params=(
            ParamSpec(
                "--device-name",
                "string",
                "Human-readable name of this device (stored in the config).",
            ),
            DATA_ROOT,
        ),
        examples=(
            Example("graph-admin init", "Create the default data root and config."),
            Example(
                "graph-admin init --data-root <dir> --device-name <value> --json",
                "Create a data root elsewhere and name the device.",
            ),
        ),
        errors=ROOT_ERRORS,
        handler=init_command,
        output=OUTPUT,
        remote="local",
    ),
)
