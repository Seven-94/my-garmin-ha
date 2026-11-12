"""Sensors: daily summary, last night's sleep and HRV, weight, last activity."""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, TypedDict, Unpack

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfLength,
    UnitOfMass,
    UnitOfTime,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import MyGarminConfigEntry
from .api import JsonObject
from .coordinator import MyGarminCoordinator
from .entity import MyGarminEntity
from .key_expiry import key_expires

# Not every Garmin unit has a Home Assistant constant.
UNIT_STEPS = "steps"
UNIT_FLOORS = "floors"
UNIT_KCAL = "kcal"
UNIT_BPM = "bpm"
UNIT_BREATHS = "br/min"
UNIT_PUSHES = "pushes"

type Number = float | int | None
type StateValue = Number | datetime | str

_LOGGER = logging.getLogger(__name__)

SLEEP_QUALITIES = ["excellent", "good", "fair", "poor"]
STRESS_CATEGORIES = [
    "calm",
    "balanced",
    "stressful",
    "very_stressful",
    "calm_awake",
    "balanced_awake",
    "stressful_awake",
    "very_stressful_awake",
]
BREATHING_DISRUPTIONS = ["none", "low", "medium", "high"]
HRV_STATUSES = ["none", "poor", "low", "unbalanced", "balanced"]
_NO_DATA = {"invalid", "unmeasurable"}
# Unknown values are logged once per run, not at every refresh.
_REPORTED_VALUES: set[tuple[str, str]] = set()


def _measure(
    key: str, *, zero_is_missing: bool = False, signed: bool = False
) -> Callable[[JsonObject], Number]:
    """Read a number; Garmin sends null or a negative value when not measured.

    Some readings also use 0 for "no reading" (a heart rate is never 0), while
    a count such as steps can genuinely be 0. A few values, such as the
    calories left for the day, are genuinely negative.
    """

    def read(data: JsonObject) -> Number:
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        if (value < 0 and not signed) or (zero_is_missing and value == 0):
            return None
        return value

    return read


def _intensity_minutes(data: JsonObject) -> Number:
    # Garmin's own count: a vigorous minute is worth two moderate ones.
    moderate = _measure("moderateIntensityMinutes")(data)
    vigorous = _measure("vigorousIntensityMinutes")(data)
    if moderate is None and vigorous is None:
        return None
    return (moderate or 0) + 2 * (vigorous or 0)


def _utc(value: object) -> datetime | None:
    """A Garmin *GMT time: milliseconds since the epoch, or an ISO string."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value / 1000, tz=UTC) if value > 0 else None
    if not isinstance(value, str):
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        return None
    # Garmin's *GMT strings carry no offset: they are UTC.
    return parsed.replace(tzinfo=dt_util.UTC) if parsed.tzinfo is None else parsed


def _last_sync(data: JsonObject) -> StateValue:
    return _utc(data.get("lastSyncTimestampGMT"))


def _timestamp(key: str) -> Callable[[JsonObject], StateValue]:
    def read(data: JsonObject) -> StateValue:
        return _utc(data.get(key))

    return read


def _nested(data: JsonObject, key: str) -> dict[str, Any]:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _clock(minutes: object) -> str | None:
    """Minutes around midnight as HH:MM; negative ones fall the evening before."""
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        return None
    minutes %= 24 * 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _code(value: object) -> str | None:
    """A Garmin code shown as an attribute, in Home Assistant's lower case."""
    return value.lower() if isinstance(value, str) else None


def _score_item(data: JsonObject, component: str) -> dict[str, Any]:
    return _nested(_nested(data, "sleepScores"), component)


def _sleep_score(data: JsonObject) -> StateValue:
    return _measure("value", zero_is_missing=True)(_score_item(data, "overall"))


def _option(value: object, options: list[str], what: str) -> StateValue:
    """Map a Garmin enum value to one of the sensor options."""
    if not isinstance(value, str):
        return None
    option = value.lower()
    if option in options:
        return option
    # Garmin's own way to say "no data", not a new value.
    if option in _NO_DATA:
        return None
    if (what, option) not in _REPORTED_VALUES:
        _REPORTED_VALUES.add((what, option))
        _LOGGER.warning("Unknown %s from Garmin: %s", what, value)
    return None


def _sleep_quality(data: JsonObject) -> StateValue:
    value = _score_item(data, "overall").get("qualifierKey")
    return _option(value, SLEEP_QUALITIES, "sleep quality")


def _stress_category(data: JsonObject) -> StateValue:
    return _option(data.get("stressQualifier"), STRESS_CATEGORIES, "stress category")


def _night_of(data: JsonObject) -> dict[str, Any]:
    return {"night_of": data.get("calendarDate")}


@dataclass(frozen=True, kw_only=True)
class MyGarminSensorDescription(SensorEntityDescription):
    value_fn: Callable[[JsonObject], StateValue]
    attributes_fn: Callable[[JsonObject], dict[str, Any]] | None = None


class _NumberOptions(TypedDict, total=False):
    device_class: SensorDeviceClass
    suggested_unit: str
    precision: int
    signed: bool
    enabled: bool


def _number(
    key: str,
    field: str,
    unit: str | None = None,
    *,
    state_class: SensorStateClass | None = SensorStateClass.MEASUREMENT,
    zero_is_missing: bool = False,
    **options: Unpack[_NumberOptions],
) -> MyGarminSensorDescription:
    """A number read straight from one Garmin field."""
    return MyGarminSensorDescription(
        key=key,
        translation_key=key,
        native_unit_of_measurement=unit,
        device_class=options.get("device_class"),
        suggested_unit_of_measurement=options.get("suggested_unit"),
        suggested_display_precision=options.get("precision"),
        state_class=state_class,
        entity_registry_enabled_default=options.get("enabled", True),
        value_fn=_measure(
            field,
            zero_is_missing=zero_is_missing,
            signed=options.get("signed", False),
        ),
    )


