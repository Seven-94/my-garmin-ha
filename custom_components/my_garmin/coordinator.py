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
NIGHT_INTERVAL = timedelta(hours=2)
# Scales upload weigh-ins on their own, without a watch sync.
WEIGHT_INTERVAL = timedelta(hours=1)
# The range response also carries the last weigh-in before it: a week is enough.
WEIGHT_DAYS = 7
# Activities logged in the app, or imported, arrive without a watch sync.
ACTIVITY_INTERVAL = timedelta(hours=1)
# Enough to catch every activity recorded between two refreshes.
ACTIVITY_LIMIT = 10
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


def _positive(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and value > 0


def has_night(data: JsonObject | None) -> bool:
    """Whether a sleep response holds a recorded night."""
    return bool(data) and _positive((data or {}).get("sleepTimeSeconds"))


def has_hrv(data: JsonObject | None) -> bool:
    """Whether an HRV response holds a recorded night."""
    summary = (data or {}).get("hrvSummary")
    return isinstance(summary, dict) and _positive(summary.get("lastNightAvg"))


class LastNightCoordinator(MyGarminCoordinator):
    """Last night, refreshed after a sync and every two hours.

    Before the morning sync, today's night is empty: the last recorded one
    stays shown (its date is exposed) instead of going back to unknown.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry[MyGarminData],
        client: GarminPersonalClient,
        *,
        name: str,
        fetch: Callable[[date], Awaitable[JsonObject]],
        has_data: Callable[[JsonObject | None], bool],
    ) -> None:
        super().__init__(
            hass,
            entry,
            client,
            name=name,
            update_interval=NIGHT_INTERVAL,
            fetch=self._async_fetch_night,
        )
        self._fetch_night = fetch
        self._has_data = has_data

    async def _async_fetch_night(self, day: date) -> JsonObject:
        night = await self._fetch_night(day)
        if not self._has_data(night) and self._has_data(self.data):
            return self.data
        return night


class SleepCoordinator(LastNightCoordinator):
    """Last night's sleep."""

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
            fetch=client.get_daily_sleep,
            has_data=has_night,
        )


class HrvCoordinator(LastNightCoordinator):
    """Last night's heart rate variability; empty for devices without HRV."""

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
            name="HRV",
            fetch=client.get_hrv,
            has_data=has_hrv,
        )


def latest_weigh_in(data: JsonObject) -> JsonObject:
    """The most recent weigh-in of a weight range response, or {}."""
    days = data.get("dailyWeightSummaries")
    candidates = (
        [day.get("latestWeight") for day in days if isinstance(day, dict)]
        if isinstance(days, list)
        else []
    )
    candidates.append(data.get("previousDateWeight"))
    weigh_ins = [w for w in candidates if isinstance(w, dict)]
    # Days come newest first in practice; the date says it for sure.
    return max(weigh_ins, key=lambda w: str(w.get("calendarDate") or ""), default={})


class WeightCoordinator(MyGarminCoordinator):
    """The latest weigh-in and the weight goal, refreshed every hour."""

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
            name="weight",
            update_interval=WEIGHT_INTERVAL,
            fetch=self._async_fetch_weight,
        )

    async def _async_fetch_weight(self, day: date) -> JsonObject:
        start = day - timedelta(days=WEIGHT_DAYS - 1)
        weigh_in = latest_weigh_in(await self.client.get_weight_range(start, day))
        try:
            goal = await self.client.get_weight_goal(day, day)
        except GarminApiError as err:
            # A goal is optional: its failure must not hide the weigh-in.
            _LOGGER.debug("No weight goal: %s", err)
            goal = {}
        return {"weigh_in": weigh_in, "goal": goal}


def activity_start(activity: JsonObject) -> str:
    """Sort key: Garmin's GMT start time, "YYYY-MM-DD HH:MM:SS"."""
    return str(activity.get("startTimeGMT") or "")


class ActivitiesCoordinator(MyGarminCoordinator):
    """The latest activities, newest first, refreshed every hour."""

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
            name="activities",
            update_interval=ACTIVITY_INTERVAL,
            fetch=self._async_fetch_activities,
        )

    async def _async_fetch_activities(self, day: date) -> JsonObject:
        del day  # the latest activities, whatever their day
        activities = await self.client.get_activities(ACTIVITY_LIMIT)
        return {"activities": sorted(activities, key=activity_start, reverse=True)}


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
    hrv: HrvCoordinator
    weight: WeightCoordinator
    activities: ActivitiesCoordinator
