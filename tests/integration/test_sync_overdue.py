"""Sync overdue binary sensor, and the options that tune it and the polling."""

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
)

from custom_components.my_garmin.const import (
    CONF_DAILY_INTERVAL_MINUTES,
    CONF_SYNC_OVERDUE_HOURS,
    DOMAIN,
)
from tests.integration.conftest import PROFILE_ID, load_fixture


@pytest.fixture(autouse=True)
def _frozen_noon(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    del hass  # requested so that Home Assistant's time zone is set first
    freezer.move_to(datetime(2026, 11, 11, 12, tzinfo=dt_util.get_default_time_zone()))


@pytest.fixture
def summary(mock_client: MagicMock) -> dict[str, Any]:
    data = load_fixture("daily_summary.json")
    mock_client.get_daily_summary.return_value = data
    return data


def _garmin_time(hours_ago: float) -> str:
    """A lastSyncTimestampGMT as Garmin writes it: UTC, no offset."""
    moment = dt_util.utcnow() - timedelta(hours=hours_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.0")


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _entity_id(hass: HomeAssistant) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{PROFILE_ID}_sync_overdue"
    )
    assert entity_id is not None
    return entity_id


def _overdue(hass: HomeAssistant) -> State:
    state = hass.states.get(_entity_id(hass))
    assert state is not None
    return state


async def _set_options(
    hass: HomeAssistant, entry: MockConfigEntry, **options: int
) -> None:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], options
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


async def test_recent_sync_is_not_overdue(
    hass: HomeAssistant, summary: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    summary["lastSyncTimestampGMT"] = _garmin_time(2)

    await _setup(hass, config_entry)

    state = _overdue(hass)
    assert state.state == STATE_OFF
    assert state.attributes["device_class"] == "problem"
    expected = dt_util.utcnow().replace(microsecond=0) - timedelta(hours=2)
    assert state.attributes["last_sync"] == expected.isoformat()
    assert state.attributes["threshold_hours"] == 24


async def test_old_sync_is_overdue(
    hass: HomeAssistant, summary: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    summary["lastSyncTimestampGMT"] = _garmin_time(25)

    await _setup(hass, config_entry)

    assert _overdue(hass).state == STATE_ON


async def test_no_sync_today_is_unknown_until_midnight_is_past_the_threshold(
    hass: HomeAssistant, summary: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    # Garmin sends null until the watch syncs today: it is noon here.
    summary["lastSyncTimestampGMT"] = None

    await _setup(hass, config_entry)
    assert _overdue(hass).state == STATE_UNKNOWN

    await _set_options(
        hass,
        config_entry,
        **{CONF_SYNC_OVERDUE_HOURS: 6, CONF_DAILY_INTERVAL_MINUTES: 10},
    )
    assert _overdue(hass).state == STATE_ON


async def test_last_known_sync_is_kept_after_midnight(
    hass: HomeAssistant,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    summary["lastSyncTimestampGMT"] = _garmin_time(1)
    await _setup(hass, config_entry)

    summary["lastSyncTimestampGMT"] = None
    await config_entry.runtime_data.daily.async_refresh()
    await hass.async_block_till_done()

    state = _overdue(hass)
    assert state.state == STATE_OFF
    expected = dt_util.utcnow().replace(microsecond=0) - timedelta(hours=1)
    assert state.attributes["last_sync"] == expected.isoformat()


async def test_last_sync_survives_a_restart(
    hass: HomeAssistant, summary: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    last_sync = dt_util.utcnow().replace(microsecond=0) - timedelta(hours=30)
    mock_restore_cache(
        hass,
        [
            State(
                "binary_sensor.my_garmin_watch_sync_overdue",
                STATE_ON,
                {"last_sync": last_sync.isoformat()},
            )
        ],
    )
    summary["lastSyncTimestampGMT"] = None

    await _setup(hass, config_entry)

    state = _overdue(hass)
    assert state.state == STATE_ON
    assert state.attributes["last_sync"] == last_sync.isoformat()


@pytest.mark.usefixtures("summary")
async def test_options_have_defaults(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    schema = result["data_schema"]
    assert schema is not None
    defaults = {str(key): key.default() for key in schema.schema}
    assert defaults == {CONF_SYNC_OVERDUE_HOURS: 24, CONF_DAILY_INTERVAL_MINUTES: 10}


async def test_options_reload_the_entry(
    hass: HomeAssistant, summary: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    summary["lastSyncTimestampGMT"] = _garmin_time(5)
    await _setup(hass, config_entry)
    assert _overdue(hass).state == STATE_OFF

    await _set_options(
        hass,
        config_entry,
        **{CONF_SYNC_OVERDUE_HOURS: 4, CONF_DAILY_INTERVAL_MINUTES: 30},
    )

    assert config_entry.options == {
        CONF_SYNC_OVERDUE_HOURS: 4,
        CONF_DAILY_INTERVAL_MINUTES: 30,
    }
    assert config_entry.runtime_data.daily.update_interval == timedelta(minutes=30)
    state = _overdue(hass)
    assert state.state == STATE_ON
    assert state.attributes["threshold_hours"] == 4
