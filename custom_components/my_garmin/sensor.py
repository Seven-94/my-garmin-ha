"""Sensors: the daily summary, and last night."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

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

type Number = float | int | None
type StateValue = Number | datetime | str

_LOGGER = logging.getLogger(__name__)

SLEEP_QUALITIES = ["excellent", "good", "fair", "poor"]
# Unknown values are logged once per run, not at every refresh.
_REPORTED_QUALITIES: set[str] = set()


def _measure(
    key: str, *, zero_is_missing: bool = False
) -> Callable[[JsonObject], Number]:
    """Read a number; Garmin sends null or a negative value when not measured.

    Some readings also use 0 for "no reading" (a heart rate is never 0), while
    a count such as steps can genuinely be 0.
    """

    def read(data: JsonObject) -> Number:
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        if value < 0 or (zero_is_missing and value == 0):
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


def _sleep_quality(data: JsonObject) -> StateValue:
    value = _overall_score(data).get("qualifierKey")
    if not isinstance(value, str):
        return None
    quality = value.lower()
    if quality in SLEEP_QUALITIES:
        return quality
    if quality not in _REPORTED_QUALITIES:
        _REPORTED_QUALITIES.add(quality)
        _LOGGER.warning("Unknown sleep quality from Garmin: %s", value)
    return None


def _night_of(data: JsonObject) -> dict[str, Any]:
    return {"night_of": data.get("calendarDate")}


@dataclass(frozen=True, kw_only=True)
class MyGarminSensorDescription(SensorEntityDescription):
    value_fn: Callable[[JsonObject], StateValue]
    attributes_fn: Callable[[JsonObject], dict[str, Any]] | None = None


def _counter(
    key: str,
    field: str,
    unit: str,
    *,
    device_class: SensorDeviceClass | None = None,
    suggested_unit: str | None = None,
    precision: int | None = None,
    enabled: bool = True,
) -> MyGarminSensorDescription:
    """A total that grows during the day and restarts at midnight."""
    return MyGarminSensorDescription(
        key=key,
        translation_key=key,
        native_unit_of_measurement=unit,
        device_class=device_class,
        suggested_unit_of_measurement=suggested_unit,
        suggested_display_precision=precision,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_registry_enabled_default=enabled,
        value_fn=_measure(field),
    )


def _reading(
    key: str, field: str, unit: str, *, enabled: bool = True
) -> MyGarminSensorDescription:
    """A physiological reading, where 0 means "not measured"."""
    return MyGarminSensorDescription(
        key=key,
        translation_key=key,
        native_unit_of_measurement=unit,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=enabled,
        value_fn=_measure(field, zero_is_missing=True),
    )


SENSORS: tuple[MyGarminSensorDescription, ...] = (
    _counter("steps", "totalSteps", UNIT_STEPS),
    MyGarminSensorDescription(
        key="step_goal",
        translation_key="step_goal",
        native_unit_of_measurement=UNIT_STEPS,
        value_fn=_measure("dailyStepGoal"),
    ),
    _counter(
        "distance",
        "totalDistanceMeters",
        UnitOfLength.METERS,
        device_class=SensorDeviceClass.DISTANCE,
        suggested_unit=UnitOfLength.KILOMETERS,
        precision=2,
    ),
    _counter("active_calories", "activeKilocalories", UNIT_KCAL),
    _counter(
        "total_calories",
        "totalKilocalories",
        UNIT_KCAL,
        enabled=False,
    ),
    MyGarminSensorDescription(
        key="intensity_minutes",
        translation_key="intensity_minutes",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=_intensity_minutes,
    ),
    _counter(
        "floors_ascended",
        "floorsAscended",
        UNIT_FLOORS,
        precision=0,
    ),
    _reading("resting_heart_rate", "restingHeartRate", UNIT_BPM),
    _reading(
        "min_heart_rate",
        "minHeartRate",
        UNIT_BPM,
        enabled=False,
    ),
    _reading(
        "max_heart_rate",
        "maxHeartRate",
        UNIT_BPM,
        enabled=False,
    ),
    MyGarminSensorDescription(
        key="average_stress",
        translation_key="average_stress",
        state_class=SensorStateClass.MEASUREMENT,
        # -1 and -2 mean "not enough data"; 0 is a genuine, calm reading.
        value_fn=_measure("averageStressLevel"),
    ),
    MyGarminSensorDescription(
        key="body_battery",
        translation_key="body_battery",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_measure("bodyBatteryMostRecentValue"),
    ),
    MyGarminSensorDescription(
        key="body_battery_highest",
        translation_key="body_battery_highest",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_measure("bodyBatteryHighestValue"),
    ),
    MyGarminSensorDescription(
        key="body_battery_lowest",
        translation_key="body_battery_lowest",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_measure("bodyBatteryLowestValue"),
    ),
    _reading("respiration", "avgWakingRespirationValue", UNIT_BREATHS),
    _reading("spo2", "averageSpo2", PERCENTAGE),
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
