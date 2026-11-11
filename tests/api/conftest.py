"""Fixtures wiring the client to the fake Garmin server."""

from collections.abc import AsyncIterator

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from custom_components.my_garmin.api import GarminPersonalClient
from tests.api.fake_garmin import FAKE_API_KEY, FakeClock, FakeGarmin


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def fake_garmin(clock: FakeClock) -> AsyncIterator[FakeGarmin]:
    fake = FakeGarmin(clock)
    server = TestServer(fake.app)
    await server.start_server()
    fake.base_url = str(server.make_url("")).rstrip("/")
    yield fake
    await server.close()


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as client_session:
        yield client_session


@pytest.fixture
def client(
    session: aiohttp.ClientSession, fake_garmin: FakeGarmin, clock: FakeClock
) -> GarminPersonalClient:
    return GarminPersonalClient(
        session, FAKE_API_KEY, base_url=fake_garmin.base_url, clock=clock
    )
