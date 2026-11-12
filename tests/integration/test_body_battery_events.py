"""Body Battery events and feedback: what charged or drained it today.

`body_battery_events_synthetic.json` is built from the API schema, with
synthetic values and feedback codes. The day's readings of the same report are
not read: long-term statistics are a separate issue.
"""

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

FEEDBACK = "body_battery_feedback"
END_OF_DAY = "body_battery_end_of_day_feedback"
LATEST_EVENT = "body_battery_latest_event"
KEYS = (FEEDBACK, END_OF_DAY, LATEST_EVENT)
EVENT_TYPES = ["activity", "sleep", "stress", "recovery", "nap"]
EVENT_FIELDS = (
    "bodyBatteryDynamicFeedbackEvent",
    "endOfDayBodyBatteryDynamicFeedbackEvent",
    "bodyBatteryActivityEvent",
)


def _today() -> str:
    return dt_util.now().date().isoformat()


def _utc(value: str) -> str:
    return datetime.fromisoformat(value).replace(tzinfo=UTC).isoformat()


@pytest.fixture
def report(mock_client: MagicMock) -> dict[str, Any]:
    reports = load_fixture("body_battery_events_synthetic.json")
    # The coordinator asks for today and keeps today's report only.
    reports[0]["date"] = _today()
    mock_client.get_body_battery_reports.return_value = reports
    return reports[0]


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
async def test_feedback_sensors_show_the_feedback_codes(
    hass: HomeAssistant, report: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    for key, field in (
        (FEEDBACK, "bodyBatteryDynamicFeedbackEvent"),
        (END_OF_DAY, "endOfDayBodyBatteryDynamicFeedbackEvent"),
    ):
        event = report[field]
        state = _state(hass, key)
        assert state.state == event["feedbackShortType"].lower(), key
        assert state.attributes["long_feedback"] == event["feedbackLongType"].lower()
        assert state.attributes["level"] == event["bodyBatteryLevel"].lower()
        assert state.attributes["time"] == _utc(event["eventTimestampGmt"])


@pytest.mark.usefixtures("summary", "report")
async def test_today_is_requested(
    hass: HomeAssistant, mock_client: MagicMock, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    # Yesterday too, in the same request: a whole day tells whether the watch
    # makes events at all, even just after midnight.
    today = dt_util.now().date()
    mock_client.get_body_battery_reports.assert_awaited_with(
        today - timedelta(days=1), today
    )


@pytest.mark.usefixtures("summary", "report")
async def test_latest_event_is_the_most_recent_one(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    # The stress event comes last in the list but started before the activity.
    state = _state(hass, LATEST_EVENT)
    assert state.state == "activity"
    assert state.attributes["device_class"] == "enum"
    assert state.attributes["options"] == EVENT_TYPES
    assert state.attributes["start"] == _utc("2026-11-08T12:00:00.0")
    assert state.attributes["impact"] == -15
    assert state.attributes["duration_seconds"] == 3600
    assert state.attributes["feedback"] == "synthetic_activity_feedback"
    assert state.attributes["short_feedback"] == "synthetic_activity_short"


@pytest.mark.usefixtures("summary", "report")
async def test_day_events_are_sorted_without_duplicates(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    events = _state(hass, LATEST_EVENT).attributes["events"]
    assert [(e["type"], e["impact"]) for e in events] == [
        ("sleep", 45),
        ("stress", -5),
        ("activity", -15),
    ]
    assert events[0] == {
        "type": "sleep",
        "start": _utc("2026-11-07T22:30:00.0"),
        "duration_seconds": 27000,
        "impact": 45,
        "feedback": "synthetic_sleep_feedback",
        "short_feedback": "synthetic_sleep_short",
    }


@pytest.mark.usefixtures("summary", "report")
async def test_the_day_readings_are_not_kept(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    data = config_entry.runtime_data.body_battery_events.data
    assert "bodyBatteryValuesArray" not in data
    assert "bodyBatteryValueDescriptorDTOList" not in data


@pytest.mark.usefixtures("summary", "report")
async def test_end_of_day_feedback_is_disabled_by_default(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    registry = er.async_get(hass)

    for key in KEYS:
        entry = registry.async_get(_entity_id(hass, key))
        assert entry is not None, key
        disabled = entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert disabled is (key == END_OF_DAY), key


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
async def test_an_unknown_event_type_is_unknown_and_logged_once(
    hass: HomeAssistant,
    report: dict[str, Any],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    report["bodyBatteryActivityEvent"] = [
        {"eventType": "TELEPORT", "eventStartTimeGmt": "2026-11-08T12:00:00.0"}
    ]

    await _setup(hass, config_entry)
    await config_entry.runtime_data.body_battery_events.async_refresh()
    await hass.async_block_till_done()

    assert _state(hass, LATEST_EVENT).state == STATE_UNKNOWN
    assert (
        caplog.text.lower().count("unknown body battery event from garmin: teleport")
        == 1
    )


def _yesterday() -> str:
    return (dt_util.now().date() - timedelta(days=1)).isoformat()


def _without_events(report: dict[str, Any]) -> dict[str, Any]:
    """A report as Garmin sends it for a watch without events or feedback."""
    return {k: v for k, v in report.items() if k not in EVENT_FIELDS}


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
@pytest.mark.parametrize(
    "today",
    [
        None,
        {},
        {
            "bodyBatteryDynamicFeedbackEvent": None,
            "endOfDayBodyBatteryDynamicFeedbackEvent": None,
            "bodyBatteryActivityEvent": None,
        },
    ],
    ids=["no-report", "no-events", "null-events"],
)
async def test_no_event_today_is_unknown_without_logs(
    hass: HomeAssistant,
    mock_client: MagicMock,
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
    today: dict[str, Any] | None,
) -> None:
    # The watch makes events (yesterday has some), but none yet today.
    synthetic = load_fixture("body_battery_events_synthetic.json")[0]
    answer = [{**synthetic, "date": _yesterday()}]
    if today is not None:
        answer.append({**_without_events(synthetic), **today, "date": _today()})
    mock_client.get_body_battery_reports.return_value = answer
    caplog.set_level(logging.WARNING)

    await _setup(hass, config_entry)

    for key in KEYS:
        assert _state(hass, key).state == STATE_UNKNOWN, key
    assert _state(hass, LATEST_EVENT).attributes["events"] == []
    assert not [r for r in caplog.records if r.name.startswith("custom_components")]


@pytest.fixture
def no_events(mock_client: MagicMock) -> None:
    """Yesterday and today, readings only: what Garmin sends for some watches."""
    synthetic = _without_events(load_fixture("body_battery_events_synthetic.json")[0])
    mock_client.get_body_battery_reports.return_value = [
        {**synthetic, "date": _yesterday()},
        {**synthetic, "date": _today()},
    ]


@pytest.mark.usefixtures("summary", "no_events", "entity_registry_enabled_by_default")
async def test_no_event_sensors_when_garmin_sends_no_events(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    registry = er.async_get(hass)
    for key in KEYS:
        unique_id = f"{PROFILE_ID}_{key}"
        assert registry.async_get_entity_id("sensor", DOMAIN, unique_id) is None, key
    # The day's Body Battery levels come from the daily summary, untouched.
    assert _state(hass, "body_battery_charged").state != STATE_UNAVAILABLE


@pytest.mark.usefixtures("summary", "no_events")
async def test_event_sensors_left_by_an_older_version_are_removed(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    for key in KEYS:
        registry.async_get_or_create(
            "sensor", DOMAIN, f"{PROFILE_ID}_{key}", config_entry=config_entry
        )

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    for key in KEYS:
        unique_id = f"{PROFILE_ID}_{key}"
        assert registry.async_get_entity_id("sensor", DOMAIN, unique_id) is None, key


async def test_events_refresh_after_a_watch_sync(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    report: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    del report
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data.body_battery_events
    assert coordinator.update_interval == timedelta(hours=2)
    calls = mock_client.get_body_battery_reports.await_count

    summary["lastSyncTimestampGMT"] = "2026-11-10T07:40:00.0"
    await config_entry.runtime_data.daily.async_refresh()
    await hass.async_block_till_done()

    assert mock_client.get_body_battery_reports.await_count == calls + 1


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_events_failure_does_not_block_other_sensors(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    mock_client.get_body_battery_reports.side_effect = GarminConnectionError("down")

    await _setup(hass, config_entry)

    assert _state(hass, "steps").state == str(summary["totalSteps"])
    for key in KEYS:
        assert _state(hass, key).state == STATE_UNAVAILABLE, key
