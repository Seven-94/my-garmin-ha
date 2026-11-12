"""The backfill_statistics service: daily history as external statistics.

`daily_summary_v2_synthetic.json` and `sleep_summary_synthetic.json` are one day
each, built from the API schema with synthetic values; the tests derive every
requested day from them.
"""

import asyncio
import copy
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    list_statistic_ids,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.my_garmin import statistics
from custom_components.my_garmin.api import GarminRateLimitError
from custom_components.my_garmin.const import DOMAIN
from tests.integration.conftest import PROFILE_ID, load_fixture

SERVICE = "backfill_statistics"
TYPES: set[Any] = {"state", "sum", "mean", "min", "max"}


def _id(key: str) -> str:
    return f"{DOMAIN}:{PROFILE_ID}_{key}"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    recorder_db_url: str, enable_custom_integrations: None
) -> None:
    """The test database must exist before hass: request it first."""
    del recorder_db_url, enable_custom_integrations


@pytest.fixture(autouse=True)
def no_pause(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    # The module's own pause only: asyncio.sleep is shared with Home Assistant.
    pause = AsyncMock()
    monkeypatch.setattr(statistics, "_pause", pause)
    return pause


def _yesterday() -> date:
    return dt_util.now().date() - timedelta(days=1)


def _summary(day: date) -> dict[str, Any]:
    """The template day, with values that differ from one day to the next."""
    data = copy.deepcopy(load_fixture("daily_summary_v2_synthetic.json"))
    data["calendarDate"] = day.isoformat()
    data["movement"]["steps"]["value"] = 1000 + day.day
    data["heartRate"]["restingValue"] = 50 + day.day % 5
    return data


def _night(day: date) -> dict[str, Any]:
    data = copy.deepcopy(load_fixture("sleep_summary_synthetic.json"))
    data["calendarDate"] = day.isoformat()
    data["sleepTimeSeconds"] = 25000 + day.day
    return data


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=n) for n in range((end - start).days + 1)]


def _serve(make: Callable[[date], dict[str, Any]]) -> AsyncMock:
    async def reply(start: date, end: date) -> list[dict[str, Any]]:
        return [make(day) for day in _days(start, end)]

    return AsyncMock(side_effect=reply)


@pytest.fixture
def garmin(mock_client: MagicMock) -> MagicMock:
    mock_client.get_daily_summaries = _serve(_summary)
    mock_client.get_sleep_summaries = _serve(_night)
    return mock_client


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _backfill(hass: HomeAssistant, **data: Any) -> dict[str, Any]:
    response = await hass.services.async_call(
        DOMAIN, SERVICE, data, blocking=True, return_response=True
    )
    await async_wait_recording_done(hass)
    assert isinstance(response, dict)
    return response


async def _rows(hass: HomeAssistant, key: str) -> list[dict[str, Any]]:
    start = dt_util.now() - timedelta(days=1200)
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, None, {_id(key)}, "hour", None, TYPES
    )
    return [dict(row) for row in rows.get(_id(key), [])]


def _midnight(day: date) -> float:
    return datetime.combine(day, time(), dt_util.get_default_time_zone()).timestamp()


