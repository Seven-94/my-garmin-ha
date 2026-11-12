"""Menstrual cycle sensors: today's cycle summary, every hour.

`menstrual_cycle_synthetic.json` is built from the API schema, with synthetic
values. The day view (logged symptoms, flow, mood) is never requested, and the
diagnostics drop the whole cycle.
"""

import json
import logging
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.my_garmin.api import GarminConnectionError
from custom_components.my_garmin.const import DOMAIN
from tests.integration.conftest import PROFILE_ID, load_fixture

# Sensor key, and the Garmin field it shows: a position in the cycle.
POSITIONS = {
    "menstrual_cycle_phase": "currentPhase",
    "menstrual_cycle_day": "dayInCycle",
    "menstrual_fertile_window_start": "fertileWindowStart",
    "menstrual_luteal_phase_start": "lutealPhaseStart",
}
# Sensor key, and the Garmin field it shows: a number of days.
DURATIONS = {
    "menstrual_days_until_next_phase": "daysUntilNextPhase",
    "menstrual_phase_length": "lengthOfCurrentPhase",
    "menstrual_predicted_cycle_length": "predictedCycleLength",
    "menstrual_period_length": "periodLength",
    "menstrual_fertile_window_length": "lengthOfFertileWindow",
}
PREGNANCY_WEEK = "menstrual_pregnancy_week"
SENSOR_KEYS = {*POSITIONS, *DURATIONS, PREGNANCY_WEEK}
PREDICTED = "menstrual_cycle_predicted"


@pytest.fixture
def cycle(mock_client: MagicMock) -> dict[str, Any]:
    data = load_fixture("menstrual_cycle_synthetic.json")
    mock_client.get_menstrual_cycle.return_value = data
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


def _entity_id(hass: HomeAssistant, key: str, platform: str = "sensor") -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{PROFILE_ID}_{key}"
    )
    assert entity_id is not None, key
    return entity_id


def _state(hass: HomeAssistant, key: str, platform: str = "sensor") -> State:
    state = hass.states.get(_entity_id(hass, key, platform))
    assert state is not None, key
    return state


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
async def test_cycle_sensors_read_today(
    hass: HomeAssistant, cycle: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    for key, field in {**POSITIONS, **DURATIONS}.items():
        assert float(_state(hass, key).state) == cycle[field], key
    assert _state(hass, PREDICTED, "binary_sensor").state == STATE_ON


@pytest.mark.usefixtures("summary", "cycle")
async def test_today_is_requested(
    hass: HomeAssistant, mock_client: MagicMock, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    mock_client.get_menstrual_cycle.assert_awaited_with(dt_util.now().date())


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
async def test_cycle_day_carries_the_cycle_dates(
    hass: HomeAssistant, cycle: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    attributes = _state(hass, "menstrual_cycle_day").attributes
    assert attributes["cycle_start"] == cycle["startDate"]
    assert attributes["cycle_end"] is None
    assert attributes["cycle_type"] == "regular"
    assert attributes["predicted_ovulation"] == cycle["predictedOvulationDate"]
    assert attributes["skin_temperature_used"] is False


@pytest.mark.usefixtures("summary", "cycle", "entity_registry_enabled_by_default")
async def test_units_and_classes(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    for key in DURATIONS:
        attributes = _state(hass, key).attributes
        assert attributes["unit_of_measurement"] == "d", key
        assert attributes["device_class"] == "duration", key
        assert "state_class" not in attributes, key
    for key in (*POSITIONS, PREGNANCY_WEEK):
        attributes = _state(hass, key).attributes
        # A position in the cycle, not a quantity: no unit, no statistics.
        assert "unit_of_measurement" not in attributes, key
        assert "state_class" not in attributes, key


@pytest.mark.usefixtures("summary", "cycle")
async def test_cycle_entities_are_disabled_by_default(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    registry = er.async_get(hass)

    entities = [(key, "sensor") for key in SENSOR_KEYS]
    for key, platform in [*entities, (PREDICTED, "binary_sensor")]:
        entry = registry.async_get(_entity_id(hass, key, platform))
        assert entry is not None, key
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION, key


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
async def test_pregnancy_week_only_during_a_pregnancy(
    hass: HomeAssistant,
    mock_client: MagicMock,
    cycle: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    await _setup(hass, config_entry)
    assert _state(hass, PREGNANCY_WEEK).state == STATE_UNKNOWN

    cycle.update(
        pregnancyCycle=True,
        numberOfWeek=14,
        dueDate="2027-05-20",
        deliveryDate=None,
    )
    mock_client.get_menstrual_cycle.return_value = cycle
    await config_entry.runtime_data.menstrual_cycle.async_refresh()
    await hass.async_block_till_done()

    week = _state(hass, PREGNANCY_WEEK)
    assert week.state == "14"
    assert week.attributes["due_date"] == "2027-05-20"
    assert week.attributes["delivery_date"] is None


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
async def test_a_week_number_outside_a_pregnancy_is_unknown(
    hass: HomeAssistant, cycle: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    cycle["numberOfWeek"] = 3

    await _setup(hass, config_entry)

    assert _state(hass, PREGNANCY_WEEK).state == STATE_UNKNOWN


@pytest.mark.usefixtures("summary", "cycle")
async def test_diagnostics_hold_nothing_of_the_cycle(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    config_entry: MockConfigEntry,
) -> None:
    await _setup(hass, config_entry)

    result: dict[str, Any] = await get_diagnostics_for_config_entry(
        hass, hass_client, config_entry
    )

    assert result["coordinators"]["menstrual_cycle"]["data"] == {}
    dumped = json.dumps(result)
    for field in ("dayInCycle", "periodLength", "SYNTHETIC_FEEDBACK_PHRASE"):
        assert field not in dumped, field


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
async def test_no_cycle_tracked_is_unknown_without_logs(
    hass: HomeAssistant,
    mock_client: MagicMock,
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # What the client returns for Garmin's null body.
    mock_client.get_menstrual_cycle.return_value = {}
    caplog.set_level(logging.WARNING)

    await _setup(hass, config_entry)
    await config_entry.runtime_data.menstrual_cycle.async_refresh()
    await hass.async_block_till_done()

    for key in SENSOR_KEYS:
        assert _state(hass, key).state == STATE_UNKNOWN, key
    assert _state(hass, PREDICTED, "binary_sensor").state == STATE_UNKNOWN
    assert not [r for r in caplog.records if r.name.startswith("custom_components")]


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
async def test_a_logged_cycle_is_not_predicted(
    hass: HomeAssistant, cycle: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    cycle["predictedCycle"] = False

    await _setup(hass, config_entry)

    assert _state(hass, PREDICTED, "binary_sensor").state == STATE_OFF


async def test_cycle_polls_hourly_not_on_watch_sync(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    cycle: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    del cycle
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data.menstrual_cycle
    assert coordinator.update_interval == timedelta(hours=1)
    calls = mock_client.get_menstrual_cycle.await_count

    summary["lastSyncTimestampGMT"] = "2026-11-10T07:40:00.0"
    await config_entry.runtime_data.daily.async_refresh()
    await hass.async_block_till_done()

    assert mock_client.get_menstrual_cycle.await_count == calls


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_cycle_failure_does_not_block_other_sensors(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    mock_client.get_menstrual_cycle.side_effect = GarminConnectionError("down")

    await _setup(hass, config_entry)

    assert _state(hass, "steps").state == str(summary["totalSteps"])
    for key in SENSOR_KEYS:
        assert _state(hass, key).state == STATE_UNAVAILABLE, key
    assert _state(hass, PREDICTED, "binary_sensor").state == STATE_UNAVAILABLE
