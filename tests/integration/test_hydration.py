"""Hydration sensors: sweat loss and intake during activities, for today.

`hydration_synthetic.json` is built from the API schema, with synthetic values.
The day's intake and goal of the same response are not read: the daily summary
sensors already show them.
"""

import logging
from datetime import datetime, timedelta
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
from custom_components.my_garmin.sensor import HYDRATION_SENSORS
from tests.integration.conftest import PROFILE_ID, load_fixture

# Sensor key, and the Garmin field it shows, in mL.
VOLUMES = {
    "hydration_sweat_loss": "sweatLossInML",
    "hydration_activity_intake": "activityIntakeInML",
}


def _today() -> str:
    return dt_util.now().date().isoformat()


@pytest.fixture
def hydration(mock_client: MagicMock) -> dict[str, Any]:
    days = load_fixture("hydration_synthetic.json")
    # The coordinator asks for today and keeps today's entry only.
    days[0]["calendarDate"] = _today()
    mock_client.get_hydration.return_value = days
    return days[0]


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


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
async def test_hydration_sensors_read_today(
    hass: HomeAssistant, hydration: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    for key, field in VOLUMES.items():
        assert float(_state(hass, key).state) == hydration[field], key
        assert _state(hass, key).attributes["day"] == hydration["calendarDate"], key


@pytest.mark.usefixtures("summary", "hydration")
async def test_today_is_requested(
    hass: HomeAssistant, mock_client: MagicMock, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    today = dt_util.now().date()
    mock_client.get_hydration.assert_awaited_with(today, today)


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
async def test_last_entry_time_is_local_to_home_assistant(
    hass: HomeAssistant, hydration: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    naive = datetime.fromisoformat(hydration["lastEntryTimestampLocal"])
    expected = naive.replace(tzinfo=dt_util.get_default_time_zone()).isoformat()
    for key in VOLUMES:
        assert _state(hass, key).attributes["last_entry"] == expected, key


@pytest.mark.usefixtures("summary", "hydration", "entity_registry_enabled_by_default")
async def test_hydration_volumes_are_daily_totals_in_millilitres(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    for key in VOLUMES:
        attributes = _state(hass, key).attributes
        assert attributes["unit_of_measurement"] == "mL", key
        assert attributes["device_class"] == "volume", key
        assert attributes["state_class"] == "total_increasing", key


@pytest.mark.usefixtures("summary", "hydration")
async def test_hydration_sensors_are_disabled_by_default(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    registry = er.async_get(hass)

    for key in VOLUMES:
        entry = registry.async_get(_entity_id(hass, key))
        assert entry is not None, key
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION, key


def test_summary_intake_and_goal_are_not_duplicated() -> None:
    # Intake and goal come from the daily summary: this response adds the
    # sweat loss and the intake during activities only.
    assert {d.key for d in HYDRATION_SENSORS} == set(VOLUMES)


@pytest.mark.usefixtures("summary", "hydration", "entity_registry_enabled_by_default")
async def test_zero_is_a_real_total(
    hass: HomeAssistant, mock_client: MagicMock, config_entry: MockConfigEntry
) -> None:
    mock_client.get_hydration.return_value = [
        {"calendarDate": _today(), "sweatLossInML": 0.0, "activityIntakeInML": 0.0}
    ]

    await _setup(hass, config_entry)

    for key in VOLUMES:
        assert float(_state(hass, key).state) == 0, key
        assert "last_entry" not in _state(hass, key).attributes, key


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
@pytest.mark.parametrize(
    "answer",
    [
        [],
        [{"calendarDate": None}],
        [{"calendarDate": "2020-01-01", "sweatLossInML": 640.0}],
        [{"sweatLossInML": None, "activityIntakeInML": None}],
    ],
    ids=["empty", "no-date", "another-day", "null-values"],
)
async def test_no_hydration_today_is_unknown_without_logs(
    hass: HomeAssistant,
    mock_client: MagicMock,
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
    answer: list[dict[str, Any]],
) -> None:
    for day in answer:
        if "calendarDate" not in day:
            day["calendarDate"] = _today()
    mock_client.get_hydration.return_value = answer
    caplog.set_level(logging.WARNING)

    await _setup(hass, config_entry)
    await config_entry.runtime_data.hydration.async_refresh()
    await hass.async_block_till_done()

    for key in VOLUMES:
        assert _state(hass, key).state == STATE_UNKNOWN, key
    assert not [r for r in caplog.records if r.name.startswith("custom_components")]


async def test_hydration_polls_hourly_and_after_a_watch_sync(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    hydration: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    del hydration
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data.hydration
    assert coordinator.update_interval == timedelta(hours=1)
    calls = mock_client.get_hydration.await_count

    summary["lastSyncTimestampGMT"] = "2026-11-10T07:40:00.0"
    await config_entry.runtime_data.daily.async_refresh()
    await hass.async_block_till_done()

    assert mock_client.get_hydration.await_count == calls + 1


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_hydration_failure_does_not_block_other_sensors(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    mock_client.get_hydration.side_effect = GarminConnectionError("down")

    await _setup(hass, config_entry)

    assert _state(hass, "steps").state == str(summary["totalSteps"])
    for key in VOLUMES:
        assert _state(hass, key).state == STATE_UNAVAILABLE, key
