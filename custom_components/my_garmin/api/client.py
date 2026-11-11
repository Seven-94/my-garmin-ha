"""Asynchronous client for the Garmin Connect Personal API (read-only)."""

import logging
import time
from collections.abc import Callable, Mapping
from datetime import date
from http import HTTPStatus
from typing import Any

import aiohttp

from ._http import DEFAULT_BASE_URL, DEFAULT_TIMEOUT, raise_for_error
from .auth import TokenManager
from .exceptions import GarminApiError, GarminAuthError, GarminConnectionError

_LOGGER = logging.getLogger(__name__)

type JsonObject = dict[str, Any]


class _TokenRejectedError(Exception):
    """Internal signal: a data request answered 401."""


class GarminPersonalClient:
    """Reads one Garmin account with its personal API key.

    The aiohttp session belongs to the caller (Home Assistant's shared session):
    the client never closes it.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._session = session
        self._base_url = base_url
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._auth = TokenManager(
            session, api_key, base_url=base_url, timeout=self._timeout, clock=clock
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(base_url={self._base_url!r})"

    async def get_user_profile(self) -> JsonObject:
        """User settings: units, time zone, locale and the profile ID."""
        return await self._get_object("/userprofile-service/userprofile/settings")

    async def get_daily_summary(self, day: date) -> JsonObject:
        """Daily totals and the timestamp of the last watch sync."""
        return await self._get_object(
            "/usersummary-service/usersummary/daily",
            {"calendarDate": day.isoformat()},
        )

    async def get_daily_sleep(self, day: date) -> JsonObject:
        """The night that ended on this day: Garmin dates a night by its wake-up."""
        return await self._get_object(
            "/sleep-service/sleep/dailySleep", {"date": day.isoformat()}
        )

    async def get_hrv(self, day: date) -> JsonObject:
        """Heart rate variability of the night that ended on this day.

        Empty when the device records no HRV: Garmin then answers a null body.
        """
        return await self._get_object(
            f"/hrv-service/hrv/{day.isoformat()}", null_is_empty=True
        )

    async def get_weight_range(self, start: date, end: date) -> JsonObject:
        """Weigh-ins between two days, and the last one before them."""
        # weight/all answers HTTP 400: weight/range is the working endpoint.
        return await self._get_object(
            f"/weight-service/weight/range/{start.isoformat()}/{end.isoformat()}",
            {"includeAll": "true"},
            null_is_empty=True,
        )

    async def get_weight_goal(self, start: date, end: date) -> JsonObject:
        """Weight goals in effect between two days, in grams."""
        return await self._get_object(
            "/goal-service/goal/user/effective/weightgoal/"
            f"{start.isoformat()}/{end.isoformat()}",
            null_is_empty=True,
        )

    async def get_activities(self, limit: int) -> list[JsonObject]:
        """The most recent activities, newest first."""
        path = "/activitylist-service/activities/search/activities"
        payload = await self.get(path, {"start": "0", "limit": str(limit)})
        if payload is None:
            return []
        if not isinstance(payload, list) or not all(
            isinstance(item, dict) for item in payload
        ):
            raise GarminApiError(
                HTTPStatus.OK, f"Expected a list of objects from {path}"
            )
        return payload

    async def get_heart_rate_zones(self) -> list[JsonObject]:
        """Heart rate zones of each sport, with Garmin's defaults when none are set."""
        path = "/biometric-service/heartRateZones/v2"
        payload = await self.get(path)
        if payload is None:
            return []
        if not isinstance(payload, list) or not all(
            isinstance(item, dict) for item in payload
        ):
            raise GarminApiError(
                HTTPStatus.OK, f"Expected a list of objects from {path}"
            )
        return payload

    async def _get_object(
        self,
        path: str,
        params: Mapping[str, str] | None = None,
        *,
        null_is_empty: bool = False,
    ) -> JsonObject:
        payload = await self.get(path, params)
        if payload is None and null_is_empty:
            return {}
        if not isinstance(payload, dict):
            raise GarminApiError(HTTPStatus.OK, f"Expected a JSON object from {path}")
        return payload

    async def get(self, path: str, params: Mapping[str, str] | None = None) -> object:
        """GET any endpoint and return the decoded JSON, whatever its shape.

        For endpoints without a dedicated method, such as the fixture capture.
        """
        token = await self._auth.async_get_token()
        try:
            return await self._get_with_token(path, params, token)
        except _TokenRejectedError:
            # Garmin revokes tokens when the key is deleted or replaced: one new
            # exchange tells a revoked token apart from a dead key.
            _LOGGER.debug("Token rejected on %s, exchanging the API key again", path)
            self._auth.invalidate(token)

        token = await self._auth.async_get_token()
        try:
            return await self._get_with_token(path, params, token)
        except _TokenRejectedError:
            raise GarminAuthError(
                "Garmin rejected a freshly issued token: the API key is unusable"
            ) from None

    async def _get_with_token(
        self, path: str, params: Mapping[str, str] | None, token: str
    ) -> object:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        try:
            async with self._session.get(
                f"{self._base_url}{path}",
                params=params,
                headers=headers,
                timeout=self._timeout,
            ) as response:
                if response.status == HTTPStatus.UNAUTHORIZED:
                    raise _TokenRejectedError
                raise_for_error(response)
                try:
                    return await response.json(content_type=None)
                except ValueError:
                    raise GarminApiError(
                        response.status, f"Invalid JSON from {path}"
                    ) from None
        except (aiohttp.ClientError, TimeoutError) as err:
            raise GarminConnectionError(f"Cannot reach Garmin for {path}") from err