def _counter(
    key: str, field: str, unit: str | None = None, **options: Unpack[_NumberOptions]
) -> MyGarminSensorDescription:
    """A total that grows during the day and restarts at midnight."""
    return _number(
        key, field, unit, state_class=SensorStateClass.TOTAL_INCREASING, **options
    )


def _reading(
    key: str, field: str, unit: str | None = None, **options: Unpack[_NumberOptions]
) -> MyGarminSensorDescription:
    """A physiological reading, where 0 means "not measured"."""
    return _number(key, field, unit, zero_is_missing=True, **options)


def _goal(
    key: str, field: str, unit: str, **options: Unpack[_NumberOptions]
) -> MyGarminSensorDescription:
    """A target set by the user: not a measurement, so no statistics."""
    return _number(key, field, unit, state_class=None, **options)


def _time_spent(
    key: str,
    field: str,
    suggested_unit: UnitOfTime = UnitOfTime.MINUTES,
    *,
    enabled: bool = True,
) -> MyGarminSensorDescription:
    """Seconds spent in a state today."""
    return _counter(
        key,
        field,
        UnitOfTime.SECONDS,
        device_class=SensorDeviceClass.DURATION,
        suggested_unit=suggested_unit,
        precision=0 if suggested_unit is UnitOfTime.MINUTES else 1,
        enabled=enabled,
    )


def _distance(
    key: str, field: str, *, enabled: bool = True
) -> MyGarminSensorDescription:
    """Metres travelled today, shown in kilometres."""
    return _counter(
        key,
        field,
        UnitOfLength.METERS,
        device_class=SensorDeviceClass.DISTANCE,
        suggested_unit=UnitOfLength.KILOMETERS,
        precision=2,
        enabled=enabled,
    )


def _percentage(
    key: str, field: str, *, enabled: bool = True
) -> MyGarminSensorDescription:
    return _number(key, field, PERCENTAGE, enabled=enabled)


SENSORS: tuple[MyGarminSensorDescription, ...] = (
    _counter("steps", "totalSteps", UNIT_STEPS),
    _goal("step_goal", "dailyStepGoal", UNIT_STEPS),
    _distance("distance", "totalDistanceMeters"),
    _distance("wellness_distance", "wellnessDistanceMeters", enabled=False),
    # Activity time
    _time_spent("highly_active_time", "highlyActiveSeconds"),
    _time_spent("active_time", "activeSeconds"),
    _time_spent("sedentary_time", "sedentarySeconds", UnitOfTime.HOURS),
    _time_spent("sleeping_time", "sleepingSeconds", UnitOfTime.HOURS, enabled=False),
    MyGarminSensorDescription(
        key="intensity_minutes",
        translation_key="intensity_minutes",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=_intensity_minutes,
    ),
    _goal(
        "intensity_minutes_goal",
        "intensityMinutesGoal",
        UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
    ),
    # Floors
    _counter("floors_ascended", "floorsAscended", UNIT_FLOORS, precision=0),
    _counter("floors_descended", "floorsDescended", UNIT_FLOORS, precision=0),
    _goal("floors_goal", "userFloorsAscendedGoal", UNIT_FLOORS),
    _counter(
        "floors_ascended_distance",
        "floorsAscendedInMeters",
        UnitOfLength.METERS,
        device_class=SensorDeviceClass.DISTANCE,
        precision=0,
        enabled=False,
    ),
    _counter(
        "floors_descended_distance",
        "floorsDescendedInMeters",
        UnitOfLength.METERS,
        device_class=SensorDeviceClass.DISTANCE,
        precision=0,
        enabled=False,
    ),
    # Calories
    _counter("active_calories", "activeKilocalories", UNIT_KCAL),
    _counter("total_calories", "totalKilocalories", UNIT_KCAL, enabled=False),
    _counter("bmr_calories", "bmrKilocalories", UNIT_KCAL, enabled=False),
    # Logged food can be deleted: not a total that only grows.
    _number("consumed_calories", "consumedKilocalories", UNIT_KCAL, enabled=False),
    _number(
        "remaining_calories",
        "remainingKilocalories",
        UNIT_KCAL,
        signed=True,
        enabled=False,
    ),
    _goal("net_calorie_goal", "netCalorieGoal", UNIT_KCAL, enabled=False),
    # Wheelchair
    _counter("pushes", "totalPushes", UNIT_PUSHES, enabled=False),
    _distance("push_distance", "totalPushDistance", enabled=False),
    _goal("push_goal", "dailyPushesGoal", UNIT_PUSHES, enabled=False),
    # Heart rate
    _reading("resting_heart_rate", "restingHeartRate", UNIT_BPM),
    _reading("resting_heart_rate_7_days", "lastSevenDaysAvgRestingHeartRate", UNIT_BPM),
    _reading("min_heart_rate", "minHeartRate", UNIT_BPM, enabled=False),
    _reading("max_heart_rate", "maxHeartRate", UNIT_BPM, enabled=False),
    _reading("min_avg_heart_rate", "minAvgHeartRate", UNIT_BPM, enabled=False),
    _reading("max_avg_heart_rate", "maxAvgHeartRate", UNIT_BPM, enabled=False),
    _counter("abnormal_heart_rate_alerts", "abnormalHeartRateAlertsCount"),
    # Stress: -1 and -2 mean "not enough data"; 0 is a genuine, calm reading.
    _number("average_stress", "averageStressLevel"),
    _number("max_stress", "maxStressLevel"),
    MyGarminSensorDescription(
        key="stress_category",
        translation_key="stress_category",
        device_class=SensorDeviceClass.ENUM,
        options=STRESS_CATEGORIES,
        value_fn=_stress_category,
    ),
    _time_spent("rest_stress_duration", "restStressDuration"),
    _time_spent("low_stress_duration", "lowStressDuration"),
    _time_spent("medium_stress_duration", "mediumStressDuration"),
    _time_spent("high_stress_duration", "highStressDuration"),
    _time_spent("stress_duration", "stressDuration", enabled=False),
    _time_spent("activity_stress_duration", "activityStressDuration", enabled=False),
    _time_spent(
        "uncategorized_stress_duration", "uncategorizedStressDuration", enabled=False
    ),
    _time_spent("total_stress_duration", "totalStressDuration", enabled=False),
    _percentage("stress_percentage", "stressPercentage", enabled=False),
    _percentage("rest_stress_percentage", "restStressPercentage", enabled=False),
    _percentage(
        "activity_stress_percentage", "activityStressPercentage", enabled=False
    ),
    _percentage(
        "uncategorized_stress_percentage",
        "uncategorizedStressPercentage",
        enabled=False,
    ),
    _percentage("low_stress_percentage", "lowStressPercentage", enabled=False),
    _percentage("medium_stress_percentage", "mediumStressPercentage", enabled=False),
    _percentage("high_stress_percentage", "highStressPercentage", enabled=False),
    _time_spent(
        "measurable_awake_time",
        "measurableAwakeDuration",
        UnitOfTime.HOURS,
        enabled=False,
    ),
    _time_spent(
        "measurable_asleep_time",
        "measurableAsleepDuration",
        UnitOfTime.HOURS,
        enabled=False,
    ),
    # Body Battery
    _percentage("body_battery", "bodyBatteryMostRecentValue"),
    _percentage("body_battery_highest", "bodyBatteryHighestValue"),
    _percentage("body_battery_lowest", "bodyBatteryLowestValue"),
    _counter("body_battery_charged", "bodyBatteryChargedValue", PERCENTAGE),
    _counter("body_battery_drained", "bodyBatteryDrainedValue", PERCENTAGE),
    _percentage("body_battery_during_sleep", "bodyBatteryDuringSleep"),
    _percentage("body_battery_at_wake", "bodyBatteryAtWakeTime"),
    # Respiration and SpO2
    _reading("respiration", "avgWakingRespirationValue", UNIT_BREATHS),
    _reading("respiration_latest", "latestRespirationValue", UNIT_BREATHS),
    _reading(
        "respiration_highest", "highestRespirationValue", UNIT_BREATHS, enabled=False
    ),
    _reading(
        "respiration_lowest", "lowestRespirationValue", UNIT_BREATHS, enabled=False
    ),
    _reading("spo2", "averageSpo2", PERCENTAGE),
    _reading("spo2_lowest", "lowestSpo2", PERCENTAGE),
    _reading("spo2_latest", "latestSpo2", PERCENTAGE),
    # Hydration
    _counter(
        "hydration_intake",
        "hydrationValueInML",
        UnitOfVolume.MILLILITERS,
        device_class=SensorDeviceClass.VOLUME,
        enabled=False,
    ),
    _goal(
        "hydration_goal",
        "hydrationGoalInML",
        UnitOfVolume.MILLILITERS,
        device_class=SensorDeviceClass.VOLUME,
        enabled=False,
    ),
    MyGarminSensorDescription(
        key="last_sync",
        translation_key="last_sync",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_last_sync,
    ),
)


