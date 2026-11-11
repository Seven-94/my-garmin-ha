"""API key to JWT exchange: when the client exchanges, and how it reports failures."""

import asyncio

import aiohttp
import pytest

from custom_components.my_garmin.api import (
    GarminApiError,
    GarminAuthError,
    GarminConnectionError,
    GarminPersonalClient,
    GarminRateLimitError,
)
from tests.api.fake_garmin import (
    DAY,
    FAKE_API_KEY,
    KEY_EXCHANGE_PATH,
    FakeClock,
    FakeGarmin,
    Reply,
    make_jwt,
)

HOUR = 3_600.0


async def test_exchange_sends_api_key_then_bearer_token(
    client: GarminPersonalClient, fake_garmin: FakeGarmin
) -> None:
    await client.get_user_profile()

    exchange, data = fake_garmin.requests
    assert exchange.method == "POST"
    assert exchange.path == KEY_EXCHANGE_PATH
    assert exchange.headers["Authorization"] == f"ApiKey {FAKE_API_KEY}"
    assert exchange.headers["Accept"] == "*/*"
    assert data.headers["Authorization"] == f"Bearer {fake_garmin.issued[0]}"
    assert data.headers["Accept"] == "application/json"


async def test_token_is_reused_while_valid(
    client: GarminPersonalClient, fake_garmin: FakeGarmin, clock: FakeClock
) -> None:
    await client.get_user_profile()
    clock.advance(DAY - HOUR - 1)
    await client.get_user_profile()

    assert fake_garmin.exchanges == 1


async def test_token_is_renewed_less_than_one_hour_before_expiry(
    client: GarminPersonalClient, fake_garmin: FakeGarmin, clock: FakeClock
) -> None:
    await client.get_user_profile()
    clock.advance(DAY - HOUR + 1)
    await client.get_user_profile()

    assert fake_garmin.exchanges == 2
    assert fake_garmin.requests[-1].headers["Authorization"] == (
        f"Bearer {fake_garmin.issued[1]}"
    )


async def test_short_lived_token_is_renewed_at_half_its_lifetime(
    client: GarminPersonalClient, fake_garmin: FakeGarmin, clock: FakeClock
) -> None:
    # A key close to its own expiry yields a token shorter than the one-hour
    # margin: renewing at half its lifetime avoids an exchange on every call.
    fake_garmin.token_lifetime = 30 * 60
    await client.get_user_profile()
    clock.advance(14 * 60)
    await client.get_user_profile()
    assert fake_garmin.exchanges == 1

    clock.advance(2 * 60)
    await client.get_user_profile()
    assert fake_garmin.exchanges == 2


async def test_concurrent_requests_share_one_exchange(
    client: GarminPersonalClient, fake_garmin: FakeGarmin
) -> None:
    await asyncio.gather(*(client.get_user_profile() for _ in range(5)))

    assert fake_garmin.exchanges == 1


@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_key_raises_auth_error(
    client: GarminPersonalClient, fake_garmin: FakeGarmin, status: int
) -> None:
    fake_garmin.token_replies.append(Reply(status=status, body=""))

    with pytest.raises(GarminAuthError):
        await client.get_user_profile()


async def test_rate_limited_exchange_raises_rate_limit_error(
    client: GarminPersonalClient, fake_garmin: FakeGarmin
) -> None:
    fake_garmin.token_replies.append(
        Reply(status=429, body="", headers={"Retry-After": "60"})
    )

    with pytest.raises(GarminRateLimitError) as err:
        await client.get_user_profile()
    assert err.value.retry_after == 60.0


async def test_server_error_on_exchange_raises_api_error(
    client: GarminPersonalClient, fake_garmin: FakeGarmin
) -> None:
    fake_garmin.token_replies.append(Reply(status=503, body=""))

    with pytest.raises(GarminApiError) as err:
        await client.get_user_profile()
    assert err.value.status == 503


@pytest.mark.parametrize(
    "body",
    [
        "not-a-jwt",
        "a.!!!.c",
        "a.bm90LWpzb24.c",  # payload is not JSON
        make_jwt({"sub": "no-exp"}),
        make_jwt({"exp": "tomorrow"}),
        make_jwt({"exp": True}),
    ],
)
async def test_invalid_token_raises_api_error_without_echoing_it(
    client: GarminPersonalClient, fake_garmin: FakeGarmin, body: str
) -> None:
    fake_garmin.token_replies.append(Reply(body=body))

    with pytest.raises(GarminApiError) as err:
        await client.get_user_profile()
    assert body not in str(err.value)
    assert err.value.__cause__ is None


async def test_unreachable_token_endpoint_raises_connection_error(
    session: aiohttp.ClientSession, clock: FakeClock
) -> None:
    # Port 1 on localhost is closed: the connection is refused at once.
    client = GarminPersonalClient(
        session, FAKE_API_KEY, base_url="http://127.0.0.1:1", clock=clock
    )

    with pytest.raises(GarminConnectionError):
        await client.get_user_profile()
