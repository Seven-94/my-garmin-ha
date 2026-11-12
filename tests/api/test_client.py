"""Data requests: endpoints, the single retry on 401, error mapping and secrecy."""

import asyncio
import json
import logging
from datetime import date
from pathlib import Path

import aiohttp
import pytest

from custom_components.my_garmin.api import (
    GarminApiError,
    GarminAuthError,
    GarminConnectionError,
    GarminError,
    GarminPersonalClient,
    GarminRateLimitError,
)
from tests.api.fake_garmin import FAKE_API_KEY, FakeClock, FakeGarmin, Reply

PROFILE_PATH = "/userprofile-service/userprofile/settings"
DAILY_PATH = "/usersummary-service/usersummary/daily"
HRV_PATH = "/hrv-service/hrv/2026-11-10"


async def test_get_user_profile_returns_the_json_object(
    client: GarminPersonalClient,
) -> None:
    assert await client.get_user_profile() == {"path": PROFILE_PATH, "query": {}}


async def test_get_daily_summary_sends_the_calendar_date(
    client: GarminPersonalClient,
) -> None:
    summary = await client.get_daily_summary(date(2026, 11, 10))

    assert summary == {"path": DAILY_PATH, "query": {"calendarDate": "2026-11-10"}}


async def test_get_daily_sleep_sends_the_wake_up_date(
    client: GarminPersonalClient,
) -> None:
    night = await client.get_daily_sleep(date(2026, 11, 10))

    assert night == {
        "path": "/sleep-service/sleep/dailySleep",
        "query": {"date": "2026-11-10"},
    }


async def test_get_returns_any_json_shape(
    client: GarminPersonalClient, fake_garmin: FakeGarmin
) -> None:
    fake_garmin.data_replies["/some-service/list"] = [Reply(body="[1, 2]")]

    assert await client.get("/some-service/list", {"limit": "2"}) == [1, 2]
    assert fake_garmin.requests[-1].query == {"limit": "2"}


async def test_revoked_token_is_renewed_and_request_retried_once(
    client: GarminPersonalClient, fake_garmin: FakeGarmin
) -> None:
    await client.get_user_profile()
    fake_garmin.revoke_issued()

    assert await client.get_user_profile() == {"path": PROFILE_PATH, "query": {}}
    assert fake_garmin.exchanges == 2


async def test_second_401_raises_auth_error(
    client: GarminPersonalClient, fake_garmin: FakeGarmin
) -> None:
    fake_garmin.data_replies[PROFILE_PATH] = [Reply(status=401), Reply(status=401)]

    with pytest.raises(GarminAuthError):
        await client.get_user_profile()
    assert fake_garmin.exchanges == 2


async def test_concurrent_401s_trigger_a_single_renewal(
    client: GarminPersonalClient, fake_garmin: FakeGarmin
) -> None:
    await client.get_user_profile()
    # Both requests use the first token; the second 401 arrives only after the
    # first request has renewed it.
    fake_garmin.data_replies[PROFILE_PATH] = [
        Reply(status=401),
        Reply(status=401, delay=0.2),
    ]

    await asyncio.gather(client.get_user_profile(), client.get_user_profile())

    # One exchange for the first token, one for its replacement: the late 401
    # must not discard the token the first request just obtained.
    assert fake_garmin.exchanges == 2


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Retry-After": "120"}, 120.0),
        ({}, None),
        ({"Retry-After": "Wed, 11 Nov 2026 16:00:00 GMT"}, None),
        ({"Retry-After": "-5"}, None),
    ],
)
async def test_429_raises_rate_limit_error_with_retry_after(
    client: GarminPersonalClient,
    fake_garmin: FakeGarmin,
    headers: dict[str, str],
    expected: float | None,
) -> None:
    fake_garmin.data_replies[PROFILE_PATH] = [Reply(status=429, headers=headers)]

    with pytest.raises(GarminRateLimitError) as err:
        await client.get_user_profile()
    assert err.value.retry_after == expected


