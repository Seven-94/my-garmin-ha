"""Polling of the daily summary, the integration's pivot endpoint."""

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    GarminApiError,
    GarminAuthError,
    GarminConnectionError,
    GarminPersonalClient,
    GarminRateLimitError,
    JsonObject,
)
from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

# The summary is light and carries the last sync time: polled often, while
# heavier data will only be fetched after a new watch sync.
DAILY_INTERVAL = timedelta(minutes=10)
# Garmin publishes no quota and may throttle without notice: after a 429, stay
# away for at least this long, or longer if Garmin says so.
RATE_LIMIT_BACKOFF = 30 * 60.0


class DailySummaryCoordinator(DataUpdateCoordinator[JsonObject]):
    """Today's daily summary, refreshed every few minutes."""

    config_entry: ConfigEntry[MyGarminData]

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry[MyGarminData],
        client: GarminPersonalClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} daily summary",
            update_interval=DAILY_INTERVAL,
        )
        self.client = client

    async def _async_update_data(self) -> JsonObject:
        # Home Assistant's local date: Garmin days follow the user's time zone.
        today = dt_util.now().date()
        try:
            return await self.client.get_daily_summary(today)
        except GarminAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except GarminRateLimitError as err:
            retry_after = max(err.retry_after or 0.0, RATE_LIMIT_BACKOFF)
            raise UpdateFailed(str(err), retry_after=retry_after) from err
        except (GarminConnectionError, GarminApiError) as err:
            raise UpdateFailed(str(err)) from err


@dataclass
class MyGarminData:
    """What a loaded config entry keeps in runtime_data."""

    client: GarminPersonalClient
    daily: DailySummaryCoordinator
