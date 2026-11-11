"""Sleep sensors: last night, refreshed after each watch sync."""

import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.my_garmin.api import GarminConnectionError
from custom_components.my_garmin.const import DOMAIN
from tests.integration.conftest import PROFILE_ID, load_fixture


@pytest.fixture
def night(mock_client: MagicMock) -> dict[str, Any]:
    data = load_fixture("sleep.json")
    mock_client.get_daily_sleep.return_value = data
    return data


@pytest.fixture
def summary(mock_client: MagicMock) -> dict[str, Any]:
    data = load_fixture("daily_summary.json")
    data["lastSyncTimestampGMT"] = "2026-11-10T06:15:00.0"
    mock_client.get_daily_summary.return_value = data
    return data


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _state(hass: HomeAssistant, key: str) -> State:
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{PROFILE_ID}_{key}"
    )
    assert entity_id is not None, key
    state = hass.states.get(entity_id)
    assert state is not None, key
    return state


def _utc_iso(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=UTC).isoformat()


@pytest.mark.usefixtures("summary")
async def test_sleep_sensors_read_last_night(
    hass: HomeAssistant, night: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    durations = {
        "sleep_duration": night["sleepTimeSeconds"] / 3600,  # shown in hours
        "deep_sleep": night["deepSleepSeconds"] / 60,  # stages in minutes
        "light_sleep": night["lightSleepSeconds"] / 60,
        "rem_sleep": night["remSleepSeconds"] / 60,
        "awake_time": night["awakeSleepSeconds"] / 60,
    }
    for key, value in durations.items():
        assert float(_state(hass, key).state) == pytest.approx(value), key
    overall = night["sleepScores"]["overall"]
    assert float(_state(hass, "sleep_score").state) == overall["value"]
    assert _state(hass, "sleep_quality").state == overall["qualifierKey"].lower()
    assert _state(hass, "bedtime").state == _utc_iso(night["sleepStartTimestampGMT"])
    assert _state(hass, "wake_time").state == _utc_iso(night["sleepEndTimestampGMT"])
    # Every sleep sensor tells which night it shows.
    attributes = _state(hass, "sleep_duration").attributes
    assert attributes["night_of"] == night["calendarDate"]


@pytest.mark.usefixtures("summary", "night")
async def test_sleep_units_and_classes(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    duration = _state(hass, "sleep_duration").attributes
    assert duration["device_class"] == "duration"
    assert duration["unit_of_measurement"] == "h"
    assert _state(hass, "deep_sleep").attributes["unit_of_measurement"] == "min"
    quality = _state(hass, "sleep_quality").attributes
    assert quality["device_class"] == "enum"
    assert quality["options"] == ["excellent", "good", "fair", "poor"]
    assert _state(hass, "bedtime").attributes["device_class"] == "timestamp"


@pytest.mark.usefixtures("summary")
@pytest.mark.parametrize(
    "empty", [{}, {"sleepTimeSeconds": None}, {"dailySleep": None}]
)
async def test_last_night_is_kept_until_the_morning_sync(
    hass: HomeAssistant,
    mock_client: MagicMock,
    night: dict[str, Any],
    config_entry: MockConfigEntry,
    empty: dict[str, Any],
) -> None:
    await _setup(hass, config_entry)

    # Before the watch syncs, Garmin returns an empty night for today.
    mock_client.get_daily_sleep.return_value = empty
    await config_entry.runtime_data.sleep.async_refresh()

    expected = night["sleepTimeSeconds"] / 3600
    assert float(_state(hass, "sleep_duration").state) == pytest.approx(expected)
    assert (
        _state(hass, "sleep_duration").attributes["night_of"] == (night["calendarDate"])
    )


@pytest.mark.usefixtures("summary")
async def test_no_night_yet_is_unknown(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    assert _state(hass, "sleep_duration").state == STATE_UNKNOWN
    assert _state(hass, "sleep_quality").state == STATE_UNKNOWN


@pytest.mark.usefixtures("summary")
async def test_unknown_quality_is_logged_once(
    hass: HomeAssistant,
    mock_client: MagicMock,
    night: dict[str, Any],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    night["sleepScores"]["overall"]["qualifierKey"] = "SUPERB"
    mock_client.get_daily_sleep.return_value = night
    caplog.set_level(logging.WARNING)

    await _setup(hass, config_entry)
    await config_entry.runtime_data.sleep.async_refresh()
    await hass.async_block_till_done()

    assert _state(hass, "sleep_quality").state == STATE_UNKNOWN
    assert caplog.text.count("SUPERB") == 1


async def test_a_new_watch_sync_refreshes_sleep(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    night: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    del night
    await _setup(hass, config_entry)
    daily = config_entry.runtime_data.daily
    calls = mock_client.get_daily_sleep.await_count

    # Same sync time: nothing new to download.
    await daily.async_refresh()
    await hass.async_block_till_done()
    assert mock_client.get_daily_sleep.await_count == calls

    summary["lastSyncTimestampGMT"] = "2026-11-10T07:40:00.0"
    await daily.async_refresh()
    await hass.async_block_till_done()
    assert mock_client.get_daily_sleep.await_count == calls + 1


async def test_sleep_failure_does_not_block_daily_sensors(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    mock_client.get_daily_sleep.side_effect = GarminConnectionError("down")

    await _setup(hass, config_entry)

    assert _state(hass, "steps").state == str(summary["totalSteps"])
    assert _state(hass, "sleep_duration").state == STATE_UNAVAILABLE
