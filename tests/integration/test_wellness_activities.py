"""Wellness activity sensor: the latest health monitoring session.

`wellness_activities_synthetic.json` is built from the API schema, with
synthetic values. Session names and free-text notes never leave the
coordinator.
"""

import json
import logging
from datetime import UTC, datetime, timedelta
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
from tests.integration.conftest import PROFILE_ID, load_fixture

KEY = "wellness_latest_session"
PRIVATE_TEXT = ("Synthetic session name", "Synthetic free-text note")


def _utc(value: str) -> str:
    return datetime.fromisoformat(value).replace(tzinfo=UTC).isoformat()


@pytest.fixture
def sessions(mock_client: MagicMock) -> list[dict[str, Any]]:
    data = load_fixture("wellness_activities_synthetic.json")
    mock_client.get_wellness_activities.return_value = data
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


def _state(hass: HomeAssistant, key: str = KEY) -> State:
    state = hass.states.get(_entity_id(hass, key))
    assert state is not None, key
    return state


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
async def test_latest_session_is_the_most_recent_start(
    hass: HomeAssistant, sessions: list[dict[str, Any]], config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    latest = sessions[1]
    state = _state(hass)
    assert state.state == _utc(latest["startTimestampGMT"])
    assert state.attributes["device_class"] == "timestamp"
    assert state.attributes["type"] == "health_monitoring"
    assert state.attributes["day"] == latest["calendarDate"]
    assert state.attributes["end"] == _utc(latest["endTimestampGMT"])
    assert state.attributes["duration_seconds"] == 120


@pytest.mark.usefixtures("summary", "sessions", "entity_registry_enabled_by_default")
async def test_metrics_and_zones_are_attributes(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    attributes = _state(hass).attributes
    assert attributes["metrics"] == {
        "heart_rate": {"min": 52.0, "avg": 58.5, "max": 68.0},
        "stress": {"min": 10.0, "avg": 20.0, "max": 35.0},
        "respiration": {"min": 12.0, "avg": 14.0, "max": 16.0},
        "spo2": {"min": 94.0, "avg": 97.0, "max": 99.0},
        "rmssd_hrv": {"min": 30.0, "avg": 45.0, "max": 60.0},
        "sdrr_hrv": {"min": 40.0, "avg": 55.0, "max": 75.0},
    }
    assert attributes["time_in_zones"] == [
        {"zone": 1, "seconds": 90, "low_boundary": 50},
        {"zone": 2, "seconds": 30, "low_boundary": 65},
    ]


@pytest.mark.usefixtures("summary", "sessions")
async def test_last_seven_days_are_requested(
    hass: HomeAssistant, mock_client: MagicMock, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    today = dt_util.now().date()
    mock_client.get_wellness_activities.assert_awaited_with(
        today - timedelta(days=6), today
    )


@pytest.mark.usefixtures("summary", "sessions")
async def test_session_sensor_is_disabled_by_default(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    entry = er.async_get(hass).async_get(_entity_id(hass, KEY))
    assert entry is not None
    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION


@pytest.mark.usefixtures("summary", "sessions", "entity_registry_enabled_by_default")
async def test_names_and_notes_never_leave_the_coordinator(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    await _setup(hass, config_entry)

    data = json.dumps(config_entry.runtime_data.wellness_activities.data)
    states = json.dumps(
        [state.as_dict() for state in hass.states.async_all()], default=str
    )
    for text in PRIVATE_TEXT:
        assert text not in data, text
        assert text not in states, text
        assert text not in caplog.text, text


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
async def test_latest_session_is_kept_when_the_week_has_none(
    hass: HomeAssistant,
    mock_client: MagicMock,
    sessions: list[dict[str, Any]],
    config_entry: MockConfigEntry,
) -> None:
    await _setup(hass, config_entry)

    mock_client.get_wellness_activities.return_value = []
    await config_entry.runtime_data.wellness_activities.async_refresh()
    await hass.async_block_till_done()

    assert _state(hass).state == _utc(sessions[1]["startTimestampGMT"])


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
@pytest.mark.parametrize(
    "answer",
    [[], [{"startTimestampGMT": None, "summaryTypeDataList": None}]],
    ids=["empty", "no-start"],
)
async def test_no_session_is_unknown_without_logs(
    hass: HomeAssistant,
    mock_client: MagicMock,
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
    answer: list[dict[str, Any]],
) -> None:
    mock_client.get_wellness_activities.return_value = answer
    caplog.set_level(logging.WARNING)

    await _setup(hass, config_entry)

    assert _state(hass).state == STATE_UNKNOWN
    assert not [r for r in caplog.records if r.name.startswith("custom_components")]


async def test_sessions_refresh_after_a_watch_sync(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    sessions: list[dict[str, Any]],
    config_entry: MockConfigEntry,
) -> None:
    del sessions
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data.wellness_activities
    assert coordinator.update_interval == timedelta(hours=2)
    calls = mock_client.get_wellness_activities.await_count

    summary["lastSyncTimestampGMT"] = "2026-11-10T07:40:00.0"
    await config_entry.runtime_data.daily.async_refresh()
    await hass.async_block_till_done()

    assert mock_client.get_wellness_activities.await_count == calls + 1


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_session_failure_does_not_block_other_sensors(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    mock_client.get_wellness_activities.side_effect = GarminConnectionError("down")

    await _setup(hass, config_entry)

    assert _state(hass, "steps").state == str(summary["totalSteps"])
    assert _state(hass).state == STATE_UNAVAILABLE
