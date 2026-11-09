"""``graph-admin init``: create the data root, the config file and the KEK file (B12.4, T16).

Idempotent. An existing ``config.toml`` belongs to the user and is never
rewritten; a new one gets a generated device id (B2.3, Decision 30). The
data root is persisted in the new config only when it was chosen with
``--data-root``. The key-encryption key of sealed graphs is created where
``[keys] kek_file`` (or ``GRAPH_CLI_KEK_FILE``) says, only when no file is
there: an existing KEK is never replaced. No audit event: init is no action
of the taxonomy and touches no graph.
"""

from __future__ import annotations

import os

from graph_cli.catalog.config import format_new_config
from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.errors import GraphCliError
from graph_cli.kernel.ids import format_id
from graph_cli.storage.files.durable import OsSyncer, ensure_dir, write_temp
from graph_cli_admin.services_admin.context import AdminContext


def init(ctx: AdminContext, *, device_name: str | None) -> dict[str, JsonValue]:
    roots, syncer = ctx.roots, OsSyncer(ctx.config.durability)
    try:
        root_created = ensure_dir(roots.data_root / "graphs", syncer)
    except OSError as exc:
        raise GraphCliError(
            "backend.unavailable", f"cannot create the data root: {exc.strerror or exc}"
        ) from exc
    device_id = ctx.config.device_id
    config_created = False
    if not ctx.config.exists:
        device_id = format_id(ctx.ids.new_id())
        persisted = str(roots.data_root) if roots.root_source == "flag" else None
        data = format_new_config(device_id=device_id, device_name=device_name, data_root=persisted)
        try:
            ensure_dir(roots.config_file.parent, syncer)
            temp = write_temp(roots.config_file, data, syncer)
            os.replace(temp, roots.config_file)
            syncer.sync_dir(roots.config_file.parent)
        except OSError as exc:
            raise GraphCliError(
                "config.invalid", f"cannot write {roots.config_file}: {exc.strerror or exc}"
            ) from exc
        config_created = True
    kek_file = ctx.maintenance.rings.settings.kek_file if ctx.maintenance.rings else None
    kek_created = False
    if kek_file is not None and not os.path.lexists(kek_file):
        from graph_cli.storage.files.keystore import create_kek_file  # the cipher library

        try:
            create_kek_file(kek_file, syncer=syncer)
        except OSError as exc:
            raise GraphCliError(
                "config.invalid", f"cannot write {kek_file}: {exc.strerror or exc}"
            ) from exc
        kek_created = True
    return {
        "data_root": str(roots.data_root),
        "root_source": roots.root_source,
        "config_file": str(roots.config_file),
        "device_id": device_id,
        "kek_file": str(kek_file) if kek_file is not None else None,
        "created": {"data_root": root_created, "config": config_created, "kek": kek_created},
    }