def _last_night(
    description: MyGarminSensorDescription,
    attributes_fn: Callable[[JsonObject], dict[str, Any]] = _night_of,
) -> MyGarminSensorDescription:
    """A sleep sensor: it tells which night it shows."""
    return replace(description, attributes_fn=attributes_fn)


def _sleep_duration(
    key: str,
    field: str,
    suggested_unit: UnitOfTime = UnitOfTime.MINUTES,
    *,
    enabled: bool = True,
    attributes_fn: Callable[[JsonObject], dict[str, Any]] = _night_of,
) -> MyGarminSensorDescription:
    # A stage can genuinely last 0 s; a missing night is caught upstream.
    return _last_night(
        _number(
            key,
            field,
            UnitOfTime.SECONDS,
            device_class=SensorDeviceClass.DURATION,
            suggested_unit=suggested_unit,
            precision=1 if suggested_unit is UnitOfTime.HOURS else 0,
            enabled=enabled,
        ),
        attributes_fn,
    )


def _naps(data: JsonObject) -> dict[str, Any]:
    naps = data.get("dailyNapDTOS")
    return {
        **_night_of(data),
        "naps": [
            {
                "start": _isoformat(_utc(nap.get("napStartTimestampGMT"))),
                "end": _isoformat(_utc(nap.get("napEndTimestampGMT"))),
                "duration": _measure("napTimeSec")(nap),
            }
            for nap in (naps if isinstance(naps, list) else [])
            if isinstance(nap, dict)
        ],
    }


def _isoformat(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def _score_feedback(data: JsonObject) -> dict[str, Any]:
    return {
        **_night_of(data),
        "feedback": _code(data.get("sleepScoreFeedback")),
        "insight": _code(data.get("sleepScoreInsight")),
        "personalized_insight": _code(data.get("sleepScorePersonalizedInsight")),
    }


def _spo2_threshold(data: JsonObject) -> dict[str, Any]:
    return {**_night_of(data), "threshold": _measure("spo2Threshold")(data)}


def _breathing_disruption(data: JsonObject) -> StateValue:
    return _option(
        data.get("breathingDisruptionSeverity"),
        BREATHING_DISRUPTIONS,
        "breathing disruption severity",
    )


def _rating(key: str, component: str) -> MyGarminSensorDescription:
    """The qualifier of one sleep score component, with its optimal range."""

    def read(data: JsonObject) -> StateValue:
        value = _score_item(data, component).get("qualifierKey")
        return _option(value, SLEEP_QUALITIES, "sleep rating")

    def attributes(data: JsonObject) -> dict[str, Any]:
        item = _score_item(data, component)
        return {
            **_night_of(data),
            "optimal_start": item.get("optimalStart"),
            "optimal_end": item.get("optimalEnd"),
        }

    return MyGarminSensorDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.ENUM,
        options=SLEEP_QUALITIES,
        entity_registry_enabled_default=False,
        value_fn=read,
        attributes_fn=attributes,
    )