@pytest.mark.parametrize("status", [400, 403, 404, 500])
async def test_http_error_raises_api_error_with_status(
    client: GarminPersonalClient, fake_garmin: FakeGarmin, status: int
) -> None:
    fake_garmin.data_replies[PROFILE_PATH] = [Reply(status=status)]

    with pytest.raises(GarminApiError) as err:
        await client.get_user_profile()
    assert err.value.status == status


async def test_get_hrv_puts_the_date_in_the_path(
    client: GarminPersonalClient,
) -> None:
    night = await client.get_hrv(date(2026, 11, 10))

    assert night == {"path": "/hrv-service/hrv/2026-11-10", "query": {}}


@pytest.mark.parametrize("body", ["null", ""])
async def test_get_hrv_without_data_returns_an_empty_object(
    client: GarminPersonalClient, fake_garmin: FakeGarmin, body: str
) -> None:
    # Devices that record no HRV get HTTP 200 with a null body.
    fake_garmin.data_replies[HRV_PATH] = [Reply(body=body)]

    assert await client.get_hrv(date(2026, 11, 10)) == {}


async def test_get_hrv_rejects_a_payload_that_is_not_an_object(
    client: GarminPersonalClient, fake_garmin: FakeGarmin
) -> None:
    fake_garmin.data_replies[HRV_PATH] = [Reply(body="[1, 2]")]

    with pytest.raises(GarminApiError):
        await client.get_hrv(date(2026, 11, 10))


async def test_get_weight_range_asks_for_every_weigh_in(
    client: GarminPersonalClient,
) -> None:
    weights = await client.get_weight_range(date(2026, 11, 3), date(2026, 11, 10))

    assert weights == {
        "path": "/weight-service/weight/range/2026-11-03/2026-11-10",
        "query": {"includeAll": "true"},
    }


async def test_get_weight_goal_puts_the_dates_in_the_path(
    client: GarminPersonalClient,
) -> None:
    goal = await client.get_weight_goal(date(2026, 11, 10), date(2026, 11, 10))

    assert goal == {
        "path": "/goal-service/goal/user/effective/weightgoal/2026-11-10/2026-11-10",
        "query": {},
    }


@pytest.mark.parametrize(
    "path",
    [
        "/weight-service/weight/range/2026-11-10/2026-11-10",
        "/goal-service/goal/user/effective/weightgoal/2026-11-10/2026-11-10",
    ],
)
async def test_weight_endpoints_without_data_return_an_empty_object(
    client: GarminPersonalClient, fake_garmin: FakeGarmin, path: str
) -> None:
    fake_garmin.data_replies[path] = [Reply(body="null")]
    day = date(2026, 11, 10)

    if "goal" in path:
        assert await client.get_weight_goal(day, day) == {}
    else:
        assert await client.get_weight_range(day, day) == {}


ACTIVITIES_PATH = "/activitylist-service/activities/search/activities"


async def test_get_activities_returns_the_latest_ones(
    client: GarminPersonalClient, fake_garmin: FakeGarmin
) -> None:
    fake_garmin.data_replies[ACTIVITIES_PATH] = [Reply(body='[{"activityId": 1}]')]

    assert await client.get_activities(10) == [{"activityId": 1}]
    request = fake_garmin.requests[-1]
    assert dict(request.query) == {"start": "0", "limit": "10"}


@pytest.mark.parametrize("body", ["null", ""])
async def test_get_activities_without_data_returns_an_empty_list(
    client: GarminPersonalClient, fake_garmin: FakeGarmin, body: str
) -> None:
    fake_garmin.data_replies[ACTIVITIES_PATH] = [Reply(body=body)]

    assert await client.get_activities(10) == []


@pytest.mark.parametrize("body", ['{"activityId": 1}', "[1, 2]"])
async def test_get_activities_rejects_anything_but_a_list_of_objects(
    client: GarminPersonalClient, fake_garmin: FakeGarmin, body: str
) -> None:
    fake_garmin.data_replies[ACTIVITIES_PATH] = [Reply(body=body)]

    with pytest.raises(GarminApiError):
        await client.get_activities(10)


ZONES_PATH = "/biometric-service/heartRateZones/v2"


