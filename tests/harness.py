"""Types and constants shared by tests (fixtures live in conftest.py)."""

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

BIN_DIR = Path(sys.executable).parent
REPO_ROOT = Path(__file__).resolve().parent.parent

CliRunner = Callable[..., subprocess.CompletedProcess[str]]