def _stage_percentage(key: str, component: str) -> MyGarminSensorDescription:
    def read(data: JsonObject) -> StateValue:
        return _measure("value")(_score_item(data, component))

    return MyGarminSensorDescription(
        key=key,
        translation_key=key,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=read,
        attributes_fn=_night_of,
    )


def _sleep_need(key: str, field: str) -> MyGarminSensorDescription:
    """Garmin's sleep need, in minutes, with the recommended bedtime."""

    def read(data: JsonObject) -> StateValue:
        return _measure("actual", zero_is_missing=True)(_nested(data, field))

    def attributes(data: JsonObject) -> dict[str, Any]:
        need = _nested(data, field)
        return {
            **_night_of(data),
            "baseline": _measure("baseline", zero_is_missing=True)(need),
            "recommended_bedtime_start": _clock(
                need.get("recommendedBedtimeStartMins")
            ),
            "recommended_bedtime_end": _clock(need.get("recommendedBedtimeEndMins")),
        }

    return MyGarminSensorDescription(
        key=key,
        translation_key=key,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_unit_of_measurement=UnitOfTime.HOURS,
        suggested_display_precision=1,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=read,
        attributes_fn=attributes,
    )


# Alignment attribute, Garmin field (signed minutes around midnight).
_ALIGNMENT_TIMES = {
    "optimal_window_start": "optimalSleepWindowStartMins",
    "optimal_window_end": "optimalSleepWindowEndMins",
    "optimal_window_midpoint": "optimalSleepWindowMidpointMins",
    "last_sleep_midpoint": "lastSleepMidpointMins",
}


def _alignment_status(data: JsonObject) -> StateValue:
    # Free text in the spec: no list of values to build an enum from.
    return _code(_nested(data, "sleepAlignment").get("status"))


def _alignment(data: JsonObject) -> dict[str, Any]:
    alignment = _nested(data, "sleepAlignment")
    times = {
        name: _clock(alignment.get(field)) for name, field in _ALIGNMENT_TIMES.items()
    }
    return {**_night_of(data), **{k: v for k, v in times.items() if v is not None}}


SLEEP_SENSORS: tuple[MyGarminSensorDescription, ...] = (
    _sleep_duration("sleep_duration", "sleepTimeSeconds", UnitOfTime.HOURS),
    _sleep_duration("deep_sleep", "deepSleepSeconds"),
    _sleep_duration("light_sleep", "lightSleepSeconds"),
    _sleep_duration("rem_sleep", "remSleepSeconds"),
    _sleep_duration("awake_time", "awakeSleepSeconds"),
    _sleep_duration("nap_time", "napTimeSeconds", attributes_fn=_naps),
    _sleep_duration("unmeasurable_sleep", "unmeasurableSleepSeconds", enabled=False),
    _last_night(_number("awake_count", "awakeCount")),
    MyGarminSensorDescription(
        key="sleep_score",
        translation_key="sleep_score",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_sleep_score,
        attributes_fn=_score_feedback,
    ),
    MyGarminSensorDescription(
        key="sleep_quality",
        translation_key="sleep_quality",
        device_class=SensorDeviceClass.ENUM,
        options=SLEEP_QUALITIES,
        value_fn=_sleep_quality,
        attributes_fn=_night_of,
    ),
    _rating("sleep_duration_rating", "totalDuration"),
    _rating("sleep_stress_rating", "stress"),
    _rating("awake_count_rating", "awakeCount"),
    _rating("rem_sleep_rating", "remPercentage"),
    _rating("light_sleep_rating", "lightPercentage"),
    _rating("deep_sleep_rating", "deepPercentage"),
    _rating("restlessness_rating", "restlessness"),
    _stage_percentage("rem_sleep_percentage", "remPercentage"),
    _stage_percentage("light_sleep_percentage", "lightPercentage"),
    _stage_percentage("deep_sleep_percentage", "deepPercentage"),
    MyGarminSensorDescription(
        key="bedtime",
        translation_key="bedtime",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_timestamp("sleepStartTimestampGMT"),
        attributes_fn=_night_of,
    ),
    MyGarminSensorDescription(
        key="wake_time",
        translation_key="wake_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_timestamp("sleepEndTimestampGMT"),
        attributes_fn=_night_of,
    ),
    # Stress: 0 is a genuine, calm reading.
    _last_night(_number("sleep_stress", "avgSleepStress")),
    _last_night(_reading("sleep_heart_rate", "avgHeartRate", UNIT_BPM)),
    _last_night(_reading("sleep_respiration", "averageRespirationValue", UNIT_BREATHS)),
    _last_night(
        _reading(
            "sleep_respiration_highest",
            "highestRespirationValue",
            UNIT_BREATHS,
            enabled=False,
        )
    ),
    _last_night(
        _reading(
            "sleep_respiration_lowest",
            "lowestRespirationValue",
            UNIT_BREATHS,
            enabled=False,
        )
    ),
    _last_night(_reading("sleep_spo2", "averageSpO2Value", PERCENTAGE)),
    _last_night(_reading("sleep_spo2_lowest", "lowestSpO2Value", PERCENTAGE)),
    _last_night(
        _reading("sleep_spo2_highest", "highestSpO2Value", PERCENTAGE, enabled=False)
    ),
    _last_night(
        _reading("sleep_spo2_heart_rate", "averageSpO2HRSleep", UNIT_BPM, enabled=False)
    ),
    _last_night(
        _number("sleep_spo2_events", "numberOfEventsBelowThreshold", enabled=False),
        _spo2_threshold,
    ),
    MyGarminSensorDescription(
        key="breathing_disruption",
        translation_key="breathing_disruption",
        device_class=SensorDeviceClass.ENUM,
        options=BREATHING_DISRUPTIONS,
        value_fn=_breathing_disruption,
        attributes_fn=_night_of,
    ),
    _sleep_need("sleep_need", "sleepNeed"),
    _sleep_need("next_sleep_need", "nextSleepNeed"),
    MyGarminSensorDescription(
        key="sleep_alignment",
        translation_key="sleep_alignment",
        entity_registry_enabled_default=False,
        value_fn=_alignment_status,
        attributes_fn=_alignment,
    ),
)


