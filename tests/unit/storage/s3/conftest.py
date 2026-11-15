"""Fixtures of the S3 client tests: the fake store, a clock shared with it and a client whose
sleeps and jitter are recorded instead of waited for."""

from collections.abc import Iterator

import pytest
from support.clock import FixedClock
from support.fake_s3 import FakeS3
from support.s3_helpers import T0, ClientFactory, Sleeps

from graph_cli.storage.s3.client import ObjectStoreClient, RetryPolicy, Timeouts
from graph_cli.storage.s3.settings import ObjectStoreSettings


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(T0)


@pytest.fixture
def fake(clock: FixedClock) -> Iterator[FakeS3]:
    with FakeS3(clock=clock) as store:
        yield store


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


@pytest.fixture
def make_client(fake: FakeS3, clock: FixedClock, sleeps: Sleeps) -> ClientFactory:
    """``make_client(settings=None, **client_options)``: a client for the fake (or the given
    settings) with short timeouts, the default retry policy and recorded sleeps."""

    def build(settings: ObjectStoreSettings | None = None, **options: object) -> ObjectStoreClient:
        values: dict[str, object] = {
            "clock": clock,
            "timeouts": Timeouts(connect_s=2.0, read_s=2.0),
            "retry": RetryPolicy(max_retries=3, base_s=0.5, cap_s=8.0),
            "sleep": sleeps.sleep,
            "rng": sleeps.random,
            "chunk_size": 64 * 1024,
        }
        values.update(options)
        return ObjectStoreClient(settings or fake.settings(), **values)  # type: ignore[arg-type]

    return build


@pytest.fixture
def client(make_client: ClientFactory) -> ObjectStoreClient:
    return make_client()
