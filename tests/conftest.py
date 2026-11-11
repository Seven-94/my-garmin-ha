"""Shared fixtures: the fake Garmin server, and Home Assistant set-up."""

from collections.abc import AsyncIterator

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from custom_components.my_garmin.api import GarminPersonalClient
from tests.api.fake_garmin import FAKE_API_KEY, FakeClock, FakeGarmin


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load custom_components/my_garmin in every test."""


# The Home Assistant test plugin blocks every socket. socket_enabled lifts that
# block for the fake server, while the plugin's 127.0.0.1-only filter still
# stops any connection to the Internet.
@pytest.fixture
async def fake_garmin(
    clock: FakeClock, socket_enabled: None
) -> AsyncIterator[FakeGarmin]:
    del socket_enabled  # requested for its side effect only
    fake = FakeGarmin(clock)
    server = TestServer(fake.app)
    await server.start_server()
    fake.base_url = str(server.make_url("")).rstrip("/")
    yield fake
    await server.close()


@pytest.fixture
async def session(socket_enabled: None) -> AsyncIterator[aiohttp.ClientSession]:
    del socket_enabled  # requested for its side effect only
    async with aiohttp.ClientSession() as client_session:
        yield client_session


@pytest.fixture
def client(
    session: aiohttp.ClientSession, fake_garmin: FakeGarmin, clock: FakeClock
) -> GarminPersonalClient:
    return GarminPersonalClient(
        session, FAKE_API_KEY, base_url=fake_garmin.base_url, clock=clock
    )
