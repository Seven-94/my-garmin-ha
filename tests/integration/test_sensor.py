"""Daily summary sensors and their coordinator."""

from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.my_garmin.api import (
    GarminAuthError,
    GarminConnectionError,
    GarminRateLimitError,
)
from custom_components.my_garmin.const import DOMAIN
from tests.integration.conftest import PROFILE_ID, load_fixture


@pytest.fixture
def summary(mock_client: MagicMock) -> dict[str, Any]:
    data = load_fixture("daily_summary.json")
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


@pytest.mark.usefixtures("summary")
async def test_sensors_read_the_daily_summary(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    data = load_fixture("daily_summary.json")
    expected = {
        "steps": data["totalSteps"],
        "step_goal": data["dailyStepGoal"],
        "distance": data["totalDistanceMeters"] / 1000,  # shown in km
        "active_calories": data["activeKilocalories"],
        # Vigorous minutes count double.
        "intensity_minutes": data["moderateIntensityMinutes"]
        + 2 * data["vigorousIntensityMinutes"],
        "floors_ascended": data["floorsAscended"],
        "resting_heart_rate": data["restingHeartRate"],
        "average_stress": data["averageStressLevel"],
        "body_battery": data["bodyBatteryMostRecentValue"],
        "body_battery_highest": data["bodyBatteryHighestValue"],
        "body_battery_lowest": data["bodyBatteryLowestValue"],
        "respiration": data["avgWakingRespirationValue"],
        "spo2": data["averageSpo2"],
    }
    for key, value in expected.items():
        assert float(_state(hass, key).state) == pytest.approx(value), key
    # No sync timestamp in this capture.
    assert _state(hass, "last_sync").state == STATE_UNKNOWN


@pytest.mark.usefixtures("summary")
async def test_sensor_units_and_classes(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    steps = _state(hass, "steps").attributes
    assert steps["unit_of_measurement"] == "steps"
    assert steps["state_class"] == "total_increasing"
    distance = _state(hass, "distance").attributes
    assert distance["device_class"] == "distance"
    assert distance["unit_of_measurement"] == "km"
    heart = _state(hass, "resting_heart_rate").attributes
    assert heart["unit_of_measurement"] == "bpm"
    assert heart["state_class"] == "measurement"
    assert _state(hass, "intensity_minutes").attributes["device_class"] == "duration"
    assert _state(hass, "last_sync").attributes["device_class"] == "timestamp"


@pytest.mark.usefixtures("summary")
async def test_noisy_sensors_are_disabled_by_default(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    registry = er.async_get(hass)

    for key in ("min_heart_rate", "max_heart_rate", "total_calories"):
        entry = registry.async_get(_entity_id(hass, key))
        assert entry is not None
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION, key


async def test_not_measured_values_are_unknown(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    summary.update(
        restingHeartRate=0,  # Garmin's "no reading"
        averageStressLevel=-1,  # not enough data
        bodyBatteryMostRecentValue=None,
        totalSteps=0,  # a real zero
        lastSyncTimestampGMT="2026-11-10T06:15:00.0",
    )
    mock_client.get_daily_summary.return_value = summary

    await _setup(hass, config_entry)

    assert _state(hass, "resting_heart_rate").state == STATE_UNKNOWN
    assert _state(hass, "average_stress").state == STATE_UNKNOWN
    assert _state(hass, "body_battery").state == STATE_UNKNOWN
    assert _state(hass, "steps").state == "0"
    assert _state(hass, "last_sync").state == "2026-11-10T06:15:00+00:00"


@pytest.mark.usefixtures("summary")
async def test_one_device_for_the_account(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, str(PROFILE_ID)), config_entry.entry_id
    )
    assert device is not None
    assert device.name == "My Garmin"
    assert device.manufacturer == "Garmin"
    assert device.entry_type is dr.DeviceEntryType.SERVICE


@pytest.mark.parametrize(
    "error", [GarminConnectionError("down"), GarminRateLimitError(60.0)]
)
async def test_failed_refresh_makes_sensors_unavailable_until_recovery(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
    error: Exception,
) -> None:
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data.daily

    mock_client.get_daily_summary.side_effect = error
    await coordinator.async_refresh()
    assert _state(hass, "steps").state == STATE_UNAVAILABLE

    mock_client.get_daily_summary.side_effect = None
    mock_client.get_daily_summary.return_value = summary
    await coordinator.async_refresh()
    assert _state(hass, "steps").state == str(summary["totalSteps"])


async def test_rate_limit_backs_off_for_at_least_30_minutes(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    del summary
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data.daily

    mock_client.get_daily_summary.side_effect = GarminRateLimitError(60.0)
    await coordinator.async_refresh()

    assert coordinator.last_exception.retry_after >= 30 * 60


async def test_rejected_key_during_refresh_starts_reauth(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    del summary
    await _setup(hass, config_entry)

    mock_client.get_daily_summary.side_effect = GarminAuthError("rejected")
    await config_entry.runtime_data.daily.async_refresh()
    await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-11-10T06:15:00.0", "2026-11-10T06:15:00+00:00"),
        # The real format, with milliseconds.
        ("2026-11-10T06:15:00.123", "2026-11-10T06:15:00+00:00"),
        ("2026-11-10T07:15:00+01:00", "2026-11-10T06:15:00+00:00"),
        ("not a date", STATE_UNKNOWN),
        (None, STATE_UNKNOWN),
    ],
)
async def test_last_sync_is_read_as_utc(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
    raw: str | None,
    expected: str,
) -> None:
    summary["lastSyncTimestampGMT"] = raw
    mock_client.get_daily_summary.return_value = summary

    await _setup(hass, config_entry)

    assert _state(hass, "last_sync").state == expected


# Fields absent from the capture: synthetic values, shaped after the API spec.
SYNTHETIC_FIELDS: dict[str, Any] = {
    "consumedKilocalories": 1800.0,
    "remainingKilocalories": -150.0,  # over the goal: negative is meaningful
    "netCalorieGoal": 1650,
    "totalPushes": 1200,
    "totalPushDistance": 850,
    "dailyPushesGoal": 1500,
    "abnormalHeartRateAlertsCount": 2,
    "hydrationValueInML": 1250.0,
    "hydrationGoalInML": 2000.0,
}

MINUTES = 1 / 60
HOURS = 1 / 3600
KILOMETERS = 1 / 1000
# Entity key, Garmin field, factor from the Garmin unit to the displayed unit.
DETAIL_SENSORS: list[tuple[str, str, float]] = [
    ("highly_active_time", "highlyActiveSeconds", MINUTES),
    ("active_time", "activeSeconds", MINUTES),
    ("sedentary_time", "sedentarySeconds", HOURS),
    ("sleeping_time", "sleepingSeconds", HOURS),
    ("floors_descended", "floorsDescended", 1),
    ("floors_goal", "userFloorsAscendedGoal", 1),
    ("floors_ascended_distance", "floorsAscendedInMeters", 1),
    ("floors_descended_distance", "floorsDescendedInMeters", 1),
    ("intensity_minutes_goal", "intensityMinutesGoal", 1),
    ("bmr_calories", "bmrKilocalories", 1),
    ("consumed_calories", "consumedKilocalories", 1),
    ("remaining_calories", "remainingKilocalories", 1),
    ("net_calorie_goal", "netCalorieGoal", 1),
    ("pushes", "totalPushes", 1),
    ("push_distance", "totalPushDistance", KILOMETERS),
    ("push_goal", "dailyPushesGoal", 1),
    ("resting_heart_rate_7_days", "lastSevenDaysAvgRestingHeartRate", 1),
    ("min_avg_heart_rate", "minAvgHeartRate", 1),
    ("max_avg_heart_rate", "maxAvgHeartRate", 1),
    ("abnormal_heart_rate_alerts", "abnormalHeartRateAlertsCount", 1),
    ("max_stress", "maxStressLevel", 1),
    ("stress_duration", "stressDuration", MINUTES),
    ("rest_stress_duration", "restStressDuration", MINUTES),
    ("activity_stress_duration", "activityStressDuration", MINUTES),
    ("uncategorized_stress_duration", "uncategorizedStressDuration", MINUTES),
    ("total_stress_duration", "totalStressDuration", MINUTES),
    ("low_stress_duration", "lowStressDuration", MINUTES),
    ("medium_stress_duration", "mediumStressDuration", MINUTES),
    ("high_stress_duration", "highStressDuration", MINUTES),
    ("stress_percentage", "stressPercentage", 1),
    ("rest_stress_percentage", "restStressPercentage", 1),
    ("activity_stress_percentage", "activityStressPercentage", 1),
    ("uncategorized_stress_percentage", "uncategorizedStressPercentage", 1),
    ("low_stress_percentage", "lowStressPercentage", 1),
    ("medium_stress_percentage", "mediumStressPercentage", 1),
    ("high_stress_percentage", "highStressPercentage", 1),
    ("measurable_awake_time", "measurableAwakeDuration", HOURS),
    ("measurable_asleep_time", "measurableAsleepDuration", HOURS),
    ("body_battery_charged", "bodyBatteryChargedValue", 1),
    ("body_battery_drained", "bodyBatteryDrainedValue", 1),
    ("body_battery_during_sleep", "bodyBatteryDuringSleep", 1),
    ("body_battery_at_wake", "bodyBatteryAtWakeTime", 1),
    ("respiration_highest", "highestRespirationValue", 1),
    ("respiration_lowest", "lowestRespirationValue", 1),
    ("respiration_latest", "latestRespirationValue", 1),
    ("spo2_lowest", "lowestSpo2", 1),
    ("spo2_latest", "latestSpo2", 1),
    ("hydration_intake", "hydrationValueInML", 1),
    ("hydration_goal", "hydrationGoalInML", 1),
    ("wellness_distance", "wellnessDistanceMeters", KILOMETERS),
]

DISABLED_BY_DEFAULT = {
    "sleeping_time",
    "floors_ascended_distance",
    "floors_descended_distance",
    "bmr_calories",
    "consumed_calories",
    "remaining_calories",
    "net_calorie_goal",
    "pushes",
    "push_distance",
    "push_goal",
    "min_avg_heart_rate",
    "max_avg_heart_rate",
    "stress_duration",
    "activity_stress_duration",
    "uncategorized_stress_duration",
    "total_stress_duration",
    "stress_percentage",
    "rest_stress_percentage",
    "activity_stress_percentage",
    "uncategorized_stress_percentage",
    "low_stress_percentage",
    "medium_stress_percentage",
    "high_stress_percentage",
    "measurable_awake_time",
    "measurable_asleep_time",
    "respiration_highest",
    "respiration_lowest",
    "hydration_intake",
    "hydration_goal",
    "wellness_distance",
}


@pytest.fixture
def full_summary(summary: dict[str, Any], mock_client: MagicMock) -> dict[str, Any]:
    summary.update(SYNTHETIC_FIELDS)
    mock_client.get_daily_summary.return_value = summary
    return summary


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_detail_sensors_read_every_summary_field(
    hass: HomeAssistant, full_summary: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    for key, field, factor in DETAIL_SENSORS:
        expected = full_summary[field] * factor
        assert float(_state(hass, key).state) == pytest.approx(expected), key


@pytest.mark.usefixtures("full_summary")
async def test_niche_detail_sensors_are_disabled_by_default(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    registry = er.async_get(hass)

    for key, _, _ in DETAIL_SENSORS:
        entry = registry.async_get(_entity_id(hass, key))
        assert entry is not None, key
        disabled = entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert disabled is (key in DISABLED_BY_DEFAULT), key


@pytest.mark.usefixtures("entity_registry_enabled_by_default", "full_summary")
async def test_detail_sensor_units_and_classes(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    expected = {
        "active_time": ("min", "duration", "total_increasing"),
        "sedentary_time": ("h", "duration", "total_increasing"),
        "floors_descended": ("floors", None, "total_increasing"),
        "floors_goal": ("floors", None, None),
        "floors_ascended_distance": ("m", "distance", "total_increasing"),
        "intensity_minutes_goal": ("min", "duration", None),
        "bmr_calories": ("kcal", None, "total_increasing"),
        "remaining_calories": ("kcal", None, "measurement"),
        "net_calorie_goal": ("kcal", None, None),
        "pushes": ("pushes", None, "total_increasing"),
        "push_distance": ("km", "distance", "total_increasing"),
        "resting_heart_rate_7_days": ("bpm", None, "measurement"),
        "abnormal_heart_rate_alerts": (None, None, "total_increasing"),
        "max_stress": (None, None, "measurement"),
        "low_stress_duration": ("min", "duration", "total_increasing"),
        "low_stress_percentage": ("%", None, "measurement"),
        "body_battery_charged": ("%", None, "total_increasing"),
        "body_battery_at_wake": ("%", None, "measurement"),
        "respiration_latest": ("br/min", None, "measurement"),
        "spo2_latest": ("%", None, "measurement"),
        "hydration_intake": ("mL", "volume", "total_increasing"),
        "hydration_goal": ("mL", "volume", None),
        "wellness_distance": ("km", "distance", "total_increasing"),
    }
    for key, (unit, device_class, state_class) in expected.items():
        attributes = _state(hass, key).attributes
        assert attributes.get("unit_of_measurement") == unit, key
        assert attributes.get("device_class") == device_class, key
        assert attributes.get("state_class") == state_class, key


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_detail_readings_of_zero_or_null_are_unknown(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    summary.update(
        lastSevenDaysAvgRestingHeartRate=0,
        latestRespirationValue=0,
        latestSpo2=None,
        remainingKilocalories=None,
        maxStressLevel=-2,
        highlyActiveSeconds=0,  # a real zero
    )
    mock_client.get_daily_summary.return_value = summary

    await _setup(hass, config_entry)

    for key in (
        "resting_heart_rate_7_days",
        "respiration_latest",
        "spo2_latest",
        "remaining_calories",
        "max_stress",
    ):
        assert _state(hass, key).state == STATE_UNKNOWN, key
    assert float(_state(hass, "highly_active_time").state) == 0


STRESS_QUALIFIERS = [
    "calm",
    "balanced",
    "stressful",
    "very_stressful",
    "calm_awake",
    "balanced_awake",
    "stressful_awake",
    "very_stressful_awake",
]


@pytest.mark.parametrize("qualifier", STRESS_QUALIFIERS)
async def test_stress_category_covers_the_spec_enum(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
    qualifier: str,
) -> None:
    summary["stressQualifier"] = qualifier.upper()
    mock_client.get_daily_summary.return_value = summary

    await _setup(hass, config_entry)

    state = _state(hass, "stress_category")
    assert state.state == qualifier
    assert state.attributes["device_class"] == "enum"
    assert state.attributes["options"] == STRESS_QUALIFIERS


async def test_unknown_stress_category_is_logged_once(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    summary["stressQualifier"] = "SERENE_TEST_ONLY"
    mock_client.get_daily_summary.return_value = summary
    await _setup(hass, config_entry)
    await config_entry.runtime_data.daily.async_refresh()
    await hass.async_block_till_done()

    assert _state(hass, "stress_category").state == STATE_UNKNOWN
    assert caplog.text.count("SERENE_TEST_ONLY") == 1


async def test_missing_stress_category_is_unknown(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    summary["stressQualifier"] = None
    mock_client.get_daily_summary.return_value = summary

    await _setup(hass, config_entry)

    assert _state(hass, "stress_category").state == STATE_UNKNOWN
