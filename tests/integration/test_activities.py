"""Activities: the last activity sensor and the new activity event.

The fixture is built from the API schema with synthetic values: real
activity types and exercises describe the user's habits.
"""

import copy
import logging
from datetime import datetime
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


@pytest.fixture
def activities(mock_client: MagicMock) -> list[dict[str, Any]]:
    data = load_fixture("activities_synthetic.json")
    mock_client.get_activities.return_value = data
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


def _state(hass: HomeAssistant, key: str, platform: str = "sensor") -> State:
    entity_id = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{PROFILE_ID}_{key}"
    )
    assert entity_id is not None, key
    state = hass.states.get(entity_id)
    assert state is not None, key
    return state


def _utc(garmin_time: str) -> str:
    # Garmin's GMT times carry no offset: they are UTC.
    return datetime.fromisoformat(f"{garmin_time}+00:00").isoformat()


def _new_activity(activities: list[dict[str, Any]]) -> dict[str, Any]:
    activity = copy.deepcopy(activities[0])
    activity.update(
        activityId=activity["activityId"] + 1,
        activityName="Synthetic new ride",
        activityType={"typeId": 2, "typeKey": "cycling", "parentTypeId": 17},
        startTimeGMT="2026-11-11 07:00:00",
        beginTimestamp=1794380400000,
    )
    return activity


