"""Binary sensors from the daily summary."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MyGarminConfigEntry
from .api import JsonObject
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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyGarminConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    del hass
    daily = entry.runtime_data.daily
    async_add_entities(
        MyGarminBinarySensor(daily, description) for description in BINARY_SENSORS
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