def _hrv_summary(data: JsonObject) -> dict[str, Any]:
    # Unlike sleep, the HRV night and its date sit under hrvSummary.
    return _nested(data, "hrvSummary")


def _hrv_night_of(data: JsonObject) -> dict[str, Any]:
    return {"night_of": _hrv_summary(data).get("calendarDate")}


def _hrv(key: str, field: str, *, enabled: bool = True) -> MyGarminSensorDescription:
    """An HRV value in milliseconds.

    No device class: duration would let users convert HRV to hours.
    """
    read = _measure(field, zero_is_missing=True)
    return MyGarminSensorDescription(
        key=key,
        translation_key=key,
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=enabled,
        value_fn=lambda data: read(_hrv_summary(data)),
        attributes_fn=_hrv_night_of,
    )


def _hrv_status(data: JsonObject) -> StateValue:
    return _option(_hrv_summary(data).get("status"), HRV_STATUSES, "HRV status")


_BASELINE_BOUNDS = {
    "low_upper": "lowUpper",
    "balanced_low": "balancedLow",
    "balanced_upper": "balancedUpper",
    "marker_value": "markerValue",
}


def _hrv_baseline(data: JsonObject) -> dict[str, Any]:
    summary = _hrv_summary(data)
    baseline = _nested(summary, "baseline")
    bounds = {
        name: _measure(field)(baseline) for name, field in _BASELINE_BOUNDS.items()
    }
    return {
        **_hrv_night_of(data),
        **{name: value for name, value in bounds.items() if value is not None},
        "feedback": _code(summary.get("feedbackPhrase")),
    }


HRV_SENSORS: tuple[MyGarminSensorDescription, ...] = (
    _hrv("hrv_last_night", "lastNightAvg"),
    _hrv("hrv_weekly_average", "weeklyAvg"),
    _hrv("hrv_last_night_high", "lastNight5MinHigh", enabled=False),
    MyGarminSensorDescription(
        key="hrv_status",
        translation_key="hrv_status",
        device_class=SensorDeviceClass.ENUM,
        options=HRV_STATUSES,
        value_fn=_hrv_status,
        attributes_fn=_hrv_baseline,
    ),
)


def _weigh_in(data: JsonObject) -> dict[str, Any]:
    return _nested(data, "weigh_in")


def _to_grams(weigh_in: JsonObject) -> int:
    """Factor turning this weigh-in's masses into grams.

    The spec says kilograms but Garmin sends grams. Nobody weighs a tonne, nor
    less than a kilogram: the weight itself tells the unit.
    """
    weight = weigh_in.get("weight")
    if isinstance(weight, int | float) and 0 < weight < 1000:
        return 1000
    return 1


def _weighed(field: str, *, signed: bool = False) -> Callable[[JsonObject], Number]:
    """A mass of the latest weigh-in, in grams; a change can be 0 or negative."""
    read = _measure(field, zero_is_missing=not signed, signed=signed)

    def value(data: JsonObject) -> Number:
        weigh_in = _weigh_in(data)
        mass = read(weigh_in)
        return None if mass is None else mass * _to_grams(weigh_in)

    return value


def _weigh_in_attributes(data: JsonObject) -> dict[str, Any]:
    weigh_in = _weigh_in(data)
    measured_at = _utc(weigh_in.get("timestampGMT"))
    return {
        "measured_at": measured_at.isoformat() if measured_at else None,
        "source": _code(weigh_in.get("sourceType")),
    }


def _latest(collection: str) -> Callable[[JsonObject], dict[str, Any]]:
    """The goal (or goal range) in effect: Garmin lists the latest last."""

    def read(data: JsonObject) -> dict[str, Any]:
        items = _nested(data, "goal").get(collection)
        if not isinstance(items, list):
            return {}
        return next((i for i in reversed(items) if isinstance(i, dict)), {})

    return read


_weight_goal = _latest("userGoals")
_weight_goal_range = _latest("userGoalRanges")


def _goal_grams(
    item: Callable[[JsonObject], dict[str, Any]], field: str
) -> Callable[[JsonObject], Number]:
    # Goals are always in grams (userGoalType WEIGHT_GRAMS).
    read = _measure(field, zero_is_missing=True)
    return lambda data: read(item(data))


def _goal_attributes(data: JsonObject) -> dict[str, Any]:
    goal = _weight_goal(data)
    return {
        "category": _code(goal.get("userGoalCategory")),
        "tracking_period": _code(goal.get("trackingPeriodType")),
    }


