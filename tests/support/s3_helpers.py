"""Shared helpers of the S3 client tests: a fixed start time, recorded sleeps and jitter."""

from collections.abc import Callable
from datetime import UTC, datetime

from graph_cli.storage.s3.client import ObjectStoreClient

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

ClientFactory = Callable[..., ObjectStoreClient]


class Sleeps:
    """Records the waits the client asks for; ``jitter`` is what the random source answers."""

    def __init__(self) -> None:
        self.waits: list[float] = []
        self.jitter = 0.5

    def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)

    def random(self) -> float:
        return self.jitter
