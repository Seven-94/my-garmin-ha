"""Key expiry: a diagnostic sensor and a repair issue before the key expires."""

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import CONF_API_KEY, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.my_garmin.api import GarminAuthError
from custom_components.my_garmin.const import CONF_KEY_EXPIRES, DOMAIN
from tests.integration.conftest import API_KEY, PROFILE_ID

ISSUE_ID = "key_expiry"


def _entry(expires: str) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="My Garmin",
        unique_id=str(PROFILE_ID),
        data={CONF_API_KEY: API_KEY, CONF_KEY_EXPIRES: expires},
    )


def _in_days(days: int) -> str:
    return (dt_util.now().date() + timedelta(days=days)).isoformat()


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _issue(hass: HomeAssistant) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ID)


async def _next_day(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.tick(timedelta(days=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def _sensor(hass: HomeAssistant) -> State:
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{PROFILE_ID}_key_expiry"
    )
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    return state


@pytest.fixture(autouse=True)
def _frozen_today(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    del hass  # requested so that Home Assistant's time zone is set first
    # Noon local time: far from midnight, when the daily check runs.
    noon = datetime(2026, 11, 11, 12, tzinfo=dt_util.get_default_time_zone())
    freezer.move_to(noon)


@pytest.mark.usefixtures("mock_client")
async def test_sensor_shows_the_expiry_date(hass: HomeAssistant) -> None:
    expires = _in_days(200)
    await _setup(hass, _entry(expires))

    state = _sensor(hass)
    assert state.state == expires
    assert state.attributes["device_class"] == "date"
    entry = er.async_get(hass).async_get(state.entity_id)
    assert entry is not None
    assert entry.entity_category is er.EntityCategory.DIAGNOSTIC


@pytest.mark.usefixtures("mock_client")
async def test_no_issue_while_the_date_is_far(hass: HomeAssistant) -> None:
    await _setup(hass, _entry(_in_days(8)))

    assert _issue(hass) is None


@pytest.mark.usefixtures("mock_client")
async def test_warning_seven_days_before(hass: HomeAssistant) -> None:
    expires = _in_days(7)
    await _setup(hass, _entry(expires))

    issue = _issue(hass)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == "key_expiring"
    assert issue.translation_placeholders == {"expires": expires, "days": "7"}
    assert not issue.is_fixable


@pytest.mark.usefixtures("mock_client")
async def test_issue_follows_the_days_then_turns_to_error(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    expires = _in_days(8)
    await _setup(hass, _entry(expires))
    assert _issue(hass) is None

    await _next_day(hass, freezer)
    issue = _issue(hass)
    assert issue is not None
    assert issue.translation_placeholders == {"expires": expires, "days": "7"}

    await _next_day(hass, freezer)
    issue = _issue(hass)
    assert issue is not None
    assert issue.translation_placeholders == {"expires": expires, "days": "6"}

    # The key still works on its expiry day; it has expired the day after.
    for _ in range(6):
        await _next_day(hass, freezer)
    issue = _issue(hass)
    assert issue is not None
    assert issue.translation_key == "key_expiring"
    assert issue.translation_placeholders == {"expires": expires, "days": "0"}

    await _next_day(hass, freezer)
    issue = _issue(hass)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.translation_key == "key_expired"
    assert issue.translation_placeholders == {"expires": expires}


@pytest.mark.usefixtures("mock_client")
async def test_a_new_date_removes_the_issue(hass: HomeAssistant) -> None:
    entry = _entry(_in_days(3))
    await _setup(hass, entry)
    assert _issue(hass) is not None

    # What the reconfigure flow does: new data, then a reload.
    new_expires = _in_days(365)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_KEY_EXPIRES: new_expires}
    )
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert _issue(hass) is None
    assert _sensor(hass).state == new_expires


async def test_expired_key_is_reported_even_when_garmin_rejects_it(
    hass: HomeAssistant, mock_client: MagicMock
) -> None:
    mock_client.get_daily_summary.side_effect = GarminAuthError("expired")

    await _setup(hass, _entry(_in_days(-2)))

    issue = _issue(hass)
    assert issue is not None
    assert issue.translation_key == "key_expired"


@pytest.mark.usefixtures("mock_client")
async def test_removing_the_integration_removes_the_issue(hass: HomeAssistant) -> None:
    entry = _entry(_in_days(1))
    await _setup(hass, entry)
    assert _issue(hass) is not None

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert _issue(hass) is None


@pytest.mark.usefixtures("mock_client")
async def test_unreadable_date_gives_no_issue_and_an_unknown_sensor(
    hass: HomeAssistant,
) -> None:
    await _setup(hass, _entry("not a date"))

    assert _issue(hass) is None
    assert _sensor(hass).state == STATE_UNKNOWN