def _mass(
    key: str,
    value_fn: Callable[[JsonObject], StateValue],
    attributes_fn: Callable[[JsonObject], dict[str, Any]] | None,
    *,
    state_class: SensorStateClass | None = SensorStateClass.MEASUREMENT,
    enabled: bool = True,
) -> MyGarminSensorDescription:
    """A mass in grams, shown in kilograms (or the user's own unit)."""
    return MyGarminSensorDescription(
        key=key,
        translation_key=key,
        native_unit_of_measurement=UnitOfMass.GRAMS,
        suggested_unit_of_measurement=UnitOfMass.KILOGRAMS,
        suggested_display_precision=1,
        device_class=SensorDeviceClass.WEIGHT,
        state_class=state_class,
        entity_registry_enabled_default=enabled,
        value_fn=value_fn,
        attributes_fn=attributes_fn,
    )


def _body(description: MyGarminSensorDescription) -> MyGarminSensorDescription:
    """A body composition reading of the latest weigh-in."""
    read = description.value_fn
    return replace(
        description,
        value_fn=lambda data: read(_weigh_in(data)),
        attributes_fn=_weigh_in_attributes,
    )


WEIGHT_SENSORS: tuple[MyGarminSensorDescription, ...] = (
    _mass("weight", _weighed("weight"), _weigh_in_attributes),
    _mass("weight_change", _weighed("weightDelta", signed=True), _weigh_in_attributes),
    _mass("muscle_mass", _weighed("muscleMass"), _weigh_in_attributes),
    _mass("bone_mass", _weighed("boneMass"), _weigh_in_attributes, enabled=False),
    _body(_reading("bmi", "bmi", precision=1)),
    _body(_reading("body_fat", "bodyFat", PERCENTAGE, precision=1)),
    _body(_reading("body_water", "bodyWater", PERCENTAGE, precision=1)),
    _body(_reading("visceral_fat", "visceralFat", enabled=False)),
    _body(_reading("metabolic_age", "metabolicAge", UnitOfTime.YEARS, enabled=False)),
    _body(_reading("physique_rating", "physiqueRating", enabled=False)),
    _mass(
        "weight_goal",
        _goal_grams(_weight_goal, "goalValue"),
        _goal_attributes,
        state_class=None,
    ),
    _mass(
        "weight_goal_low",
        _goal_grams(_weight_goal_range, "lowWeightGoalInGrams"),
        None,
        state_class=None,
        enabled=False,
    ),
    _mass(
        "weight_goal_high",
        _goal_grams(_weight_goal_range, "highWeightGoalInGrams"),
        None,
        state_class=None,
        enabled=False,
    ),
)


# Home Assistant states hold at most 255 characters.
_MAX_STATE_LENGTH = 255
# Social and profile data, places, and fields shown another way.
_HIDDEN_ACTIVITY_FIELDS = {
    "activityName",
    "description",
    "activityUUID",
    "startLatitude",
    "startLongitude",
    "endLatitude",
    "endLongitude",
    "locationName",
    "numberOfActivityLikes",
    "numberOfActivityComments",
    "likedByUser",
    "commentedByUser",
    "requestorRelationship",
    "videoUrl",
    "startTimeLocal",
    "startTimeGMT",
    "endTimeGMT",
    "beginTimestamp",
    # Same as their isX twin.
    "parent",
    "decoDive",
    "elevationCorrected",
    "pr",
    "manualActivity",
    "favorite",
    "autoCalcCalories",
}
_HIDDEN_ACTIVITY_PREFIXES = ("owner", "activityLike")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _snake(key: str) -> str:
    return _CAMEL_BOUNDARY.sub("_", key).lower()


def last_activity(data: JsonObject) -> dict[str, Any]:
    """The newest activity: the coordinator sorts them newest first."""
    activities = data.get("activities")
    if not isinstance(activities, list) or not activities:
        return {}
    first = activities[0]
    return first if isinstance(first, dict) else {}


def _activity_name(data: JsonObject) -> StateValue:
    name = last_activity(data).get("activityName")
    return name[:_MAX_STATE_LENGTH] if isinstance(name, str) else None


def _iso(value: object) -> str | None:
    moment = _utc(value)
    return moment.isoformat() if moment else None


def activity_summary(activity: JsonObject) -> dict[str, Any]:
    """Who, what and when of an activity, for attributes and events."""
    return {
        "activity_id": activity.get("activityId"),
        "activity_type": _nested(activity, "activityType").get("typeKey"),
        "start": _iso(activity.get("startTimeGMT")),
    }


def _activity_attributes(data: JsonObject) -> dict[str, Any]:
    """Every plain field of the summary, so no device-specific value is lost."""
    activity = last_activity(data)
    if not activity:
        return {}
    fields = {
        _snake(key): value
        for key, value in activity.items()
        if isinstance(value, str | int | float)  # bool is an int
        and key not in _HIDDEN_ACTIVITY_FIELDS
        and not key.startswith(_HIDDEN_ACTIVITY_PREFIXES)
    }
    return {
        **fields,
        **activity_summary(activity),
        "event_type": _nested(activity, "eventType").get("typeKey"),
        "end": _iso(activity.get("endTimeGMT")),
    }


ACTIVITY_SENSORS: tuple[MyGarminSensorDescription, ...] = (
    MyGarminSensorDescription(
        key="last_activity",
        translation_key="last_activity",
        value_fn=_activity_name,
        attributes_fn=_activity_attributes,
    ),
)


ZONE_TRAINING_METHODS = ["hr_max", "lactate_threshold", "hr_reserve", "custom"]
# Attribute name of each zone field, as shown for every sport.
_ZONE_FIELDS = {
    "max_heart_rate": "maxHeartRateUsed",
    "lactate_threshold_heart_rate": "lactateThresholdHeartRateUsed",
    "resting_heart_rate": "restingHeartRateUsed",
    "zone_1_floor": "zone1Floor",
    "zone_2_floor": "zone2Floor",
    "zone_3_floor": "zone3Floor",
    "zone_4_floor": "zone4Floor",
    "zone_5_floor": "zone5Floor",
}


