"""Daily summary sensors and their coordinator."""

from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.my_garmin.api import (
    GarminAuthError,
    GarminConnectionError,
    GarminRateLimitError,
)
from custom_components.my_garmin.const import DOMAIN
from tests.integration.conftest import PROFILE_ID, load_fixture


@pytest.fixture
def summary(mock_client: MagicMock) -> dict[str, Any]:
    data = load_fixture("daily_summary.json")
    mock_client.get_daily_summary.return_value = data
    return data


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _entity_id(hass: HomeAssistant, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{PROFILE_ID}_{key}"
    )
    assert entity_id is not None, key
    return entity_id


def _state(hass: HomeAssistant, key: str) -> State:
    state = hass.states.get(_entity_id(hass, key))
    assert state is not None, key
    return state


@pytest.mark.usefixtures("summary")
async def test_sensors_read_the_daily_summary(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    data = load_fixture("daily_summary.json")
    expected = {
        "steps": data["totalSteps"],
        "step_goal": data["dailyStepGoal"],
        "distance": data["totalDistanceMeters"] / 1000,  # shown in km
        "active_calories": data["activeKilocalories"],
        # Vigorous minutes count double.
        "intensity_minutes": data["moderateIntensityMinutes"]
        + 2 * data["vigorousIntensityMinutes"],
        "floors_ascended": data["floorsAscended"],
        "resting_heart_rate": data["restingHeartRate"],
        "average_stress": data["averageStressLevel"],
        "body_battery": data["bodyBatteryMostRecentValue"],
        "body_battery_highest": data["bodyBatteryHighestValue"],
        "body_battery_lowest": data["bodyBatteryLowestValue"],
        "respiration": data["avgWakingRespirationValue"],
        "spo2": data["averageSpo2"],
    }
    for key, value in expected.items():
        assert float(_state(hass, key).state) == pytest.approx(value), key
    # No sync timestamp in this capture.
    assert _state(hass, "last_sync").state == STATE_UNKNOWN


@pytest.mark.usefixtures("summary")
async def test_sensor_units_and_classes(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    steps = _state(hass, "steps").attributes
    assert steps["unit_of_measurement"] == "steps"
    assert steps["state_class"] == "total_increasing"
    distance = _state(hass, "distance").attributes
    assert distance["device_class"] == "distance"
    assert distance["unit_of_measurement"] == "km"
    heart = _state(hass, "resting_heart_rate").attributes
    assert heart["unit_of_measurement"] == "bpm"
    assert heart["state_class"] == "measurement"
    assert _state(hass, "intensity_minutes").attributes["device_class"] == "duration"
    assert _state(hass, "last_sync").attributes["device_class"] == "timestamp"


@pytest.mark.usefixtures("summary")
async def test_noisy_sensors_are_disabled_by_default(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    registry = er.async_get(hass)

    for key in ("min_heart_rate", "max_heart_rate", "total_calories"):
        entry = registry.async_get(_entity_id(hass, key))
        assert entry is not None
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION, key


async def test_not_measured_values_are_unknown(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    summary.update(
        restingHeartRate=0,  # Garmin's "no reading"
        averageStressLevel=-1,  # not enough data
        bodyBatteryMostRecentValue=None,
        totalSteps=0,  # a real zero
        lastSyncTimestampGMT="2026-11-10T06:15:00.0",
    )
    mock_client.get_daily_summary.return_value = summary

    await _setup(hass, config_entry)

    assert _state(hass, "resting_heart_rate").state == STATE_UNKNOWN
    assert _state(hass, "average_stress").state == STATE_UNKNOWN
    assert _state(hass, "body_battery").state == STATE_UNKNOWN
    assert _state(hass, "steps").state == "0"
    assert _state(hass, "last_sync").state == "2026-11-10T06:15:00+00:00"


@pytest.mark.usefixtures("summary")
async def test_one_device_for_the_account(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, str(PROFILE_ID)), config_entry.entry_id
    )
    assert device is not None
    assert device.name == "My Garmin"
    assert device.manufacturer == "Garmin"
    assert device.entry_type is dr.DeviceEntryType.SERVICE


@pytest.mark.parametrize(
    "error", [GarminConnectionError("down"), GarminRateLimitError(60.0)]
)
async def test_failed_refresh_makes_sensors_unavailable_until_recovery(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
    error: Exception,
) -> None:
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data.daily

    mock_client.get_daily_summary.side_effect = error
    await coordinator.async_refresh()
    assert _state(hass, "steps").state == STATE_UNAVAILABLE

    mock_client.get_daily_summary.side_effect = None
    mock_client.get_daily_summary.return_value = summary
    await coordinator.async_refresh()
    assert _state(hass, "steps").state == str(summary["totalSteps"])


async def test_rate_limit_backs_off_for_at_least_30_minutes(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    del summary
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data.daily

    mock_client.get_daily_summary.side_effect = GarminRateLimitError(60.0)
    await coordinator.async_refresh()

    assert coordinator.last_exception.retry_after >= 30 * 60


async def test_rejected_key_during_refresh_starts_reauth(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    del summary
    await _setup(hass, config_entry)

    mock_client.get_daily_summary.side_effect = GarminAuthError("rejected")
    await config_entry.runtime_data.daily.async_refresh()
    await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-11-10T06:15:00.0", "2026-11-10T06:15:00+00:00"),
        ("2026-11-10T07:15:00+01:00", "2026-11-10T06:15:00+00:00"),
        ("not a date", STATE_UNKNOWN),
        (None, STATE_UNKNOWN),
    ],
)
async def test_last_sync_is_read_as_utc(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
    raw: str | None,
    expected: str,
) -> None:
    summary["lastSyncTimestampGMT"] = raw
    mock_client.get_daily_summary.return_value = summary

    await _setup(hass, config_entry)

    assert _state(hass, "last_sync").state == expected
