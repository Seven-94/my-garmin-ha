"""Binary sensors from the daily summary."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from . import MyGarminConfigEntry
from .api import JsonObject
from .const import CONF_SYNC_OVERDUE_HOURS, DEFAULT_SYNC_OVERDUE_HOURS
from .coordinator import MyGarminCoordinator
from .entity import MyGarminEntity


def _flag(key: str) -> Callable[[JsonObject], bool | None]:
    def read(data: JsonObject) -> bool | None:
        value = data.get(key)
        return value if isinstance(value, bool) else None

    return read


def _trip(data: JsonObject) -> dict[str, Any]:
    name = data.get("jetLagTripName")
    return {"trip_name": name} if isinstance(name, str) else {}


@dataclass(frozen=True, kw_only=True)
class MyGarminBinarySensorDescription(BinarySensorEntityDescription):
    value_fn: Callable[[JsonObject], bool | None]
    attributes_fn: Callable[[JsonObject], dict[str, Any]] | None = None


BINARY_SENSORS: tuple[MyGarminBinarySensorDescription, ...] = (
    MyGarminBinarySensorDescription(
        key="jet_lag",
        translation_key="jet_lag",
        value_fn=_flag("isJetLagDay"),
        attributes_fn=_trip,
    ),
)


def _cycle_predicted(data: JsonObject) -> bool | None:
    cycle = data.get("menstrualCycle")
    return _flag("predictedCycle")(cycle) if isinstance(cycle, dict) else None


# On when Garmin predicts the current cycle rather than reading it from logs.
MENSTRUAL_CYCLE_PREDICTED = MyGarminBinarySensorDescription(
    key="menstrual_cycle_predicted",
    translation_key="menstrual_cycle_predicted",
    entity_registry_enabled_default=False,
    value_fn=_cycle_predicted,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyGarminConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    del hass
    daily = entry.runtime_data.daily
    async_add_entities(
        [
            *(MyGarminBinarySensor(daily, d) for d in BINARY_SENSORS),
            MyGarminSyncOverdueSensor(daily, SYNC_OVERDUE),
            MyGarminBinarySensor(
                entry.runtime_data.menstrual_cycle, MENSTRUAL_CYCLE_PREDICTED
            ),
        ]
    )


class MyGarminBinarySensor(MyGarminEntity, BinarySensorEntity):
    def __init__(
        self,
        coordinator: MyGarminCoordinator,
        description: MyGarminBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator, description)
        self._value_fn = description.value_fn
        self._attributes_fn = description.attributes_fn
        self._read()

    def _read(self) -> None:
        # No data yet when the first refresh of this coordinator failed.
        data = self.coordinator.data or {}
        self._attr_is_on = self._value_fn(data)
        if self._attributes_fn is not None:
            self._attr_extra_state_attributes = self._attributes_fn(data)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._read()
        super()._handle_coordinator_update()


SYNC_OVERDUE = BinarySensorEntityDescription(
    key="sync_overdue",
    translation_key="sync_overdue",
    device_class=BinarySensorDeviceClass.PROBLEM,
)


def _utc(value: object) -> datetime | None:
    """An ISO time; Garmin's lastSyncTimestampGMT has no offset and is UTC."""
    if not isinstance(value, str):
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        return None
    return parsed.replace(tzinfo=dt_util.UTC) if parsed.tzinfo is None else parsed


class MyGarminSyncOverdueSensor(MyGarminEntity, BinarySensorEntity, RestoreEntity):
    """On when the watch has not synced for longer than the threshold option.

    Garmin sends the last sync time only once the watch has synced today: the
    last one seen is kept, across restarts too.
    """

    def __init__(
        self,
        coordinator: MyGarminCoordinator,
        description: BinarySensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, description)
        hours = coordinator.config_entry.options.get(
            CONF_SYNC_OVERDUE_HOURS, DEFAULT_SYNC_OVERDUE_HOURS
        )
        self._threshold = timedelta(hours=hours)
        self._last_sync: datetime | None = None
        self._read()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._remember(_utc(last_state.attributes.get("last_sync")))
            self._read()
            self.async_write_ha_state()

    def _remember(self, sync: datetime | None) -> None:
        if sync is not None and (self._last_sync is None or sync > self._last_sync):
            self._last_sync = sync

    def _read(self) -> None:
        data = self.coordinator.data or {}
        self._remember(_utc(data.get("lastSyncTimestampGMT")))
        now = dt_util.utcnow()
        if self._last_sync is not None:
            self._attr_is_on = now - self._last_sync > self._threshold
        elif now - dt_util.start_of_local_day() > self._threshold:
            # No sync today: the last one was before midnight, too long ago.
            self._attr_is_on = True
        else:
            self._attr_is_on = None
        self._attr_extra_state_attributes = {
            "last_sync": self._last_sync.isoformat() if self._last_sync else None,
            "threshold_hours": int(self._threshold.total_seconds() // 3600),
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        self._read()
        super()._handle_coordinator_update()
