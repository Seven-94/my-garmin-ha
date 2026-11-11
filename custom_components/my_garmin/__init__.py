"""My Garmin: Home Assistant integration for the Garmin Connect Personal API."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    GarminAuthError,
    GarminConnectionError,
    GarminPersonalClient,
    GarminRateLimitError,
)
from .const import DOMAIN

__all__ = ["DOMAIN", "MyGarminConfigEntry", "async_setup_entry", "async_unload_entry"]

type MyGarminConfigEntry = ConfigEntry[GarminPersonalClient]


async def async_setup_entry(hass: HomeAssistant, entry: MyGarminConfigEntry) -> bool:
    """Check the key once, then keep the client for the platforms."""
    client = GarminPersonalClient(
        async_get_clientsession(hass), entry.data[CONF_API_KEY]
    )
    try:
        await client.get_user_profile()
    except GarminAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except (GarminConnectionError, GarminRateLimitError) as err:
        raise ConfigEntryNotReady(str(err)) from err

    entry.runtime_data = client
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MyGarminConfigEntry) -> bool:
    """Nothing to tear down yet: no platform is set up."""
    del hass, entry
    return True
