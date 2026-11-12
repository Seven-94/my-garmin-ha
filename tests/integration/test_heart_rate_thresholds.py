"""Abnormal heart rate thresholds: the alert settings, refreshed every six hours.

`heart_rate_synthetic.json` is built from the API schema, with synthetic values.
The day's resting, lowest and highest heart rates of the same response are not
read: the daily summary sensors already show them.
"""

import logging
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.my_garmin.api import GarminConnectionError
from custom_components.my_garmin.const import DOMAIN
from custom_components.my_garmin.sensor import HEART_RATE_THRESHOLD_SENSORS
from tests.integration.conftest import PROFILE_ID, load_fixture

# Sensor key, and the Garmin field it shows, in bpm.
THRESHOLDS = {
    "abnormal_heart_rate_high_threshold": "abnormalHrThresholdValue",
    "abnormal_heart_rate_low_threshold": "lowAbnormalHrThresholdValue",
}


@pytest.fixture
def heart_rate(mock_client: MagicMock) -> dict[str, Any]:
    data = load_fixture("heart_rate_synthetic.json")
    mock_client.get_daily_heart_rate.return_value = data
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
async def test_thresholds_show_the_alert_settings(
    hass: HomeAssistant, heart_rate: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    for key, field in THRESHOLDS.items():
        assert float(_state(hass, key).state) == heart_rate[field], key


@pytest.mark.usefixtures("summary", "heart_rate")
async def test_today_is_requested(
    hass: HomeAssistant, mock_client: MagicMock, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    mock_client.get_daily_heart_rate.assert_awaited_with(dt_util.now().date())


@pytest.mark.usefixtures("summary", "heart_rate")
async def test_thresholds_are_enabled_diagnostic_settings_in_bpm(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    registry = er.async_get(hass)

    for key in THRESHOLDS:
        attributes = _state(hass, key).attributes
        assert attributes["unit_of_measurement"] == "bpm", key
        # A setting, not a measurement: no long-term statistics.
        assert "state_class" not in attributes, key
        entry = registry.async_get(_entity_id(hass, key))
        assert entry is not None, key
        assert entry.entity_category is er.EntityCategory.DIAGNOSTIC, key
        assert entry.disabled_by is None, key


def test_daily_heart_rates_are_not_duplicated() -> None:
    # Resting, lowest, highest and 7-day resting heart rates come from the
    # daily summary: this response adds the thresholds only.
    assert {d.key for d in HEART_RATE_THRESHOLD_SENSORS} == set(THRESHOLDS)


@pytest.mark.usefixtures("summary")
@pytest.mark.parametrize(
    "answer",
    [
        {},
        {"abnormalHrThresholdValue": None, "lowAbnormalHrThresholdValue": None},
        {"abnormalHrThresholdValue": 0, "lowAbnormalHrThresholdValue": 0},
    ],
)
async def test_no_threshold_is_unknown_without_logs(
    hass: HomeAssistant,
    mock_client: MagicMock,
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
    answer: dict[str, Any],
) -> None:
    mock_client.get_daily_heart_rate.return_value = answer
    caplog.set_level(logging.WARNING)

    await _setup(hass, config_entry)
    await config_entry.runtime_data.heart_rate_thresholds.async_refresh()
    await hass.async_block_till_done()

    for key in THRESHOLDS:
        assert _state(hass, key).state == STATE_UNKNOWN, key
    assert not [r for r in caplog.records if r.name.startswith("custom_components")]


async def test_thresholds_poll_every_six_hours_not_on_watch_sync(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    heart_rate: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    del heart_rate
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data.heart_rate_thresholds
    assert coordinator.update_interval == timedelta(hours=6)
    calls = mock_client.get_daily_heart_rate.await_count

    summary["lastSyncTimestampGMT"] = "2026-11-10T07:40:00.0"
    await config_entry.runtime_data.daily.async_refresh()
    await hass.async_block_till_done()

    assert mock_client.get_daily_heart_rate.await_count == calls


async def test_threshold_failure_does_not_block_other_sensors(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    mock_client.get_daily_heart_rate.side_effect = GarminConnectionError("down")

    await _setup(hass, config_entry)

    assert _state(hass, "steps").state == str(summary["totalSteps"])
    for key in THRESHOLDS:
        assert _state(hass, key).state == STATE_UNAVAILABLE, key