async def _refresh(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.activities.async_refresh()
    await hass.async_block_till_done()


@pytest.mark.usefixtures("summary")
async def test_last_activity_shows_the_newest_one(
    hass: HomeAssistant,
    mock_client: MagicMock,
    activities: list[dict[str, Any]],
    config_entry: MockConfigEntry,
) -> None:
    # The order Garmin sends does not matter: the start time decides.
    mock_client.get_activities.return_value = list(reversed(activities))

    await _setup(hass, config_entry)

    latest = activities[0]
    state = _state(hass, "last_activity")
    assert state.state == latest["activityName"]
    attributes = state.attributes
    assert attributes["activity_id"] == latest["activityId"]
    assert attributes["activity_type"] == latest["activityType"]["typeKey"]
    assert attributes["event_type"] == latest["eventType"]["typeKey"]
    assert attributes["start"] == _utc(latest["startTimeGMT"])
    assert attributes["end"] == _utc(latest["endTimeGMT"])


@pytest.mark.usefixtures("summary")
async def test_every_summary_field_is_an_attribute(
    hass: HomeAssistant, activities: list[dict[str, Any]], config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    latest = activities[0]
    attributes = _state(hass, "last_activity").attributes
    expected = {
        "distance": "distance",
        "duration": "duration",
        "average_hr": "averageHR",
        "max_hr": "maxHR",
        "elevation_gain": "elevationGain",
        "average_running_cadence_in_steps_per_minute": (
            "averageRunningCadenceInStepsPerMinute"
        ),
        "v_o2_max_value": "vO2MaxValue",
        "hr_time_in_zone_1": "hrTimeInZone_1",
        "training_effect_label": "trainingEffectLabel",
        "difference_body_battery": "differenceBodyBattery",
        "is_favorite": "isFavorite",
        "manufacturer": "manufacturer",
    }
    for attribute, field in expected.items():
        assert attributes[attribute] == latest[field], attribute


@pytest.mark.usefixtures("summary", "activities")
async def test_private_and_duplicate_fields_are_left_out(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    attributes = _state(hass, "last_activity").attributes
    hidden = {
        "owner_id",
        "owner_display_name",
        "owner_full_name",
        "owner_profile_image_url_small",
        "start_latitude",
        "start_longitude",
        "end_latitude",
        "end_longitude",
        "location_name",
        "activity_uuid",
        "description",
        "activity_name",
        "number_of_activity_likes",
        "liked_by_user",
        "activity_like_display_names",
        "favorite",  # same as is_favorite
        "begin_timestamp",  # same as start
    }
    assert hidden.isdisjoint(attributes), hidden & set(attributes)


@pytest.mark.usefixtures("summary")
async def test_long_names_are_cut_to_fit_a_state(
    hass: HomeAssistant, activities: list[dict[str, Any]], config_entry: MockConfigEntry
) -> None:
    activities[0]["activityName"] = "x" * 300

    await _setup(hass, config_entry)

    assert _state(hass, "last_activity").state == "x" * 255


@pytest.mark.usefixtures("summary")
async def test_no_activity_is_unknown_without_logs(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The mocked client answers [] by default.
    caplog.set_level(logging.WARNING)

    await _setup(hass, config_entry)
    await _refresh(hass, config_entry)

    assert _state(hass, "last_activity").state == STATE_UNKNOWN
    assert _state(hass, "new_activity", "event").state == STATE_UNKNOWN
    assert not [r for r in caplog.records if r.name.startswith("custom_components")]


@pytest.mark.usefixtures("summary")
async def test_activities_known_at_start_fire_no_event(
    hass: HomeAssistant,
    activities: list[dict[str, Any]],
    config_entry: MockConfigEntry,
) -> None:
    del activities
    await _setup(hass, config_entry)
    await _refresh(hass, config_entry)

    assert _state(hass, "new_activity", "event").state == STATE_UNKNOWN


@pytest.mark.usefixtures("summary")
async def test_a_new_activity_fires_one_event(
    hass: HomeAssistant,
    mock_client: MagicMock,
    activities: list[dict[str, Any]],
    config_entry: MockConfigEntry,
) -> None:
    await _setup(hass, config_entry)
    new = _new_activity(activities)
    mock_client.get_activities.return_value = [new, *activities]

    await _refresh(hass, config_entry)

    state = _state(hass, "new_activity", "event")
    assert state.state != STATE_UNKNOWN
    assert state.attributes["event_type"] == "new_activity"
    assert state.attributes["activity_id"] == new["activityId"]
    assert state.attributes["name"] == new["activityName"]
    assert state.attributes["activity_type"] == new["activityType"]["typeKey"]
    assert state.attributes["start"] == _utc(new["startTimeGMT"])
    assert _state(hass, "last_activity").state == new["activityName"]

    # The same activity again: no second event.
    fired_at = state.state
    await _refresh(hass, config_entry)
    assert _state(hass, "new_activity", "event").state == fired_at


@pytest.mark.usefixtures("summary")
async def test_an_old_activity_moving_up_after_a_deletion_fires_no_event(
    hass: HomeAssistant,
    mock_client: MagicMock,
    activities: list[dict[str, Any]],
    config_entry: MockConfigEntry,
) -> None:
    await _setup(hass, config_entry)
    older = copy.deepcopy(activities[-1])
    older.update(activityId=older["activityId"] - 1, startTimeGMT="2026-11-01 06:00:00")
    # The newest activity is deleted: an older one enters the list.
    mock_client.get_activities.return_value = [*activities[1:], older]

    await _refresh(hass, config_entry)

    assert _state(hass, "new_activity", "event").state == STATE_UNKNOWN


@pytest.mark.usefixtures("summary")
async def test_activities_found_after_a_failed_start_fire_no_event(
    hass: HomeAssistant,
    mock_client: MagicMock,
    activities: list[dict[str, Any]],
    config_entry: MockConfigEntry,
) -> None:
    mock_client.get_activities.side_effect = GarminConnectionError("down")
    await _setup(hass, config_entry)

    mock_client.get_activities.side_effect = None
    mock_client.get_activities.return_value = activities
    await _refresh(hass, config_entry)

    assert _state(hass, "last_activity").state == activities[0]["activityName"]
    assert _state(hass, "new_activity", "event").state == STATE_UNKNOWN


@pytest.mark.usefixtures("summary")
async def test_activity_names_are_never_logged(
    hass: HomeAssistant,
    mock_client: MagicMock,
    activities: list[dict[str, Any]],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.my_garmin")
    await _setup(hass, config_entry)
    new = _new_activity(activities)
    mock_client.get_activities.return_value = [new, *activities]
    await _refresh(hass, config_entry)

    # Only this integration's records: Home Assistant core logs every state
    # change at DEBUG level, and the sensor state is the name.
    ours = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("custom_components.my_garmin")
    )
    assert ours  # the integration did log, at DEBUG level
    for activity in (new, *activities):
        assert activity["activityName"] not in ours


async def test_a_new_watch_sync_refreshes_activities(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    await _setup(hass, config_entry)
    calls = mock_client.get_activities.await_count

    summary["lastSyncTimestampGMT"] = "2026-11-10T07:40:00.0"
    await config_entry.runtime_data.daily.async_refresh()
    await hass.async_block_till_done()

    assert mock_client.get_activities.await_count == calls + 1


async def test_activity_failure_does_not_block_other_sensors(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    mock_client.get_activities.side_effect = GarminConnectionError("down")

    await _setup(hass, config_entry)

    assert _state(hass, "steps").state == str(summary["totalSteps"])
    assert _state(hass, "last_activity").state == STATE_UNAVAILABLE
