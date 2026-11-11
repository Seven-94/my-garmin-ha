"""SpO2 and acclimation sensors: the day's blood oxygen saturation, refreshed per sync.

`spo2_synthetic.json` is built from the API schema, with synthetic values. The
altitude comes from the daily summary: its fixture carries null, as for most
users, so the tests set a synthetic one.
"""

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

READINGS = {
    "acclimation_spo2_average": "averageSpO2",
    "acclimation_spo2_lowest": "lowestSpO2",
    "acclimation_spo2_latest": "latestSpO2",
    "acclimation_spo2_seven_day_average": "lastSevenDaysAvgSpO2",
    "acclimation_spo2_sleep_average": "avgSleepSpO2",
    "acclimation_spo2_tomorrow_sleep_average": "avgTomorrowSleepSpO2",
}
SPO2_KEYS = (*READINGS, "acclimation_altitude")
DISABLED_BY_DEFAULT = {
    "acclimation_spo2_tomorrow_sleep_average",
    "acclimation_altitude",
}
# Synthetic: the summary's altitude is null for most users.
ALTITUDE = 1850.0


@pytest.fixture
def spo2(mock_client: MagicMock) -> dict[str, Any]:
    data = load_fixture("spo2_synthetic.json")
    mock_client.get_spo2.return_value = data
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


async def _sync_again(
    hass: HomeAssistant, entry: MockConfigEntry, summary: dict[str, Any]
) -> None:
    summary["lastSyncTimestampGMT"] = "2026-11-10T07:40:00.0"
    await entry.runtime_data.daily.async_refresh()
    await hass.async_block_till_done()


def _utc(value: str) -> str:
    return datetime.fromisoformat(value).replace(tzinfo=UTC).isoformat()


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
async def test_spo2_sensors_read_the_day(
    hass: HomeAssistant, spo2: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    for key, field in READINGS.items():
        assert float(_state(hass, key).state) == spo2[field], key
        assert _state(hass, key).attributes["day"] == spo2["calendarDate"], key


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
async def test_latest_reading_and_sleep_carry_their_times(
    hass: HomeAssistant, spo2: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    latest = _state(hass, "acclimation_spo2_latest").attributes
    assert latest["measured_at"] == _utc(spo2["latestSpO2TimestampGMT"])
    sleep = _state(hass, "acclimation_spo2_sleep_average").attributes
    assert sleep["sleep_start"] == _utc(spo2["sleepStartTimestampGMT"])
    assert sleep["sleep_end"] == _utc(spo2["sleepEndTimestampGMT"])


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
async def test_tomorrow_sleep_and_reporting_window_are_attributes(
    hass: HomeAssistant, spo2: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    average = _state(hass, "acclimation_spo2_average").attributes
    assert average["window_start"] == _utc(spo2["startTimestampGMT"])
    assert average["window_end"] == _utc(spo2["endTimestampGMT"])
    tomorrow = _state(hass, "acclimation_spo2_tomorrow_sleep_average").attributes
    start = spo2["tomorrowSleepStartTimestampGMT"]
    end = spo2["tomorrowSleepEndTimestampGMT"]
    assert tomorrow["tomorrow_sleep_start"] == _utc(start)
    assert tomorrow["tomorrow_sleep_end"] == _utc(end)


@pytest.mark.usefixtures("summary", "spo2", "entity_registry_enabled_by_default")
async def test_spo2_units_and_classes(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    for key in READINGS:
        attributes = _state(hass, key).attributes
        assert attributes["unit_of_measurement"] == "%", key
        assert attributes["state_class"] == "measurement", key
        assert "device_class" not in attributes, key
    altitude = _state(hass, "acclimation_altitude").attributes
    assert altitude["unit_of_measurement"] == "m"
    assert altitude["state_class"] == "measurement"
    assert altitude["device_class"] == "distance"


@pytest.mark.usefixtures("summary", "spo2")
async def test_tomorrow_sleep_and_altitude_are_disabled_by_default(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    registry = er.async_get(hass)

    for key in SPO2_KEYS:
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{PROFILE_ID}_{key}"
        )
        assert entity_id is not None, key
        entry = registry.async_get(entity_id)
        assert entry is not None, key
        disabled = entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert disabled is (key in DISABLED_BY_DEFAULT), key


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
async def test_no_spo2_is_unknown_without_logs(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # What the client returns for Garmin's null body.
    mock_client.get_spo2.return_value = {}
    caplog.set_level(logging.WARNING)

    await _setup(hass, config_entry)
    await _sync_again(hass, config_entry, summary)
    await config_entry.runtime_data.spo2.async_refresh()
    await hass.async_block_till_done()

    for key in SPO2_KEYS:
        assert _state(hass, key).state == STATE_UNKNOWN, key
    assert not [r for r in caplog.records if r.name.startswith("custom_components")]


@pytest.mark.usefixtures("summary")
@pytest.mark.parametrize(
    "empty",
    [{}, {"averageSpO2": None, "latestSpO2": None}],
)
async def test_last_spo2_day_is_kept_until_the_next_one(
    hass: HomeAssistant,
    mock_client: MagicMock,
    spo2: dict[str, Any],
    config_entry: MockConfigEntry,
    empty: dict[str, Any],
) -> None:
    await _setup(hass, config_entry)

    mock_client.get_spo2.return_value = empty
    await config_entry.runtime_data.spo2.async_refresh()

    assert float(_state(hass, "acclimation_spo2_average").state) == spo2["averageSpO2"]


async def test_a_new_watch_sync_refreshes_spo2(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    spo2: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    del spo2
    await _setup(hass, config_entry)
    calls = mock_client.get_spo2.await_count

    await config_entry.runtime_data.daily.async_refresh()
    await hass.async_block_till_done()
    assert mock_client.get_spo2.await_count == calls

    await _sync_again(hass, config_entry, summary)
    assert mock_client.get_spo2.await_count == calls + 1


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_altitude_comes_from_the_daily_summary(
    hass: HomeAssistant,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    summary["averageMonitoringEnvironmentAltitude"] = ALTITUDE

    await _setup(hass, config_entry)

    assert float(_state(hass, "acclimation_altitude").state) == ALTITUDE


async def test_spo2_failure_does_not_block_other_sensors(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    mock_client.get_spo2.side_effect = GarminConnectionError("down")

    await _setup(hass, config_entry)

    assert _state(hass, "steps").state == str(summary["totalSteps"])
    assert _state(hass, "acclimation_spo2_average").state == STATE_UNAVAILABLE
