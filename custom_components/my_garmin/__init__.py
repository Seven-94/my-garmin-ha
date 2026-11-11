"""My Garmin: Home Assistant integration for the Garmin Connect Personal API."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import GarminPersonalClient
from .const import DOMAIN
from .coordinator import DailySummaryCoordinator, MyGarminData

__all__ = ["DOMAIN", "MyGarminConfigEntry", "async_setup_entry", "async_unload_entry"]

PLATFORMS = [Platform.SENSOR]

type MyGarminConfigEntry = ConfigEntry[MyGarminData]


async def async_setup_entry(hass: HomeAssistant, entry: MyGarminConfigEntry) -> bool:
    """Fetch the daily summary once, then set up the sensors.

    The first refresh also checks the key: a rejected key starts
    re-authentication, a network error or a 429 retries the set-up later.
    """
    client = GarminPersonalClient(
        async_get_clientsession(hass), entry.data[CONF_API_KEY]
    )
    daily = DailySummaryCoordinator(hass, entry, client)
    await daily.async_config_entry_first_refresh()

    entry.runtime_data = MyGarminData(client=client, daily=daily)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MyGarminConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
