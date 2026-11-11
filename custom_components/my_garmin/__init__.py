"""My Garmin: Home Assistant integration for the Garmin Connect Personal API."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import GarminPersonalClient
from .const import DOMAIN
from .coordinator import (
    ActivitiesCoordinator,
    DailySummaryCoordinator,
    HrvCoordinator,
    MyGarminData,
    SleepCoordinator,
    SyncWatcher,
    WeightCoordinator,
)
from .key_expiry import async_remove_key_expiry_issue, async_track_key_expiry

__all__ = [
    "DOMAIN",
    "MyGarminConfigEntry",
    "async_remove_entry",
    "async_setup_entry",
    "async_unload_entry",
]

PLATFORMS = [Platform.BINARY_SENSOR, Platform.EVENT, Platform.SENSOR]

type MyGarminConfigEntry = ConfigEntry[MyGarminData]


async def async_setup_entry(hass: HomeAssistant, entry: MyGarminConfigEntry) -> bool:
    """Fetch the daily summary once, then set up the sensors.

    The first refresh also checks the key: a rejected key starts
    re-authentication, a network error or a 429 retries the set-up later.
    """
    # Before any request: an expired key is reported even when Garmin
    # rejects it and the set-up fails.
    async_track_key_expiry(hass, entry)
    client = GarminPersonalClient(
        async_get_clientsession(hass), entry.data[CONF_API_KEY]
    )
    daily = DailySummaryCoordinator(hass, entry, client)
    await daily.async_config_entry_first_refresh()
    # Sleep, HRV, weight and activities must not hold the set-up back: their
    # sensors stay unavailable until a later refresh succeeds.
    sleep = SleepCoordinator(hass, entry, client)
    await sleep.async_refresh()
    hrv = HrvCoordinator(hass, entry, client)
    await hrv.async_refresh()
    weight = WeightCoordinator(hass, entry, client)
    await weight.async_refresh()
    activities = ActivitiesCoordinator(hass, entry, client)
    await activities.async_refresh()

    entry.runtime_data = MyGarminData(
        client=client,
        daily=daily,
        sleep=sleep,
        hrv=hrv,
        weight=weight,
        activities=activities,
    )
    watcher = SyncWatcher(hass, daily, [sleep, hrv, weight, activities])
    entry.async_on_unload(daily.async_add_listener(watcher))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MyGarminConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: MyGarminConfigEntry) -> None:
    del entry
    async_remove_key_expiry_issue(hass)