async def test_get_heart_rate_zones_returns_every_sport(
    client: GarminPersonalClient, fake_garmin: FakeGarmin
) -> None:
    body = '[{"sport": "DEFAULT", "maxHeartRateUsed": 186}, {"sport": "RUNNING"}]'
    fake_garmin.data_replies[ZONES_PATH] = [Reply(body=body)]

    assert await client.get_heart_rate_zones() == [
        {"sport": "DEFAULT", "maxHeartRateUsed": 186},
        {"sport": "RUNNING"},
    ]


@pytest.mark.parametrize("body", ["null", ""])
async def test_get_heart_rate_zones_without_data_returns_an_empty_list(
    client: GarminPersonalClient, fake_garmin: FakeGarmin, body: str
) -> None:
    fake_garmin.data_replies[ZONES_PATH] = [Reply(body=body)]

    assert await client.get_heart_rate_zones() == []


@pytest.mark.parametrize("body", ['{"sport": "DEFAULT"}', "[1, 2]"])
async def test_get_heart_rate_zones_rejects_anything_but_a_list_of_objects(
    client: GarminPersonalClient, fake_garmin: FakeGarmin, body: str
) -> None:
    fake_garmin.data_replies[ZONES_PATH] = [Reply(body=body)]

    with pytest.raises(GarminApiError):
        await client.get_heart_rate_zones()


@pytest.mark.parametrize("body", ["<html>", "[1, 2]", ""])
async def test_payload_that_is_not_a_json_object_raises_api_error(
    client: GarminPersonalClient, fake_garmin: FakeGarmin, body: str
) -> None:
    fake_garmin.data_replies[PROFILE_PATH] = [Reply(body=body)]

    with pytest.raises(GarminApiError):
        await client.get_user_profile()


async def test_timeout_raises_connection_error(
    session: aiohttp.ClientSession, fake_garmin: FakeGarmin, clock: FakeClock
) -> None:
    client = GarminPersonalClient(
        session, FAKE_API_KEY, base_url=fake_garmin.base_url, clock=clock, timeout=0.05
    )
    fake_garmin.data_delay = 1.0

    with pytest.raises(GarminConnectionError):
        await client.get_user_profile()


async def test_all_errors_share_a_base_class() -> None:
    for error in (
        GarminApiError,
        GarminAuthError,
        GarminConnectionError,
        GarminRateLimitError,
    ):
        assert issubclass(error, GarminError)


