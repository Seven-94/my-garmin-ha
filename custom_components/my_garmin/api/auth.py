"""Exchange of the personal API key for a short-lived JWT access token."""

import asyncio
import base64
import json
import logging
from collections.abc import Callable
from http import HTTPStatus

import aiohttp

from ._http import raise_for_error
from .exceptions import GarminApiError, GarminAuthError, GarminConnectionError

_LOGGER = logging.getLogger(__name__)

KEY_EXCHANGE_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
# Same rule as the official client: renew when less than one hour is left.
RENEW_MARGIN = 3_600.0


class TokenManager:
    """Holds the current token in memory and renews it when needed.

    The token is never written to disk: one exchange per Home Assistant start is
    cheap, and a token on disk would be one more secret to protect.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        *,
        base_url: str,
        timeout: aiohttp.ClientTimeout,
        clock: Callable[[], float],
    ) -> None:
        self._session = session
        self._api_key = api_key
        self._url = f"{base_url}{KEY_EXCHANGE_PATH}"
        self._timeout = timeout
        self._clock = clock
        self._token: str | None = None
        self._renew_at = 0.0
        # Concurrent callers wait for a single exchange instead of each
        # starting their own.
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(url={self._url!r})"

    async def async_get_token(self) -> str:
        """Return a valid token, exchanging the API key first if needed."""
        async with self._lock:
            if self._token is None or self._clock() >= self._renew_at:
                self._token = await self._async_exchange()
            return self._token

    def invalidate(self, token: str) -> None:
        """Forget a token Garmin rejected.

        Only if it is still the current one: another request may already have
        replaced it, and that fresh token must be kept.
        """
        if self._token == token:
            self._token = None

    async def _async_exchange(self) -> str:
        _LOGGER.debug("Exchanging the API key for an access token")
        headers = {"Authorization": f"ApiKey {self._api_key}", "Accept": "*/*"}
        try:
            async with self._session.post(
                self._url, headers=headers, timeout=self._timeout
            ) as response:
                if response.status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
                    raise GarminAuthError(
                        "Garmin rejected the API key: it is invalid, expired or revoked"
                    )
                raise_for_error(response)
                token = (await response.text()).strip()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise GarminConnectionError(
                "Cannot reach Garmin to exchange the API key"
            ) from err

        try:
            expires_at = _read_expiry(token)
        except ValueError:
            # No chaining: the cause could quote part of the token.
            raise GarminApiError(
                HTTPStatus.OK, "The token endpoint did not return a valid JWT"
            ) from None

        lifetime = expires_at - self._clock()
        # A token shorter than two margins (key about to expire) is renewed at
        # half its lifetime, instead of on every call.
        self._renew_at = expires_at - min(RENEW_MARGIN, max(lifetime, 0.0) / 2)
        _LOGGER.debug("Access token obtained, valid for %.0f s", lifetime)
        return token


def _read_expiry(token: str) -> float:
    """Return the `exp` claim of a JWT, as a Unix timestamp.

    The signature is not checked: the token comes straight from Garmin over
    HTTPS, and only its expiry matters to the client.
    """
    parts = token.split(".")
    if len(parts) != 3:
        msg = "not a JWT"
        raise ValueError(msg)
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload))
    exp = claims.get("exp") if isinstance(claims, dict) else None
    # bool is a subclass of int, and never a valid timestamp.
    if isinstance(exp, bool) or not isinstance(exp, int | float):
        msg = "missing or invalid exp claim"
        raise ValueError(msg)
    return float(exp)
