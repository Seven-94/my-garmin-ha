"""Binary sensors from the daily summary."""

from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.my_garmin.const import DOMAIN
from tests.integration.conftest import PROFILE_ID, load_fixture

# Synthetic: no capture holds a jet lag day.
TRIP = "Test trip"


@pytest.fixture
def summary(mock_client: MagicMock) -> dict[str, Any]:
    data = load_fixture("daily_summary.json")
    mock_client.get_daily_summary.return_value = data
    return data


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _jet_lag(hass: HomeAssistant) -> State:
    entity_id = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{PROFILE_ID}_jet_lag"
    )
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    return state


@pytest.mark.parametrize(
    ("fields", "expected", "trip"),
    [
        ({"isJetLagDay": True, "jetLagTripName": TRIP}, STATE_ON, TRIP),
        ({"isJetLagDay": False, "jetLagTripName": None}, STATE_OFF, None),
        ({}, STATE_UNKNOWN, None),  # field absent, as in the capture
        ({"isJetLagDay": "yes", "jetLagTripName": 3}, STATE_UNKNOWN, None),
    ],
)
async def test_jet_lag_day(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
    fields: dict[str, Any],
    expected: str,
    trip: str | None,
) -> None:
    summary.update(fields)
    mock_client.get_daily_summary.return_value = summary

    await _setup(hass, config_entry)

    state = _jet_lag(hass)
    assert state.state == expected
    assert state.attributes.get("trip_name") == trip


async def test_jet_lag_follows_the_coordinator(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    summary["isJetLagDay"] = False
    await _setup(hass, config_entry)
    assert _jet_lag(hass).state == STATE_OFF

    mock_client.get_daily_summary.return_value = {
        **summary,
        "isJetLagDay": True,
        "jetLagTripName": TRIP,
    }
    await config_entry.runtime_data.daily.async_refresh()
    await hass.async_block_till_done()

    state = _jet_lag(hass)
    assert state.state == STATE_ON
    assert state.attributes["trip_name"] == TRIP
