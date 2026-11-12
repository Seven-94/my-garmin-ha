"""Backfill of long-term statistics from Garmin's daily history.

Home Assistant keeps states for days; beyond that, history lives in long-term
statistics, which start empty. This imports one row per day, at local
midnight, as external statistics (`my_garmin:<profile>_<key>`), through the
recorder's documented `async_add_external_statistics`.
"""

import asyncio
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfLength, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .api import GarminPersonalClient, GarminRateLimitError, JsonObject
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Both endpoints accept 31 days per request.
WINDOW_DAYS = 31
DEFAULT_DAYS = 365
MAX_DAYS = 3 * 365
# Garmin publishes no quota: a backfill spreads its requests out.
BACKFILL_PAUSE = 2.0

type Number = int | float
type Reader = Callable[[JsonObject], Number | None]


def _field(*path: str) -> Reader:
    """A number at a path of nested objects; None when absent or not a number."""

    def read(data: JsonObject) -> Number | None:
        value: object = data
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return value

    return read


def _intensity_minutes(data: JsonObject) -> Number | None:
    # As on the watch: a vigorous minute is worth two moderate ones.
    moderate = _field("intensityMinutes", "moderate")(data)
    vigorous = _field("intensityMinutes", "vigorous")(data)
    if moderate is None and vigorous is None:
        return None
    return (moderate or 0) + 2 * (vigorous or 0)


@dataclass(frozen=True, kw_only=True)
class DailyStatistic:
    """One value per day: a total (cumulative sum) or a reading (mean, min, max)."""

    key: str
    name: str
    source: str  # "summary" or "sleep"
    value: Reader
    unit: str | None = None
    unit_class: str | None = None
    total: bool = False
    # A reading's lowest and highest values of the day, when Garmin gives them;
    # a single reading is its own min and max.
    minimum: Reader | None = None
    maximum: Reader | None = None
    single: bool = False


# The day's min and max heart rates of these summaries are those of two-minute
# averages, unlike the daily summary sensors: left out rather than mixed.
DAILY_STATISTICS: tuple[DailyStatistic, ...] = (
    DailyStatistic(
        key="steps",
        name="steps",
        source="summary",
        value=_field("movement", "steps", "value"),
        unit="steps",
        total=True,
    ),
    DailyStatistic(
        key="distance",
        name="distance",
        source="summary",
        value=_field("movement", "steps", "distanceInMeters"),
        unit=UnitOfLength.METERS,
        unit_class="distance",
        total=True,
    ),
    DailyStatistic(
        key="floors",
        name="floors climbed",
        source="summary",
        value=_field("movement", "floorsAscended", "value"),
        unit="floors",
        total=True,
    ),
    DailyStatistic(
        key="active_calories",
        name="active calories",
        source="summary",
        value=_field("calories", "burnedActive"),
        unit=UnitOfEnergy.KILO_CALORIE,
        unit_class="energy",
        total=True,
    ),
    DailyStatistic(
        key="total_calories",
        name="total calories",
        source="summary",
        value=_field("calories", "burnedTotal"),
        unit=UnitOfEnergy.KILO_CALORIE,
        unit_class="energy",
        total=True,
    ),
    DailyStatistic(
        key="intensity_minutes",
        name="intensity minutes",
        source="summary",
        value=_intensity_minutes,
        unit=UnitOfTime.MINUTES,
        unit_class="duration",
        total=True,
    ),
    DailyStatistic(
        key="body_battery_charged",
        name="Body Battery charged",
        source="summary",
        value=_field("bodyBattery", "chargedValue"),
        total=True,
    ),
    DailyStatistic(
        key="body_battery_drained",
        name="Body Battery drained",
        source="summary",
        value=_field("bodyBattery", "drainedValue"),
        total=True,
    ),
    DailyStatistic(
        key="resting_heart_rate",
        name="resting heart rate",
        source="summary",
        value=_field("heartRate", "restingValue"),
        unit="bpm",
        single=True,
    ),
    DailyStatistic(
        key="stress",
        name="stress",
        source="summary",
        value=_field("stress", "avgLevel"),
        maximum=_field("stress", "maxLevel"),
    ),
    DailyStatistic(
        key="respiration",
        name="respiration",
        source="summary",
        value=_field("respiration", "avgValue"),
        minimum=_field("respiration", "minValue"),
        maximum=_field("respiration", "maxValue"),
        unit="br/min",
    ),
    DailyStatistic(
        key="spo2",
        name="SpO2",
        source="summary",
        value=_field("pulseOx", "avgValue"),
        minimum=_field("pulseOx", "minValue"),
        unit=PERCENTAGE,
    ),
    DailyStatistic(
        key="sleep_duration",
        name="sleep duration",
        source="sleep",
        value=_field("sleepTimeSeconds"),
        unit=UnitOfTime.SECONDS,
        unit_class="duration",
        single=True,
    ),
    DailyStatistic(
        key="sleep_score",
        name="sleep score",
        source="sleep",
        value=_field("sleepScores", "overall", "value"),
        single=True,
    ),
)


@dataclass(frozen=True, kw_only=True)
class BackfillResult:
    """What a run imported, for the service response."""

    start: date
    end: date
    days: int
    statistic_ids: list[str]

    def as_response(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "days": self.days,
            "statistic_ids": self.statistic_ids,
        }