@pytest.mark.usefixtures("recorder_mock", "garmin")
async def test_daily_totals_become_cumulative_sums(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    await _backfill(hass, days=3)

    days = _days(_yesterday() - timedelta(days=2), _yesterday())
    rows = await _rows(hass, "steps")
    assert [row["start"] for row in rows] == [_midnight(day) for day in days]
    steps = [1000 + day.day for day in days]
    assert [row["state"] for row in rows] == steps
    assert [row["sum"] for row in rows] == [
        sum(steps[: n + 1]) for n in range(len(steps))
    ]


@pytest.mark.usefixtures("recorder_mock", "garmin")
async def test_daily_readings_become_means(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    await _backfill(hass, days=1)

    day = _summary(_yesterday())
    resting = (await _rows(hass, "resting_heart_rate"))[0]
    expected = day["heartRate"]["restingValue"]
    assert (resting["mean"], resting["min"], resting["max"]) == (expected,) * 3
    stress = (await _rows(hass, "stress"))[0]
    assert stress["mean"] == day["stress"]["avgLevel"]
    assert stress["max"] == day["stress"]["maxLevel"]
    respiration = (await _rows(hass, "respiration"))[0]
    assert respiration["mean"] == day["respiration"]["avgValue"]
    assert respiration["min"] == day["respiration"]["minValue"]
    assert respiration["max"] == day["respiration"]["maxValue"]
    spo2 = (await _rows(hass, "spo2"))[0]
    assert (spo2["mean"], spo2["min"]) == (96, 90)


@pytest.mark.usefixtures("recorder_mock", "garmin")
async def test_every_total_is_imported(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    await _backfill(hass, days=1)

    expected = {
        "distance": 7200.0,
        "floors": 12,
        "active_calories": 500,
        "total_calories": 2200,
        # A vigorous minute counts twice, as on the watch.
        "intensity_minutes": 20 + 2 * 10,
        "body_battery_charged": 60,
        "body_battery_drained": 55,
    }
    for key, value in expected.items():
        assert (await _rows(hass, key))[0]["state"] == value, key


@pytest.mark.usefixtures("recorder_mock", "garmin")
async def test_nights_are_imported_on_their_wake_up_day(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    await _backfill(hass, days=1)

    night = _night(_yesterday())
    duration = (await _rows(hass, "sleep_duration"))[0]
    assert duration["start"] == _midnight(_yesterday())
    assert duration["mean"] == night["sleepTimeSeconds"]
    assert (await _rows(hass, "sleep_score"))[0]["mean"] == 82


@pytest.mark.usefixtures("recorder_mock", "garmin")
async def test_statistics_carry_units_and_names(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    await _backfill(hass, days=1)

    found = await get_instance(hass).async_add_executor_job(list_statistic_ids, hass)
    by_id = {item["statistic_id"]: item for item in found}
    assert by_id[_id("steps")]["statistics_unit_of_measurement"] == "steps"
    assert by_id[_id("distance")]["unit_class"] == "distance"
    assert by_id[_id("active_calories")]["statistics_unit_of_measurement"] == "kcal"
    assert by_id[_id("sleep_duration")]["unit_class"] == "duration"
    assert by_id[_id("steps")]["source"] == DOMAIN
    assert by_id[_id("steps")]["name"] == "My Garmin steps"


@pytest.mark.usefixtures("recorder_mock")
async def test_requests_go_by_31_days_with_a_pause_between_them(
    hass: HomeAssistant,
    garmin: MagicMock,
    no_pause: AsyncMock,
    config_entry: MockConfigEntry,
) -> None:
    await _setup(hass, config_entry)

    await _backfill(hass, days=40)

    end = _yesterday()
    start = end - timedelta(days=39)
    windows = [(start, start + timedelta(days=30)), (start + timedelta(days=31), end)]
    for method in (garmin.get_daily_summaries, garmin.get_sleep_summaries):
        assert [call.args for call in method.await_args_list] == windows
    # One pause between two requests: four requests, three pauses.
    assert no_pause.await_count == 3


async def test_the_pause_lasts_the_backfill_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.undo()  # the real _pause, with asyncio.sleep replaced instead
    sleep = AsyncMock()
    monkeypatch.setattr(statistics.asyncio, "sleep", sleep)

    await statistics._pause()

    sleep.assert_awaited_once_with(statistics.BACKFILL_PAUSE)


@pytest.mark.usefixtures("recorder_mock", "garmin")
async def test_a_later_run_continues_the_stored_sums(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    await _backfill(hass, days=3)
    first = await _rows(hass, "steps")

    await _backfill(hass, days=2)

    assert await _rows(hass, "steps") == first


@pytest.mark.usefixtures("recorder_mock", "garmin")
async def test_missing_values_are_skipped(
    hass: HomeAssistant, garmin: MagicMock, config_entry: MockConfigEntry
) -> None:
    def gap(day: date) -> dict[str, Any]:
        data = _summary(day)
        if day == _yesterday() - timedelta(days=1):
            data["movement"]["steps"]["value"] = None
            data["heartRate"]["restingValue"] = 0
        return data

    garmin.get_daily_summaries = _serve(gap)
    await _setup(hass, config_entry)

    await _backfill(hass, days=3)

    first, last = _yesterday() - timedelta(days=2), _yesterday()
    steps = await _rows(hass, "steps")
    assert [row["start"] for row in steps] == [_midnight(first), _midnight(last)]
    assert steps[1]["sum"] == (1000 + first.day) + (1000 + last.day)
    resting = await _rows(hass, "resting_heart_rate")
    assert [row["start"] for row in resting] == [_midnight(first), _midnight(last)]


@pytest.mark.usefixtures("recorder_mock")
async def test_a_429_stops_the_run_and_keeps_what_was_fetched(
    hass: HomeAssistant, garmin: MagicMock, config_entry: MockConfigEntry
) -> None:
    serve = garmin.get_daily_summaries.side_effect
    calls = 0

    async def throttled(start: date, end: date) -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise GarminRateLimitError(retry_after=60)
        return await serve(start, end)

    garmin.get_daily_summaries = AsyncMock(side_effect=throttled)
    await _setup(hass, config_entry)

    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN, SERVICE, {"days": 70}, blocking=True, return_response=True
        )
    await async_wait_recording_done(hass)

    assert err.value.translation_key == "rate_limited"
    assert garmin.get_daily_summaries.await_count == 2
    # The first window was imported before Garmin said stop.
    assert len(await _rows(hass, "steps")) == 31


@pytest.mark.usefixtures("recorder_mock")
async def test_one_run_at_a_time(
    hass: HomeAssistant, garmin: MagicMock, config_entry: MockConfigEntry
) -> None:
    release = asyncio.Event()
    serve = garmin.get_daily_summaries.side_effect

    async def slow(start: date, end: date) -> list[dict[str, Any]]:
        await release.wait()
        return await serve(start, end)

    garmin.get_daily_summaries = AsyncMock(side_effect=slow)
    await _setup(hass, config_entry)
    first = hass.async_create_task(_backfill(hass, days=1))
    await asyncio.sleep(0)

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, SERVICE, {"days": 1}, blocking=True, return_response=True
        )

    assert err.value.translation_key == "already_running"
    release.set()
    await first


@pytest.mark.usefixtures("recorder_mock", "garmin")
async def test_response_says_what_was_imported(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    response = await _backfill(hass, days=2)

    assert response["start"] == (_yesterday() - timedelta(days=1)).isoformat()
    assert response["end"] == _yesterday().isoformat()
    assert response["days"] == 2
    assert _id("steps") in response["statistic_ids"]
    assert response["statistic_ids"] == sorted(response["statistic_ids"])


@pytest.mark.usefixtures("recorder_mock", "garmin")
async def test_default_is_one_year(
    hass: HomeAssistant, garmin: MagicMock, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    response = await _backfill(hass)

    assert response["start"] == (_yesterday() - timedelta(days=364)).isoformat()
    assert garmin.get_daily_summaries.await_count == 12


@pytest.mark.usefixtures("recorder_mock", "garmin")
@pytest.mark.parametrize("days", [0, 1096])
async def test_days_are_bounded(
    hass: HomeAssistant, config_entry: MockConfigEntry, days: int
) -> None:
    await _setup(hass, config_entry)

    with pytest.raises(Exception, match="days"):
        await hass.services.async_call(
            DOMAIN, SERVICE, {"days": days}, blocking=True, return_response=True
        )


@pytest.mark.usefixtures("recorder_mock", "garmin")
async def test_an_unknown_entry_is_refused(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            SERVICE,
            {"config_entry_id": "not-an-entry"},
            blocking=True,
            return_response=True,
        )

    assert err.value.translation_key == "entry_not_loaded"


@pytest.mark.usefixtures("garmin")
async def test_without_the_recorder_the_service_refuses(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, SERVICE, {}, blocking=True, return_response=True
        )

    assert err.value.translation_key == "recorder_unavailable"


@pytest.mark.usefixtures("recorder_mock")
async def test_a_source_without_data_imports_nothing_for_it(
    hass: HomeAssistant, garmin: MagicMock, config_entry: MockConfigEntry
) -> None:
    garmin.get_sleep_summaries = AsyncMock(return_value=[])
    await _setup(hass, config_entry)

    response = await _backfill(hass, days=2)

    assert _id("steps") in response["statistic_ids"]
    assert _id("sleep_duration") not in response["statistic_ids"]
    assert await _rows(hass, "sleep_duration") == []


def test_intensity_minutes_need_one_of_the_two_counts() -> None:
    read = statistics._intensity_minutes
    assert read({}) is None
    assert read({"intensityMinutes": {"vigorous": 5}}) == 10


def test_entries_with_an_unreadable_date_are_ignored() -> None:
    entries = [
        {"calendarDate": "2026-11-08", "value": 1},
        {"calendarDate": "not-a-date", "value": 2},
        {"calendarDate": None, "value": 3},
    ]

    assert statistics._by_day(entries) == {date(2026, 11, 8): entries[0]}
