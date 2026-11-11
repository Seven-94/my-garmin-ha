"""Sensors read from the daily summary."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

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
from .coordinator import DailySummaryCoordinator
from .entity import MyGarminEntity

# Not every Garmin unit has a Home Assistant constant.
UNIT_STEPS = "steps"
UNIT_FLOORS = "floors"
UNIT_KCAL = "kcal"
UNIT_BPM = "bpm"
UNIT_BREATHS = "br/min"

type Number = float | int | None
type StateValue = Number | datetime


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


@dataclass(frozen=True, kw_only=True)
class MyGarminSensorDescription(SensorEntityDescription):
    value_fn: Callable[[JsonObject], StateValue]


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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyGarminConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    del hass
    coordinator = entry.runtime_data.daily
    async_add_entities(
        MyGarminSensor(coordinator, description) for description in SENSORS
    )


class MyGarminSensor(MyGarminEntity, SensorEntity):
    def __init__(
        self,
        coordinator: DailySummaryCoordinator,
        description: MyGarminSensorDescription,
    ) -> None:
        super().__init__(coordinator, description)
        self._value_fn = description.value_fn
        self._attr_native_value = self._value_fn(coordinator.data)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._attr_native_value = self._value_fn(self.coordinator.data)
        super()._handle_coordinator_update()
