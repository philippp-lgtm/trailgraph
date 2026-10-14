"""Lock worker: ``hold`` the lock until released, or ``try`` to take it with a timeout."""

import time
from pathlib import Path

from graph_cli.kernel.errors import GraphCliError
from graph_cli.storage.files.locking import acquire
from support.procs import release, wait_for
from support.workers import args, emit


def main() -> None:
    spec = args()
    lock = Path(spec["lock"])
    if spec["mode"] == "hold":
        with acquire(lock, timeout_s=10):
            release(Path(spec["ready"]))
            wait_for(Path(spec["release"]))
        emit({"held": True})
        return
    started = time.monotonic()
    try:
        with acquire(lock, timeout_s=spec["timeout_s"]):
            emit({"acquired": True, "elapsed_s": time.monotonic() - started})
    except GraphCliError as exc:
        emit(
            {
                "acquired": False,
                "code": exc.code,
                "retryable": exc.retryable,
                "elapsed_s": time.monotonic() - started,
            }
        )


if __name__ == "__main__":
    main()
