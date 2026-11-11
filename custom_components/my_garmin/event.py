"""Event fired once for each activity recorded while Home Assistant runs."""

from homeassistant.components.event import EventEntity, EventEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MyGarminConfigEntry
from .api import JsonObject
from .coordinator import MyGarminCoordinator, activity_start
from .entity import MyGarminEntity
from .sensor import activity_summary

NEW_ACTIVITY = "new_activity"

EVENT_DESCRIPTION = EventEntityDescription(
    key=NEW_ACTIVITY,
    translation_key=NEW_ACTIVITY,
    event_types=[NEW_ACTIVITY],
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyGarminConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    del hass
    async_add_entities(
        [MyGarminActivityEvent(entry.runtime_data.activities, EVENT_DESCRIPTION)]
    )


def _activities(coordinator: MyGarminCoordinator) -> list[JsonObject] | None:
    """The activities with an ID, or None before the first successful refresh."""
    if coordinator.data is None:
        return None
    activities = coordinator.data.get("activities")
    if not isinstance(activities, list):
        return []
    return [
        a
        for a in activities
        if isinstance(a, dict) and isinstance(a.get("activityId"), int)
    ]


class MyGarminActivityEvent(MyGarminEntity, EventEntity):
    """Fires `new_activity` for each activity ID not seen before."""

    def __init__(
        self, coordinator: MyGarminCoordinator, description: EventEntityDescription
    ) -> None:
        super().__init__(coordinator, description)
        self._known: set[int] | None = None
        self._oldest_known = ""
        self._remember(_activities(coordinator))

    def _remember(self, activities: list[JsonObject] | None) -> None:
        # The first activities seen are the baseline: they were recorded before
        # Home Assistant started, or before the first successful refresh.
        if activities is None:
            return
        self._known = {a["activityId"] for a in activities}
        self._oldest_known = min(map(activity_start, activities), default="")

    @callback
    def _handle_coordinator_update(self) -> None:
        activities = _activities(self.coordinator)
        if self._known is not None and activities is not None:
            # Oldest first, so automations see them in order. An activity older
            # than the known ones is not new: it moved up after a deletion.
            for activity in reversed(activities):
                if (
                    activity["activityId"] not in self._known
                    and activity_start(activity) >= self._oldest_known
                ):
                    self._trigger_event(
                        NEW_ACTIVITY,
                        {
                            **activity_summary(activity),
                            "name": activity.get("activityName"),
                        },
                    )
                    self.async_write_ha_state()
        self._remember(activities)
        super()._handle_coordinator_update()