async def test_key_and_tokens_never_leak(
    client: GarminPersonalClient,
    fake_garmin: FakeGarmin,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    errors: list[str] = []

    await client.get_user_profile()
    fake_garmin.revoke_issued()
    await client.get_user_profile()
    fake_garmin.data_replies[PROFILE_PATH] = [Reply(status=401), Reply(status=401)]
    with pytest.raises(GarminAuthError) as auth_err:
        await client.get_user_profile()
    errors.append(repr(auth_err.value))
    fake_garmin.token_replies.append(Reply(status=401))
    fake_garmin.revoke_issued()
    with pytest.raises(GarminAuthError) as key_err:
        await client.get_user_profile()
    errors.append(repr(key_err.value))

    secrets = [FAKE_API_KEY, *fake_garmin.issued]
    assert len(secrets) == 4
    # vars() also renders the token manager, which holds the key.
    exposed = [caplog.text, repr(client), str(client), repr(vars(client)), *errors]
    for secret in secrets:
        for text in exposed:
            assert secret not in text


FITNESS_AGE_PATH = "/fitnessage-service/fitnessage/v2/2026-11-10"
FITNESS_FIXTURE = Path(__file__).parents[1] / "fixtures" / "fitness_age_synthetic.json"


async def test_get_fitness_age_puts_the_date_in_the_path(
    client: GarminPersonalClient,
) -> None:
    fitness = await client.get_fitness_age(date(2026, 11, 10))

    # The default full history scan is kept: a measurement can be months old.
    assert fitness == {"path": FITNESS_AGE_PATH, "query": {}}


async def test_get_fitness_age_reads_a_single_object(
    client: GarminPersonalClient, fake_garmin: FakeGarmin
) -> None:
    fixture = FITNESS_FIXTURE.read_text(encoding="utf-8")
    fake_garmin.data_replies[FITNESS_AGE_PATH] = [Reply(body=fixture)]

    fitness = await client.get_fitness_age(date(2026, 11, 10))

    assert fitness == json.loads(fixture)


async def test_get_fitness_age_reads_the_most_recently_updated_entry_of_a_list(
    client: GarminPersonalClient, fake_garmin: FakeGarmin
) -> None:
    # The newest entry comes first: taking the last one would be wrong.
    fitness = json.loads(FITNESS_FIXTURE.read_text(encoding="utf-8"))
    older = {**fitness, "fitnessAge": 44.0, "lastUpdated": "2026-08-01T05:40:00.0"}
    fake_garmin.data_replies[FITNESS_AGE_PATH] = [
        Reply(body=json.dumps([fitness, older]))
    ]

    assert await client.get_fitness_age(date(2026, 11, 10)) == fitness


@pytest.mark.parametrize("body", ["null", "", "[]"])
async def test_get_fitness_age_without_data_returns_an_empty_object(
    client: GarminPersonalClient, fake_garmin: FakeGarmin, body: str
) -> None:
    fake_garmin.data_replies[FITNESS_AGE_PATH] = [Reply(body=body)]

    assert await client.get_fitness_age(date(2026, 11, 10)) == {}


@pytest.mark.parametrize("body", ['"38.4"', "[1, 2]"])
async def test_get_fitness_age_rejects_anything_but_an_object_or_a_list_of_objects(
    client: GarminPersonalClient, fake_garmin: FakeGarmin, body: str
) -> None:
    fake_garmin.data_replies[FITNESS_AGE_PATH] = [Reply(body=body)]

    with pytest.raises(GarminApiError):
        await client.get_fitness_age(date(2026, 11, 10))


SPO2_PATH = "/wellness-service/wellness/daily/spo2acclimation/2026-11-10"


async def test_get_spo2_puts_the_date_in_the_path(
    client: GarminPersonalClient,
) -> None:
    spo2 = await client.get_spo2(date(2026, 11, 10))

    assert spo2 == {"path": SPO2_PATH, "query": {}}


@pytest.mark.parametrize("body", ["null", ""])
async def test_get_spo2_without_data_returns_an_empty_object(
    client: GarminPersonalClient, fake_garmin: FakeGarmin, body: str
) -> None:
    fake_garmin.data_replies[SPO2_PATH] = [Reply(body=body)]

    assert await client.get_spo2(date(2026, 11, 10)) == {}


async def test_get_spo2_rejects_a_payload_that_is_not_an_object(
    client: GarminPersonalClient, fake_garmin: FakeGarmin
) -> None:
    fake_garmin.data_replies[SPO2_PATH] = [Reply(body="[1, 2]")]

    with pytest.raises(GarminApiError):
        await client.get_spo2(date(2026, 11, 10))


DAILY_HEART_RATE_PATH = "/wellness-service/wellness/dailyHeartRate"


async def test_get_daily_heart_rate_sends_the_date(
    client: GarminPersonalClient,
) -> None:
    heart_rate = await client.get_daily_heart_rate(date(2026, 11, 10))

    assert heart_rate == {
        "path": DAILY_HEART_RATE_PATH,
        "query": {"date": "2026-11-10"},
    }


@pytest.mark.parametrize("body", ["null", ""])
async def test_get_daily_heart_rate_without_data_returns_an_empty_object(
    client: GarminPersonalClient, fake_garmin: FakeGarmin, body: str
) -> None:
    fake_garmin.data_replies[DAILY_HEART_RATE_PATH] = [Reply(body=body)]

    assert await client.get_daily_heart_rate(date(2026, 11, 10)) == {}


async def test_get_daily_heart_rate_rejects_a_payload_that_is_not_an_object(
    client: GarminPersonalClient, fake_garmin: FakeGarmin
) -> None:
    fake_garmin.data_replies[DAILY_HEART_RATE_PATH] = [Reply(body="[1, 2]")]

    with pytest.raises(GarminApiError):
        await client.get_daily_heart_rate(date(2026, 11, 10))
