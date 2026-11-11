"""HTTP details shared by the token exchange and the data requests."""

from http import HTTPStatus

import aiohttp

from .exceptions import GarminApiError, GarminRateLimitError

DEFAULT_BASE_URL = "https://personal.connectapi.garmin.com"
# The official client uses the same timeout.
DEFAULT_TIMEOUT = 30.0


def raise_for_error(response: aiohttp.ClientResponse) -> None:
    """Map a 429 or any other error status to a typed exception."""
    if response.status == HTTPStatus.TOO_MANY_REQUESTS:
        raise GarminRateLimitError(
            parse_retry_after(response.headers.get("Retry-After"))
        )
    if response.status >= HTTPStatus.BAD_REQUEST:
        raise GarminApiError(response.status, f"Garmin answered HTTP {response.status}")


def parse_retry_after(value: str | None) -> float | None:
    """Read a Retry-After given in seconds; ignore the rare HTTP-date form."""
    # isascii() too: isdigit() accepts characters such as "²" that float() refuses.
    if value is None or not (value.isascii() and value.isdigit()):
        return None
    return float(value)
