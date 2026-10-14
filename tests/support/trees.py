"""Directory tree fingerprints: prove that reads write nothing (B13.7)."""

import hashlib
import os
from pathlib import Path


def tree_hash(root: Path) -> str:
    """Hash of every directory and file below ``root``: relative path, kind, mode, bytes."""
    digest = hashlib.sha256()
    for directory, dirs, files in os.walk(root):
        dirs.sort()
        base = Path(directory)
        digest.update(f"d {base.relative_to(root)} {base.stat().st_mode:o}\n".encode())
        for name in sorted(files):
            path = base / name
            digest.update(f"f {path.relative_to(root)} {path.stat().st_mode:o}\n".encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()
