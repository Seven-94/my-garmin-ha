"""Polling: the daily summary often, the rest after each watch sync."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
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
# heavier data is fetched after a new watch sync.
DAILY_INTERVAL = timedelta(minutes=10)
# Safety net for data that may change without a watch sync, or a missed one.
SLEEP_INTERVAL = timedelta(hours=2)
# Garmin publishes no quota and may throttle without notice: after a 429, stay
# away for at least this long, or longer if Garmin says so.
RATE_LIMIT_BACKOFF = 30 * 60.0


class MyGarminCoordinator(DataUpdateCoordinator[JsonObject]):
    """Fetches one Garmin endpoint for today and maps its errors."""

    config_entry: ConfigEntry[MyGarminData]

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry[MyGarminData],
        client: GarminPersonalClient,
        *,
        name: str,
        update_interval: timedelta,
        fetch: Callable[[date], Awaitable[JsonObject]],
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {name}",
            update_interval=update_interval,
        )
        self.client = client
        self._fetch = fetch

    async def _async_update_data(self) -> JsonObject:
        # Home Assistant's local date: Garmin days follow the user's time zone.
        today = dt_util.now().date()
        try:
            return await self._fetch(today)
        except GarminAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except GarminRateLimitError as err:
            retry_after = max(err.retry_after or 0.0, RATE_LIMIT_BACKOFF)
            raise UpdateFailed(str(err), retry_after=retry_after) from err
        except (GarminConnectionError, GarminApiError) as err:
            raise UpdateFailed(str(err)) from err


class DailySummaryCoordinator(MyGarminCoordinator):
    """Today's daily summary, refreshed every few minutes."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry[MyGarminData],
        client: GarminPersonalClient,
    ) -> None:
        super().__init__(
            hass,
            entry,
            client,
            name="daily summary",
            update_interval=DAILY_INTERVAL,
            fetch=client.get_daily_summary,
        )


def has_night(data: JsonObject | None) -> bool:
    """Whether a sleep response holds a recorded night."""
    if not data:
        return False
    seconds = data.get("sleepTimeSeconds")
    return isinstance(seconds, int | float) and seconds > 0


class SleepCoordinator(MyGarminCoordinator):
    """Last night, refreshed after a sync and every two hours."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry[MyGarminData],
        client: GarminPersonalClient,
    ) -> None:
        super().__init__(
            hass,
            entry,
            client,
            name="sleep",
            update_interval=SLEEP_INTERVAL,
            fetch=self._async_fetch_night,
        )

    async def _async_fetch_night(self, day: date) -> JsonObject:
        night = await self.client.get_daily_sleep(day)
        # Before the morning sync, today's night is empty: keep showing the last
        # recorded one (its date is exposed) instead of going back to unknown.
        if not has_night(night) and has_night(self.data):
            return self.data
        return night


def _last_sync(coordinator: DailySummaryCoordinator) -> object:
    return (coordinator.data or {}).get("lastSyncTimestampGMT")


class SyncWatcher:
    """Refreshes the heavier coordinators when the watch has synced again."""

    def __init__(
        self,
        hass: HomeAssistant,
        daily: DailySummaryCoordinator,
        dependents: list[MyGarminCoordinator],
    ) -> None:
        self._hass = hass
        self._daily = daily
        self._dependents = dependents
        self._last_sync = _last_sync(daily)

    @callback
    def __call__(self) -> None:
        sync = _last_sync(self._daily)
        if sync is None or sync == self._last_sync:
            return
        self._last_sync = sync
        for coordinator in self._dependents:
            self._hass.async_create_task(coordinator.async_request_refresh())


@dataclass
class MyGarminData:
    """What a loaded config entry keeps in runtime_data."""

    client: GarminPersonalClient
    daily: DailySummaryCoordinator
    sleep: SleepCoordinator
