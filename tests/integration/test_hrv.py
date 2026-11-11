"""HRV sensors: last night's heart rate variability, refreshed after each sync.

No test account records HRV: the fixture is built from the API schema, with
synthetic values.
"""

import logging
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

HRV_KEYS = ("hrv_last_night", "hrv_weekly_average", "hrv_last_night_high", "hrv_status")


@pytest.fixture
def hrv(mock_client: MagicMock) -> dict[str, Any]:
    data = load_fixture("hrv_synthetic.json")
    mock_client.get_hrv.return_value = data
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


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
async def test_hrv_sensors_read_last_night(
    hass: HomeAssistant, hrv: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    night = hrv["hrvSummary"]
    assert float(_state(hass, "hrv_last_night").state) == night["lastNightAvg"]
    assert float(_state(hass, "hrv_weekly_average").state) == night["weeklyAvg"]
    assert (
        float(_state(hass, "hrv_last_night_high").state) == night["lastNight5MinHigh"]
    )
    assert _state(hass, "hrv_status").state == night["status"].lower()
    for key in HRV_KEYS:
        assert _state(hass, key).attributes["night_of"] == night["calendarDate"], key


@pytest.mark.usefixtures("summary")
async def test_hrv_status_carries_the_baseline(
    hass: HomeAssistant, hrv: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    night = hrv["hrvSummary"]
    baseline = night["baseline"]
    attributes = _state(hass, "hrv_status").attributes
    assert attributes["low_upper"] == baseline["lowUpper"]
    assert attributes["balanced_low"] == baseline["balancedLow"]
    assert attributes["balanced_upper"] == baseline["balancedUpper"]
    assert attributes["marker_value"] == baseline["markerValue"]
    assert attributes["feedback"] == night["feedbackPhrase"].lower()


@pytest.mark.usefixtures("summary", "hrv", "entity_registry_enabled_by_default")
async def test_hrv_units_and_classes(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    for key in ("hrv_last_night", "hrv_weekly_average", "hrv_last_night_high"):
        attributes = _state(hass, key).attributes
        assert attributes["unit_of_measurement"] == "ms", key
        assert attributes["state_class"] == "measurement", key
        # Not a duration: converting HRV to seconds or hours makes no sense.
        assert "device_class" not in attributes, key
    status = _state(hass, "hrv_status").attributes
    assert status["device_class"] == "enum"
    assert status["options"] == ["none", "poor", "low", "unbalanced", "balanced"]


@pytest.mark.usefixtures("summary", "hrv")
async def test_hrv_5_minute_high_is_disabled_by_default(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    registry = er.async_get(hass)

    for key in HRV_KEYS:
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{PROFILE_ID}_{key}"
        )
        assert entity_id is not None, key
        entry = registry.async_get(entity_id)
        assert entry is not None, key
        disabled = entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert disabled is (key == "hrv_last_night_high"), key


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
async def test_no_hrv_is_unknown_without_logs(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # What the client returns for Garmin's null body.
    mock_client.get_hrv.return_value = {}
    caplog.set_level(logging.WARNING)

    await _setup(hass, config_entry)
    await _sync_again(hass, config_entry, summary)
    await config_entry.runtime_data.hrv.async_refresh()
    await hass.async_block_till_done()

    for key in HRV_KEYS:
        assert _state(hass, key).state == STATE_UNKNOWN, key
    assert not [r for r in caplog.records if r.name.startswith("custom_components")]


@pytest.mark.usefixtures("summary")
async def test_invalid_status_is_unknown_without_warning(
    hass: HomeAssistant,
    hrv: dict[str, Any],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hrv["hrvSummary"]["status"] = "INVALID"
    caplog.set_level(logging.WARNING)

    await _setup(hass, config_entry)

    assert _state(hass, "hrv_status").state == STATE_UNKNOWN
    assert "INVALID" not in caplog.text


@pytest.mark.usefixtures("summary")
@pytest.mark.parametrize(
    "empty",
    [{}, {"hrvSummary": None}, {"hrvSummary": {"lastNightAvg": None}}],
)
async def test_last_hrv_night_is_kept_until_the_next_one(
    hass: HomeAssistant,
    mock_client: MagicMock,
    hrv: dict[str, Any],
    config_entry: MockConfigEntry,
    empty: dict[str, Any],
) -> None:
    await _setup(hass, config_entry)

    mock_client.get_hrv.return_value = empty
    await config_entry.runtime_data.hrv.async_refresh()

    night = hrv["hrvSummary"]
    assert float(_state(hass, "hrv_last_night").state) == night["lastNightAvg"]


async def test_a_new_watch_sync_refreshes_hrv(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    hrv: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    del hrv
    await _setup(hass, config_entry)
    calls = mock_client.get_hrv.await_count

    await config_entry.runtime_data.daily.async_refresh()
    await hass.async_block_till_done()
    assert mock_client.get_hrv.await_count == calls

    await _sync_again(hass, config_entry, summary)
    assert mock_client.get_hrv.await_count == calls + 1


async def test_hrv_failure_does_not_block_other_sensors(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    mock_client.get_hrv.side_effect = GarminConnectionError("down")

    await _setup(hass, config_entry)

    assert _state(hass, "steps").state == str(summary["totalSteps"])
    assert _state(hass, "hrv_last_night").state == STATE_UNAVAILABLE
