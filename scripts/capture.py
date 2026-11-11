"""Capture raw Garmin responses once, as material for test fixtures.

Usage, from the repository root:

    uv run python -m scripts.capture [--date YYYY-MM-DD]

The API key is read with getpass: it is not echoed, not stored in the shell
history and never written anywhere. The token stays in memory. Responses go to
captures/ (ignored by Git) with an index.json of what was called; they hold
personal data, so run scripts.anonymise before anything reaches the repository.

About 17 requests, one after the other, with a pause in between.
"""

import argparse
import asyncio
import getpass
import json
import logging
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import aiohttp

from custom_components.my_garmin.api import (
    GarminApiError,
    GarminPersonalClient,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_PAUSE = 1.0
ACTIVITIES = "activities"


@dataclass(frozen=True)
class Endpoint:
    name: str
    path: str
    params: dict[str, str] | None = None


def endpoints(day: date) -> list[Endpoint]:
    """The endpoints the integration plans to read, for one day.

    Ranges stay short: a week, or 30 days for the sparse weight readings.
    """
    d = day.isoformat()
    week_start = (day - timedelta(days=6)).isoformat()
    month_start = (day - timedelta(days=29)).isoformat()
    return [
        Endpoint("profile", "/userprofile-service/userprofile/settings"),
        Endpoint(
            "daily_summary",
            "/usersummary-service/usersummary/daily",
            {"calendarDate": d},
        ),
        Endpoint(
            "daily_summaries_week",
            "/usersummary-service/v2/dailysummaries",
            {"startDate": week_start, "endDate": d},
        ),
        Endpoint(
            "steps_week", f"/usersummary-service/stats/steps/daily/{week_start}/{d}"
        ),
        Endpoint(
            "stress_week", f"/usersummary-service/stats/stress/daily/{week_start}/{d}"
        ),
        Endpoint("sleep", "/sleep-service/sleep/dailySleep", {"date": d}),
        Endpoint("hrv_week", f"/hrv-service/hrv/daily/{week_start}/{d}"),
        Endpoint("hrv_night", f"/hrv-service/hrv/{d}"),
        Endpoint(
            "body_battery",
            "/wellness-service/wellness/bodyBattery/reports/daily",
            {"startDate": d, "endDate": d},
        ),
        Endpoint(
            "body_battery_detailed",
            "/wellness-service/wellness/bodyBattery/reports/daily",
            {"startDate": d, "endDate": d, "chunks": "288"},
        ),
        Endpoint(
            "heart_rate", "/wellness-service/wellness/dailyHeartRate", {"date": d}
        ),
        Endpoint("spo2", f"/wellness-service/wellness/daily/spo2acclimation/{d}"),
        # weight/all answers HTTP 400: weight/range is the working endpoint.
        Endpoint(
            "weight",
            f"/weight-service/weight/range/{month_start}/{d}",
            {"includeAll": "true"},
        ),
        Endpoint(
            ACTIVITIES,
            "/activitylist-service/activities/search/activities",
            {"start": "0", "limit": "5"},
        ),
        Endpoint("fitness_age", f"/fitnessage-service/fitnessage/v2/{d}"),
        Endpoint("heart_rate_zones", "/biometric-service/heartRateZones/v2"),
    ]


def _first_activity_id(payload: object) -> int | None:
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        activity_id = payload[0].get("activityId")
        if isinstance(activity_id, int):
            return activity_id
    return None


async def capture(
    client: GarminPersonalClient,
    day: date,
    out_dir: Path,
    *,
    pause: float = DEFAULT_PAUSE,
) -> None:
    """Call each endpoint once and save its raw JSON in out_dir.

    An HTTP error on one endpoint is recorded and the capture goes on; any other
    error (rejected key, rate limit, network) stops it.
    """
    todo = endpoints(day)
    index: list[dict[str, object]] = []
    while todo:
        endpoint = todo.pop(0)
        try:
            payload = await client.get(endpoint.path, endpoint.params)
        except GarminApiError as err:
            _LOGGER.warning("%s: HTTP %s", endpoint.name, err.status)
            status = f"HTTP {err.status}"
        else:
            out_dir.mkdir(parents=True, exist_ok=True)
            text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
            (out_dir / f"{endpoint.name}.json").write_text(text, encoding="utf-8")
            _LOGGER.info("%s: ok (%.1f kB)", endpoint.name, len(text) / 1000)
            status = "ok"
            activity_id = (
                _first_activity_id(payload) if endpoint.name == ACTIVITIES else None
            )
            if activity_id is not None:
                todo.append(
                    Endpoint(
                        "activity_detail",
                        f"/activity-service/activity/{activity_id}",
                    )
                )
        index.append(
            {
                "name": endpoint.name,
                "path": endpoint.path,
                "params": endpoint.params,
                "status": status,
            }
        )
        if todo:
            # Garmin publishes no quota: stay far from any plausible limit.
            await asyncio.sleep(pause)

    index_text = json.dumps(index, indent=2) + "\n"
    (out_dir / "index.json").write_text(index_text, encoding="utf-8")


def _yesterday() -> date:
    # Yesterday is a complete day, with its night of sleep already synced.
    return datetime.now(UTC).astimezone().date() - timedelta(days=1)


async def _run(day: date, out_dir: Path, api_key: str) -> None:
    async with aiohttp.ClientSession() as session:
        client = GarminPersonalClient(session, api_key)
        await capture(client, day, out_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date", type=date.fromisoformat, default=None)
    parser.add_argument("--out", type=Path, default=Path("captures"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    api_key = getpass.getpass("Garmin personal API key (hidden): ").strip()
    if not api_key:
        _LOGGER.error("No key entered.")
        return 1
    day = args.date or _yesterday()
    _LOGGER.info("Capturing %s into %s", day.isoformat(), args.out)
    asyncio.run(_run(day, args.out, api_key))
    _LOGGER.info("Done. Next: uv run python -m scripts.anonymise")
    return 0


if __name__ == "__main__":
    sys.exit(main())