def statistic_id(profile_id: str, statistic: DailyStatistic) -> str:
    return f"{DOMAIN}:{profile_id}_{statistic.key}"


def _windows(start: date, end: date) -> list[tuple[date, date]]:
    windows = []
    while start <= end:
        last = min(start + timedelta(days=WINDOW_DAYS - 1), end)
        windows.append((start, last))
        start = last + timedelta(days=1)
    return windows


def _by_day(entries: Iterable[JsonObject]) -> dict[date, JsonObject]:
    days: dict[date, JsonObject] = {}
    for entry in entries:
        day = entry.get("calendarDate")
        if isinstance(day, str):
            try:
                days[date.fromisoformat(day)] = entry
            except ValueError:
                continue
    return days


def _midnight(day: date) -> datetime:
    # The day's row starts at local midnight, the start of Garmin's day.
    return datetime.combine(day, time(), dt_util.get_default_time_zone())


def _metadata(
    profile_id: str, title: str, statistic: DailyStatistic
) -> StatisticMetaData:
    return {
        "has_sum": statistic.total,
        "mean_type": (
            StatisticMeanType.NONE if statistic.total else StatisticMeanType.ARITHMETIC
        ),
        "name": f"{title} {statistic.name}",
        "source": DOMAIN,
        "statistic_id": statistic_id(profile_id, statistic),
        "unit_class": statistic.unit_class,
        "unit_of_measurement": statistic.unit,
    }


def _reading_row(
    statistic: DailyStatistic, day: date, data: JsonObject
) -> StatisticData | None:
    mean = statistic.value(data)
    # A reading of 0 is "not measured": a heart rate or a score is never 0.
    if mean is None or mean <= 0:
        return None
    row: StatisticData = {"start": _midnight(day), "mean": mean}
    if statistic.single:
        row["min"] = row["max"] = mean
    if statistic.minimum and (low := statistic.minimum(data)) is not None:
        row["min"] = low
    if statistic.maximum and (high := statistic.maximum(data)) is not None:
        row["max"] = high
    return row


def _total_rows(
    statistic: DailyStatistic, days: dict[date, JsonObject], base: float
) -> list[StatisticData]:
    rows: list[StatisticData] = []
    running = base
    for day in sorted(days):
        value = statistic.value(days[day])
        # A total of 0 is real (no floor climbed); a negative one is not.
        if value is None or value < 0:
            continue
        running += value
        rows.append({"start": _midnight(day), "state": value, "sum": running})
    return rows


def _last_sum(hass: HomeAssistant, statistic: str, before: datetime) -> float:
    """The cumulative sum stored before a day, to continue it: 0 if none."""
    rows = statistics_during_period(
        hass,
        before - timedelta(days=MAX_DAYS + WINDOW_DAYS),
        before,
        {statistic},
        "hour",
        None,
        {"sum"},
    )
    stored = rows.get(statistic, [])
    return float(stored[-1].get("sum") or 0) if stored else 0.0


async def _pause() -> None:
    await asyncio.sleep(BACKFILL_PAUSE)


async def _fetch(
    client: GarminPersonalClient, start: date, end: date
) -> tuple[dict[str, dict[date, JsonObject]], GarminRateLimitError | None]:
    """Both sources, window by window, oldest first, with a pause in between.

    A 429 stops the fetching: what came before it is returned with the error.
    """
    sources: dict[str, dict[date, JsonObject]] = {"summary": {}, "sleep": {}}
    requests = (
        ("summary", client.get_daily_summaries),
        ("sleep", client.get_sleep_summaries),
    )
    first = True
    for window_start, window_end in _windows(start, end):
        for source, fetch in requests:
            if not first:
                await _pause()
            first = False
            try:
                entries = await fetch(window_start, window_end)
            except GarminRateLimitError as err:
                return sources, err
            sources[source].update(_by_day(entries))
    return sources, None


async def async_backfill(
    hass: HomeAssistant,
    client: GarminPersonalClient,
    *,
    profile_id: str,
    title: str,
    days: int,
) -> BackfillResult:
    """Import the last `days` complete days; raise after importing on a 429."""
    end = dt_util.now().date() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    sources, rate_limit = await _fetch(client, start, end)

    recorder = get_instance(hass)
    imported: list[str] = []
    for statistic in DAILY_STATISTICS:
        source = sources[statistic.source]
        metadata = _metadata(profile_id, title, statistic)
        if statistic.total:
            base = await recorder.async_add_executor_job(
                _last_sum, hass, metadata["statistic_id"], _midnight(start)
            )
            rows = _total_rows(statistic, source, base)
        else:
            rows = [
                row
                for day in sorted(source)
                if (row := _reading_row(statistic, day, source[day])) is not None
            ]
        if rows:
            async_add_external_statistics(hass, metadata, rows)
            imported.append(metadata["statistic_id"])

    fetched = set(sources["summary"]) | set(sources["sleep"])
    _LOGGER.debug("Backfilled %d days into %d statistics", len(fetched), len(imported))
    if rate_limit is not None:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="rate_limited",
            translation_placeholders={"days": str(len(fetched))},
        ) from rate_limit
    return BackfillResult(
        start=start, end=end, days=len(fetched), statistic_ids=sorted(imported)
    )