def _zone_sports(data: JsonObject) -> list[JsonObject]:
    zones = data.get("zones")
    if not isinstance(zones, list):
        return []
    return [sport for sport in zones if isinstance(sport, dict)]


def _default_zones(data: JsonObject) -> JsonObject:
    """The DEFAULT sport's zones, or the first sport's when Garmin sends none."""
    sports = _zone_sports(data)
    first = next(iter(sports), {})
    return next((s for s in sports if s.get("sport") == "DEFAULT"), first)


def _other_zones(data: JsonObject) -> list[JsonObject]:
    default = _default_zones(data)
    return [sport for sport in _zone_sports(data) if sport is not default]


def _zone_summary(sport: JsonObject) -> dict[str, Any]:
    return {
        "sport": _code(sport.get("sport")),
        "training_method": _code(sport.get("trainingMethod")),
        **{
            name: _measure(field, zero_is_missing=True)(sport)
            for name, field in _ZONE_FIELDS.items()
        },
    }


def _zone_attributes(data: JsonObject) -> dict[str, Any]:
    """The sport the values belong to, and the zones of every other sport."""
    return {
        "sport": _code(_default_zones(data).get("sport")),
        "other_sports": [_zone_summary(sport) for sport in _other_zones(data)],
    }


def _resting_zone_attributes(data: JsonObject) -> dict[str, Any]:
    auto_update = _default_zones(data).get("restingHrAutoUpdateUsed")
    return {
        **_zone_attributes(data),
        "resting_heart_rate_auto_update": (
            auto_update if isinstance(auto_update, bool) else None
        ),
    }


def _zone_training_method(data: JsonObject) -> StateValue:
    # An empty string is no answer either: only a value Garmin invents is logged.
    method = _default_zones(data).get("trainingMethod")
    return _option(method or None, ZONE_TRAINING_METHODS, "heart rate zones method")


def _zone_setting(
    key: str,
    field: str,
    *,
    enabled: bool = True,
    attributes_fn: Callable[[JsonObject], dict[str, Any]] = _zone_attributes,
) -> MyGarminSensorDescription:
    """A heart rate zone of the default sport, in bpm: a setting, not a measurement."""
    read = _measure(field, zero_is_missing=True)
    return MyGarminSensorDescription(
        key=key,
        translation_key=key,
        native_unit_of_measurement=UNIT_BPM,
        state_class=None,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=enabled,
        value_fn=lambda data: read(_default_zones(data)),
        attributes_fn=attributes_fn,
    )


HEART_RATE_ZONE_SENSORS: tuple[MyGarminSensorDescription, ...] = (
    _zone_setting("zones_max_heart_rate", "maxHeartRateUsed"),
    _zone_setting(
        "zones_lactate_threshold_heart_rate",
        "lactateThresholdHeartRateUsed",
        enabled=False,
    ),
    _zone_setting(
        "zones_resting_heart_rate",
        "restingHeartRateUsed",
        attributes_fn=_resting_zone_attributes,
    ),
    _zone_setting("zones_zone_1_floor", "zone1Floor"),
    _zone_setting("zones_zone_2_floor", "zone2Floor"),
    _zone_setting("zones_zone_3_floor", "zone3Floor"),
    _zone_setting("zones_zone_4_floor", "zone4Floor"),
    _zone_setting("zones_zone_5_floor", "zone5Floor"),
    MyGarminSensorDescription(
        key="zones_training_method",
        translation_key="zones_training_method",
        device_class=SensorDeviceClass.ENUM,
        options=ZONE_TRAINING_METHODS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_zone_training_method,
        attributes_fn=_zone_attributes,
    ),
)


KEY_EXPIRY_SENSOR = SensorEntityDescription(
    key="key_expiry",
    translation_key="key_expiry",
    device_class=SensorDeviceClass.DATE,
    entity_category=EntityCategory.DIAGNOSTIC,
)

# Attribute name and Garmin field, in UTC: the local times would repeat them.
SPO2_TIMES = {
    "window_start": "startTimestampGMT",
    "window_end": "endTimestampGMT",
    "measured_at": "latestSpO2TimestampGMT",
    "sleep_start": "sleepStartTimestampGMT",
    "sleep_end": "sleepEndTimestampGMT",
    "tomorrow_sleep_start": "tomorrowSleepStartTimestampGMT",
    "tomorrow_sleep_end": "tomorrowSleepEndTimestampGMT",
}


def _spo2_attributes(data: JsonObject) -> dict[str, Any]:
    times = {name: _utc(data.get(field)) for name, field in SPO2_TIMES.items()}
    return {
        "day": data.get("calendarDate"),
        **{name: moment.isoformat() for name, moment in times.items() if moment},
    }


def _spo2(
    key: str, field: str, *, precision: int = 0, enabled: bool = True
) -> MyGarminSensorDescription:
    """A blood oxygen saturation of the day, in percent; 0 means not measured."""
    return replace(
        _reading(key, field, PERCENTAGE, precision=precision, enabled=enabled),
        attributes_fn=_spo2_attributes,
    )


SPO2_SENSORS: tuple[MyGarminSensorDescription, ...] = (
    # The daily summary already shows the day's average, lowest and latest.
    _spo2("acclimation_spo2_average", "averageSpO2", precision=1, enabled=False),
    _spo2("acclimation_spo2_lowest", "lowestSpO2", enabled=False),
    _spo2("acclimation_spo2_latest", "latestSpO2", enabled=False),
    _spo2("acclimation_spo2_seven_day_average", "lastSevenDaysAvgSpO2", precision=1),
    _spo2("acclimation_spo2_sleep_average", "avgSleepSpO2", precision=1),
    _spo2(
        "acclimation_spo2_tomorrow_sleep_average",
        "avgTomorrowSleepSpO2",
        precision=1,
        enabled=False,
    ),
)

