"""Seeded UUIDv7 provider: the production algorithm with a deterministic RNG."""

import random

from graph_cli.kernel.clock import Clock
from graph_cli.kernel.ids import Uuid7Provider


class SeededIdProvider(Uuid7Provider):
    def __init__(self, clock: Clock, seed: int = 0) -> None:
        super().__init__(clock, randbits=random.Random(seed).getrandbits)
