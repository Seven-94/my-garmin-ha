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
from .const import (
    CONF_DAILY_INTERVAL_MINUTES,
    DEFAULT_DAILY_INTERVAL_MINUTES,
    DOMAIN,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

# The summary is light and carries the last sync time: polled often (an
# option), while heavier data is fetched after a new watch sync.
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
            update_interval=timedelta(
                minutes=entry.options.get(
                    CONF_DAILY_INTERVAL_MINUTES, DEFAULT_DAILY_INTERVAL_MINUTES
                )
            ),
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


# Zones change when the user edits them or Garmin re-estimates the resting and
# threshold heart rates: rarely. Six hours shows a new value the same day
# without asking Garmin every few minutes. A watch sync does not change them,
# so it does not trigger a refresh.
ZONES_INTERVAL = timedelta(hours=6)


class HeartRateZonesCoordinator(MyGarminCoordinator):
    """The heart rate zones of each sport, refreshed every six hours."""

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
            name="heart rate zones",
            update_interval=ZONES_INTERVAL,
            fetch=self._async_fetch_zones,
        )

    async def _async_fetch_zones(self, day: date) -> JsonObject:
        del day  # settings: the same zones whatever the day
        return {"zones": await self.client.get_heart_rate_zones()}


def has_spo2(data: JsonObject | None) -> bool:
    """Whether a SpO2 response holds a reading of its day."""
    return any(
        _positive((data or {}).get(field)) for field in ("averageSpO2", "latestSpO2")
    )


class SpO2Coordinator(LastNightCoordinator):
    """The day's SpO2 and acclimation baseline; empty for devices without SpO2."""

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
            name="SpO2",
            fetch=client.get_spo2,
            has_data=has_spo2,
        )


# The abnormal heart rate thresholds are alert settings, changed by hand in the
# app: like the zones, a watch sync does not change them.
HEART_RATE_THRESHOLDS_INTERVAL = timedelta(hours=6)


class HeartRateThresholdsCoordinator(MyGarminCoordinator):
    """Today's heart rate response, for its alert thresholds; every six hours."""

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
            name="heart rate thresholds",
            update_interval=HEART_RATE_THRESHOLDS_INTERVAL,
            fetch=client.get_daily_heart_rate,
        )


# Drinks are logged in the app, without a watch sync: an hourly poll shows them
# the same hour, and a watch sync brings the sweat loss of a new activity.
HYDRATION_INTERVAL = timedelta(hours=1)


class HydrationCoordinator(MyGarminCoordinator):
    """Today's hydration, refreshed every hour and after each watch sync."""

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
            name="hydration",
            update_interval=HYDRATION_INTERVAL,
            fetch=self._async_fetch_hydration,
        )

    async def _async_fetch_hydration(self, day: date) -> JsonObject:
        days = await self.client.get_hydration(day, day)
        # A day starts empty: yesterday's totals must not stay shown.
        return next(
            (entry for entry in days if entry.get("calendarDate") == day.isoformat()),
            {},
        )


# Meals are logged in the app, without a watch sync; the adjusted goals follow
# the day's activity, which a watch sync brings.
NUTRITION_INTERVAL = timedelta(hours=1)
# The day's totals and goals: meals, foods, their names and pictures say too much
# about a person and are dropped as soon as they arrive.
NUTRITION_SECTIONS = ("dailyNutritionContent", "dailyNutritionGoals")


class NutritionCoordinator(MyGarminCoordinator):
    """Today's nutrient totals and goals, every hour and after each watch sync."""

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
            name="nutrition",
            update_interval=NUTRITION_INTERVAL,
            fetch=self._async_fetch_nutrition,
        )

    async def _async_fetch_nutrition(self, day: date) -> JsonObject:
        log = await self.client.get_nutrition(day)
        return {section: log.get(section) for section in NUTRITION_SECTIONS}


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


# Fitness age moves slowly, with weeks of activity and measurements: a poll every
# six hours is fresh enough, and a watch sync refreshes it at once.
FITNESS_AGE_INTERVAL = timedelta(hours=6)


class FitnessAgeCoordinator(MyGarminCoordinator):
    """The fitness age, refreshed every six hours and after each watch sync."""

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
            name="fitness age",
            update_interval=FITNESS_AGE_INTERVAL,
            fetch=client.get_fitness_age,
        )


@dataclass
class MyGarminData:
    """What a loaded config entry keeps in runtime_data."""

    client: GarminPersonalClient
    daily: DailySummaryCoordinator
    sleep: SleepCoordinator
    hrv: HrvCoordinator
    weight: WeightCoordinator
    activities: ActivitiesCoordinator
    fitness_age: FitnessAgeCoordinator
    heart_rate_zones: HeartRateZonesCoordinator
    spo2: SpO2Coordinator
    heart_rate_thresholds: HeartRateThresholdsCoordinator
    hydration: HydrationCoordinator
    nutrition: NutritionCoordinator