# Daily summary, not SpO2 response: that one has no altitude. Null for most users.
ALTITUDE_SENSORS: tuple[MyGarminSensorDescription, ...] = (
    _number(
        "acclimation_altitude",
        "averageMonitoringEnvironmentAltitude",
        UnitOfLength.METERS,
        device_class=SensorDeviceClass.DISTANCE,
        signed=True,
        enabled=False,
    ),
)


def _threshold(key: str, field: str) -> MyGarminSensorDescription:
    """An abnormal heart rate alert threshold, in bpm: a setting, not a measurement."""
    return MyGarminSensorDescription(
        key=key,
        translation_key=key,
        native_unit_of_measurement=UNIT_BPM,
        state_class=None,
        entity_category=EntityCategory.DIAGNOSTIC,
        # 0 or null: the alert is off, or Garmin has no value.
        value_fn=_measure(field, zero_is_missing=True),
    )


# The same response carries the day's resting, lowest and highest heart rates:
# the daily summary sensors already show them.
HEART_RATE_THRESHOLD_SENSORS: tuple[MyGarminSensorDescription, ...] = (
    _threshold("abnormal_heart_rate_high_threshold", "abnormalHrThresholdValue"),
    _threshold("abnormal_heart_rate_low_threshold", "lowAbnormalHrThresholdValue"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyGarminConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    del hass
    data = entry.runtime_data
    async_add_entities(
        [
            *(MyGarminSensor(data.daily, description) for description in SENSORS),
            *(MyGarminSensor(data.sleep, description) for description in SLEEP_SENSORS),
            *(MyGarminSensor(data.hrv, description) for description in HRV_SENSORS),
            *(
                MyGarminSensor(data.weight, description)
                for description in WEIGHT_SENSORS
            ),
            *(
                MyGarminSensor(data.activities, description)
                for description in ACTIVITY_SENSORS
            ),
            *(
                MyGarminSensor(data.fitness_age, description)
                for description in FITNESS_AGE_SENSORS
            ),
            *(
                MyGarminSensor(data.heart_rate_zones, description)
                for description in HEART_RATE_ZONE_SENSORS
            ),
            MyGarminKeyExpirySensor(data.daily, KEY_EXPIRY_SENSOR),
            *(MyGarminSensor(data.spo2, description) for description in SPO2_SENSORS),
            *(
                MyGarminSensor(data.heart_rate_thresholds, description)
                for description in HEART_RATE_THRESHOLD_SENSORS
            ),
            *(
                MyGarminSensor(data.daily, description)
                for description in ALTITUDE_SENSORS
            ),
        ]
    )


class MyGarminSensor(MyGarminEntity, SensorEntity):
    def __init__(
        self,
        coordinator: MyGarminCoordinator,
        description: MyGarminSensorDescription,
    ) -> None:
        super().__init__(coordinator, description)
        self._value_fn = description.value_fn
        self._attributes_fn = description.attributes_fn
        self._read()

    def _read(self) -> None:
        # No data yet when the first refresh of this coordinator failed.
        data = self.coordinator.data or {}
        self._attr_native_value = self._value_fn(data)
        if self._attributes_fn is not None:
            self._attr_extra_state_attributes = self._attributes_fn(data)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._read()
        super()._handle_coordinator_update()


class MyGarminKeyExpirySensor(MyGarminEntity, SensorEntity):
    """The key expiry date the user entered: no Garmin request involved."""

    @property
    def available(self) -> bool:
        # Garmin being unreachable does not make the date unknown.
        return True

    def __init__(
        self, coordinator: MyGarminCoordinator, description: SensorEntityDescription
    ) -> None:
        super().__init__(coordinator, description)
        # A new date reloads the entry, which creates this entity again.
        self._attr_native_value = key_expires(coordinator.config_entry)


def _fitness_age(
    key: str, field: str, *, enabled: bool = True
) -> MyGarminSensorDescription:
    """An age in years, as Garmin computes it: one decimal is enough."""
    return _number(key, field, UnitOfTime.YEARS, precision=1, enabled=enabled)


def _fitness_age_attributes(data: JsonObject) -> dict[str, Any]:
    # The chronological age is deliberately never read: it is personal data.
    # Components are keyed by names that vary by device, so every field is kept
    # rather than a fixed list.
    components = {
        _snake(name): {_snake(field): value for field, value in item.items()}
        for name, item in _nested(data, "components").items()
        if isinstance(item, dict)
    }
    return {"components": components, "last_updated": data.get("lastUpdated")}


FITNESS_AGE_INVALID_REASONS = [
    "user_empty_age",
    "user_under_age",
    "user_over_age",
    "user_pregnant",
]


def _fitness_invalid_reason(data: JsonObject) -> StateValue:
    return _option(
        data.get("invalidReason"),
        FITNESS_AGE_INVALID_REASONS,
        "fitness age invalid reason",
    )


FITNESS_AGE_SENSORS: tuple[MyGarminSensorDescription, ...] = (
    replace(
        _fitness_age("fitness_age", "fitnessAge"),
        attributes_fn=_fitness_age_attributes,
    ),
    _fitness_age("achievable_fitness_age", "achievableFitnessAge"),
    _fitness_age("previous_fitness_age", "previousFitnessAge", enabled=False),
    MyGarminSensorDescription(
        key="fitness_age_invalid_reason",
        translation_key="fitness_age_invalid_reason",
        device_class=SensorDeviceClass.ENUM,
        entity_category=EntityCategory.DIAGNOSTIC,
        options=FITNESS_AGE_INVALID_REASONS,
        value_fn=_fitness_invalid_reason,
    ),
)
