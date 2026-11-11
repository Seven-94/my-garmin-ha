"""Errors raised by the Garmin Personal API client.

Messages never contain the API key, the token or a response body: Home Assistant
logs them and shows some of them in the UI.
"""


class GarminError(Exception):
    """Base class for every error raised by the client."""


class GarminAuthError(GarminError):
    """The API key is invalid, expired or revoked: the user must enter a new one."""


class GarminConnectionError(GarminError):
    """Garmin could not be reached, or did not answer in time."""


class GarminRateLimitError(GarminError):
    """Garmin answered HTTP 429."""

    def __init__(self, retry_after: float | None) -> None:
        super().__init__("Garmin rate limit reached")
        # Seconds to wait, when Garmin says so.
        self.retry_after = retry_after


class GarminApiError(GarminError):
    """Garmin answered with an unexpected status or payload."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
