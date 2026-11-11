"""Sleep sensors: last night, refreshed after each watch sync."""

import logging
from datetime import UTC, datetime
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
def night(mock_client: MagicMock) -> dict[str, Any]:
    data = load_fixture("sleep.json")
    mock_client.get_daily_sleep.return_value = data
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


def _utc_iso(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=UTC).isoformat()


@pytest.mark.usefixtures("summary")
async def test_sleep_sensors_read_last_night(
    hass: HomeAssistant, night: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    durations = {
        "sleep_duration": night["sleepTimeSeconds"] / 3600,  # shown in hours
        "deep_sleep": night["deepSleepSeconds"] / 60,  # stages in minutes
        "light_sleep": night["lightSleepSeconds"] / 60,
        "rem_sleep": night["remSleepSeconds"] / 60,
        "awake_time": night["awakeSleepSeconds"] / 60,
    }
    for key, value in durations.items():
        assert float(_state(hass, key).state) == pytest.approx(value), key
    overall = night["sleepScores"]["overall"]
    assert float(_state(hass, "sleep_score").state) == overall["value"]
    assert _state(hass, "sleep_quality").state == overall["qualifierKey"].lower()
    assert _state(hass, "bedtime").state == _utc_iso(night["sleepStartTimestampGMT"])
    assert _state(hass, "wake_time").state == _utc_iso(night["sleepEndTimestampGMT"])
    # Every sleep sensor tells which night it shows.
    attributes = _state(hass, "sleep_duration").attributes
    assert attributes["night_of"] == night["calendarDate"]


@pytest.mark.usefixtures("summary", "night")
async def test_sleep_units_and_classes(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    duration = _state(hass, "sleep_duration").attributes
    assert duration["device_class"] == "duration"
    assert duration["unit_of_measurement"] == "h"
    assert _state(hass, "deep_sleep").attributes["unit_of_measurement"] == "min"
    quality = _state(hass, "sleep_quality").attributes
    assert quality["device_class"] == "enum"
    assert quality["options"] == ["excellent", "good", "fair", "poor"]
    assert _state(hass, "bedtime").attributes["device_class"] == "timestamp"


@pytest.mark.usefixtures("summary")
@pytest.mark.parametrize(
    "empty", [{}, {"sleepTimeSeconds": None}, {"dailySleep": None}]
)
async def test_last_night_is_kept_until_the_morning_sync(
    hass: HomeAssistant,
    mock_client: MagicMock,
    night: dict[str, Any],
    config_entry: MockConfigEntry,
    empty: dict[str, Any],
) -> None:
    await _setup(hass, config_entry)

    # Before the watch syncs, Garmin returns an empty night for today.
    mock_client.get_daily_sleep.return_value = empty
    await config_entry.runtime_data.sleep.async_refresh()

    expected = night["sleepTimeSeconds"] / 3600
    assert float(_state(hass, "sleep_duration").state) == pytest.approx(expected)
    assert (
        _state(hass, "sleep_duration").attributes["night_of"] == (night["calendarDate"])
    )


@pytest.mark.usefixtures("summary")
async def test_no_night_yet_is_unknown(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    assert _state(hass, "sleep_duration").state == STATE_UNKNOWN
    assert _state(hass, "sleep_quality").state == STATE_UNKNOWN


@pytest.mark.usefixtures("summary")
async def test_unknown_quality_is_logged_once(
    hass: HomeAssistant,
    mock_client: MagicMock,
    night: dict[str, Any],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    night["sleepScores"]["overall"]["qualifierKey"] = "SUPERB"
    mock_client.get_daily_sleep.return_value = night
    caplog.set_level(logging.WARNING)

    await _setup(hass, config_entry)
    await config_entry.runtime_data.sleep.async_refresh()
    await hass.async_block_till_done()

    assert _state(hass, "sleep_quality").state == STATE_UNKNOWN
    assert caplog.text.count("SUPERB") == 1


async def test_a_new_watch_sync_refreshes_sleep(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    night: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    del night
    await _setup(hass, config_entry)
    daily = config_entry.runtime_data.daily
    calls = mock_client.get_daily_sleep.await_count

    # Same sync time: nothing new to download.
    await daily.async_refresh()
    await hass.async_block_till_done()
    assert mock_client.get_daily_sleep.await_count == calls

    summary["lastSyncTimestampGMT"] = "2026-11-10T07:40:00.0"
    await daily.async_refresh()
    await hass.async_block_till_done()
    assert mock_client.get_daily_sleep.await_count == calls + 1


async def test_sleep_failure_does_not_block_daily_sensors(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    mock_client.get_daily_sleep.side_effect = GarminConnectionError("down")

    await _setup(hass, config_entry)

    assert _state(hass, "steps").state == str(summary["totalSteps"])
    assert _state(hass, "sleep_duration").state == STATE_UNAVAILABLE


# Fields absent from the capture: synthetic values, shaped after the API spec.
SYNTHETIC_FIELDS: dict[str, Any] = {
    "avgHeartRate": 52.0,
    "numberOfEventsBelowThreshold": 3,
    "spo2Threshold": 85,
    "breathingDisruptionSeverity": "LOW",
    "sleepNeed": {
        "baseline": 480,
        "actual": 500,
        "recommendedBedtimeStartMins": 1350,
        "recommendedBedtimeEndMins": 1410,
    },
    "nextSleepNeed": {
        "baseline": 480,
        "actual": 460,
        "recommendedBedtimeStartMins": 1320,
        "recommendedBedtimeEndMins": 1380,
    },
    "sleepAlignment": {
        "status": "ALIGNED",
        "sleepNeedBaselineMins": 480,
        "optimalSleepWindowStartMins": -60,
        "optimalSleepWindowEndMins": 420,
        "optimalSleepWindowMidpointMins": 180,
        "lastSleepMidpointMins": 195,
    },
    "dailyNapDTOS": [
        {
            "napTimeSec": 1200,
            "napStartTimestampGMT": "2026-11-10T12:30:00.0",
            "napEndTimestampGMT": "2026-11-10T12:50:00.0",
        }
    ],
}

# Entity key, Garmin field, factor from the Garmin unit to the displayed unit.
DETAIL_SENSORS: list[tuple[str, str, float]] = [
    ("nap_time", "napTimeSeconds", 1 / 60),
    ("unmeasurable_sleep", "unmeasurableSleepSeconds", 1 / 60),
    ("awake_count", "awakeCount", 1),
    ("sleep_stress", "avgSleepStress", 1),
    ("sleep_heart_rate", "avgHeartRate", 1),
    ("sleep_respiration", "averageRespirationValue", 1),
    ("sleep_respiration_highest", "highestRespirationValue", 1),
    ("sleep_respiration_lowest", "lowestRespirationValue", 1),
    ("sleep_spo2", "averageSpO2Value", 1),
    ("sleep_spo2_lowest", "lowestSpO2Value", 1),
    ("sleep_spo2_highest", "highestSpO2Value", 1),
    ("sleep_spo2_heart_rate", "averageSpO2HRSleep", 1),
    ("sleep_spo2_events", "numberOfEventsBelowThreshold", 1),
]
SUB_SCORES = {
    "sleep_duration_rating": "totalDuration",
    "sleep_stress_rating": "stress",
    "awake_count_rating": "awakeCount",
    "rem_sleep_rating": "remPercentage",
    "light_sleep_rating": "lightPercentage",
    "deep_sleep_rating": "deepPercentage",
    "restlessness_rating": "restlessness",
}
STAGE_PERCENTAGES = {
    "rem_sleep_percentage": "remPercentage",
    "light_sleep_percentage": "lightPercentage",
    "deep_sleep_percentage": "deepPercentage",
}
DISABLED_BY_DEFAULT = {
    "unmeasurable_sleep",
    "sleep_respiration_highest",
    "sleep_respiration_lowest",
    "sleep_spo2_highest",
    "sleep_spo2_heart_rate",
    "sleep_spo2_events",
    "sleep_alignment",
    *SUB_SCORES,
    *STAGE_PERCENTAGES,
}
ENABLED_BY_DEFAULT = {
    "nap_time",
    "awake_count",
    "sleep_stress",
    "sleep_heart_rate",
    "sleep_respiration",
    "sleep_spo2",
    "sleep_spo2_lowest",
    "breathing_disruption",
    "sleep_need",
    "next_sleep_need",
}


@pytest.fixture
def full_night(night: dict[str, Any], mock_client: MagicMock) -> dict[str, Any]:
    night.update(SYNTHETIC_FIELDS)
    mock_client.get_daily_sleep.return_value = night
    return night


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
async def test_sleep_detail_sensors_read_last_night(
    hass: HomeAssistant, full_night: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    for key, field, factor in DETAIL_SENSORS:
        expected = full_night[field] * factor
        state = _state(hass, key)
        assert float(state.state) == pytest.approx(expected), key
        assert state.attributes["night_of"] == full_night["calendarDate"], key
    scores = full_night["sleepScores"]
    for key, component in SUB_SCORES.items():
        state = _state(hass, key)
        assert state.state == scores[component]["qualifierKey"].lower(), key
        assert state.attributes["optimal_start"] == scores[component]["optimalStart"]
        assert state.attributes["optimal_end"] == scores[component]["optimalEnd"]
    for key, component in STAGE_PERCENTAGES.items():
        assert float(_state(hass, key).state) == scores[component]["value"], key
    assert _state(hass, "breathing_disruption").state == "low"


@pytest.mark.usefixtures("summary", "full_night")
async def test_niche_sleep_sensors_are_disabled_by_default(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    registry = er.async_get(hass)

    for key in DISABLED_BY_DEFAULT | ENABLED_BY_DEFAULT:
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{PROFILE_ID}_{key}"
        )
        assert entity_id is not None, key
        entry = registry.async_get(entity_id)
        assert entry is not None, key
        disabled = entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert disabled is (key in DISABLED_BY_DEFAULT), key


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
async def test_sleep_detail_units_and_classes(
    hass: HomeAssistant, full_night: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    del full_night
    await _setup(hass, config_entry)

    expected = {
        "nap_time": ("min", "duration", "measurement"),
        "awake_count": (None, None, "measurement"),
        "sleep_stress": (None, None, "measurement"),
        "sleep_heart_rate": ("bpm", None, "measurement"),
        "sleep_respiration": ("br/min", None, "measurement"),
        "sleep_spo2": ("%", None, "measurement"),
        "sleep_spo2_events": (None, None, "measurement"),
        "rem_sleep_percentage": ("%", None, "measurement"),
        "sleep_need": ("h", "duration", "measurement"),
        "breathing_disruption": (None, "enum", None),
        "sleep_duration_rating": (None, "enum", None),
    }
    for key, (unit, device_class, state_class) in expected.items():
        attributes = _state(hass, key).attributes
        assert attributes.get("unit_of_measurement") == unit, key
        assert attributes.get("device_class") == device_class, key
        assert attributes.get("state_class") == state_class, key
    assert _state(hass, "breathing_disruption").attributes["options"] == [
        "none",
        "low",
        "medium",
        "high",
    ]
    assert _state(hass, "restlessness_rating").attributes["options"] == [
        "excellent",
        "good",
        "fair",
        "poor",
    ]


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
async def test_sleep_need_and_alignment(
    hass: HomeAssistant, full_night: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    for key, field in (
        ("sleep_need", "sleepNeed"),
        ("next_sleep_need", "nextSleepNeed"),
    ):
        need = full_night[field]
        state = _state(hass, key)
        # Garmin gives the need in minutes; shown in hours.
        assert float(state.state) == pytest.approx(need["actual"] / 60), key
        assert state.attributes["baseline"] == need["baseline"], key
    bedtime = _state(hass, "next_sleep_need").attributes
    assert bedtime["recommended_bedtime_start"] == "22:00"
    assert bedtime["recommended_bedtime_end"] == "23:00"

    alignment = _state(hass, "sleep_alignment")
    assert alignment.state == "aligned"
    # Signed minutes around midnight: -60 is 23:00 the evening before.
    assert alignment.attributes["optimal_window_start"] == "23:00"
    assert alignment.attributes["optimal_window_end"] == "07:00"
    assert alignment.attributes["optimal_window_midpoint"] == "03:00"
    assert alignment.attributes["last_sleep_midpoint"] == "03:15"


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
async def test_naps_and_score_feedback_are_attributes(
    hass: HomeAssistant, full_night: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    full_night["sleepScoreFeedback"] = "POSITIVE_DEEP"
    full_night["sleepScoreInsight"] = "NEGATIVE_LATE_BED_TIME"
    full_night["sleepScorePersonalizedInsight"] = "NOT_AVAILABLE"

    await _setup(hass, config_entry)

    naps = _state(hass, "nap_time").attributes["naps"]
    assert naps == [
        {
            "start": "2026-11-10T12:30:00+00:00",
            "end": "2026-11-10T12:50:00+00:00",
            "duration": 1200,
        }
    ]
    score = _state(hass, "sleep_score").attributes
    assert score["feedback"] == "positive_deep"
    assert score["insight"] == "negative_late_bed_time"
    assert score["personalized_insight"] == "not_available"
    assert score["night_of"] == full_night["calendarDate"]


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
async def test_missing_sleep_details_are_unknown(
    hass: HomeAssistant,
    mock_client: MagicMock,
    night: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    # The capture itself: no need, no alignment, no naps, no disruption.
    night["averageRespirationValue"] = 0
    night["sleepScores"]["stress"]["qualifierKey"] = "INVALID"
    night["breathingDisruptionSeverity"] = "UNMEASURABLE"
    night["dailyNapDTOS"] = [{"napTimeSec": "bad"}, "not a nap"]
    mock_client.get_daily_sleep.return_value = night

    await _setup(hass, config_entry)

    for key in (
        "sleep_heart_rate",
        "sleep_respiration",
        "sleep_need",
        "next_sleep_need",
        "sleep_alignment",
        "sleep_stress_rating",
        "breathing_disruption",
    ):
        assert _state(hass, key).state == STATE_UNKNOWN, key
    attributes = _state(hass, "sleep_alignment").attributes
    assert "optimal_window_start" not in attributes
    assert _state(hass, "nap_time").attributes["naps"] == [
        {"start": None, "end": None, "duration": None}
    ]
    score = _state(hass, "sleep_score").attributes
    # The anonymised capture has no real feedback code: shown as is.
    assert score["feedback"] == night["sleepScoreFeedback"].lower()
