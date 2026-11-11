"""Sensors: the daily summary, and last night."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
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


def _last_sync(data: JsonObject) -> StateValue:
    value = data.get("lastSyncTimestampGMT")
    if not isinstance(value, str):
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        return None
    # Garmin's *GMT fields carry no offset: they are UTC.
    return parsed.replace(tzinfo=dt_util.UTC) if parsed.tzinfo is None else parsed


def _epoch_ms(key: str) -> Callable[[JsonObject], StateValue]:
    """Read a *GMT timestamp sent in milliseconds since the epoch."""

    def read(data: JsonObject) -> StateValue:
        value = _measure(key, zero_is_missing=True)(data)
        if value is None:
            return None
        return datetime.fromtimestamp(value / 1000, tz=UTC)

    return read


def _overall_score(data: JsonObject) -> dict[str, Any]:
    scores = data.get("sleepScores")
    overall = scores.get("overall") if isinstance(scores, dict) else None
    return overall if isinstance(overall, dict) else {}


def _sleep_score(data: JsonObject) -> StateValue:
    return _measure("value", zero_is_missing=True)(_overall_score(data))


def _option(value: object, options: list[str], what: str) -> StateValue:
    """Map a Garmin enum value to one of the sensor options."""
    if not isinstance(value, str):
        return None
    option = value.lower()
    if option in options:
        return option
    if (what, option) not in _REPORTED_VALUES:
        _REPORTED_VALUES.add((what, option))
        _LOGGER.warning("Unknown %s from Garmin: %s", what, value)
    return None


def _sleep_quality(data: JsonObject) -> StateValue:
    value = _overall_score(data).get("qualifierKey")
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


def _sleep_duration(
    key: str, field: str, suggested_unit: UnitOfTime, precision: int
) -> MyGarminSensorDescription:
    return MyGarminSensorDescription(
        key=key,
        translation_key=key,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=suggested_unit,
        suggested_display_precision=precision,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        # A stage can genuinely last 0 s; a missing night is caught upstream.
        value_fn=_measure(field),
        attributes_fn=_night_of,
    )


SLEEP_SENSORS: tuple[MyGarminSensorDescription, ...] = (
    _sleep_duration("sleep_duration", "sleepTimeSeconds", UnitOfTime.HOURS, 1),
    _sleep_duration("deep_sleep", "deepSleepSeconds", UnitOfTime.MINUTES, 0),
    _sleep_duration("light_sleep", "lightSleepSeconds", UnitOfTime.MINUTES, 0),
    _sleep_duration("rem_sleep", "remSleepSeconds", UnitOfTime.MINUTES, 0),
    _sleep_duration("awake_time", "awakeSleepSeconds", UnitOfTime.MINUTES, 0),
    MyGarminSensorDescription(
        key="sleep_score",
        translation_key="sleep_score",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_sleep_score,
        attributes_fn=_night_of,
    ),
    MyGarminSensorDescription(
        key="sleep_quality",
        translation_key="sleep_quality",
        device_class=SensorDeviceClass.ENUM,
        options=SLEEP_QUALITIES,
        value_fn=_sleep_quality,
        attributes_fn=_night_of,
    ),
    MyGarminSensorDescription(
        key="bedtime",
        translation_key="bedtime",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_epoch_ms("sleepStartTimestampGMT"),
        attributes_fn=_night_of,
    ),
    MyGarminSensorDescription(
        key="wake_time",
        translation_key="wake_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_epoch_ms("sleepEndTimestampGMT"),
        attributes_fn=_night_of,
    ),
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
