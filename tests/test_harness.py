"""The test harness never touches the real user environment."""

import os
from pathlib import Path


def test_isolated_env_points_into_tmp(isolated_env: dict[str, str], tmp_path: Path) -> None:
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        assert Path(isolated_env[key]).is_relative_to(tmp_path), key
    overrides = {k: v for k, v in isolated_env.items() if k.startswith("GRAPH_CLI_")}
    assert overrides == {"GRAPH_CLI_SPILL_BYTES": "0"}  # answers stay on stdout
    assert isolated_env["HOME"] != os.environ.get("HOME")
