"""Base entity: one Home Assistant device per Garmin account."""

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import MyGarminCoordinator


class MyGarminEntity(CoordinatorEntity[MyGarminCoordinator]):
    """Names come from translations; IDs from the Garmin profile ID."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: MyGarminCoordinator, description: EntityDescription
    ) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        # The profile ID, not the key: a new key keeps the same entities.
        account = entry.unique_id
        self.entity_description = description
        self._attr_unique_id = f"{account}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(account))},
            name=entry.title,
            manufacturer="Garmin",
            entry_type=DeviceEntryType.SERVICE,
        )
