"""Garmin Connect Personal API client.

Kept free of Home Assistant imports, so it can be tested on its own and later
moved to a separate package.
"""

from .client import GarminPersonalClient, JsonObject
from .exceptions import (
    GarminApiError,
    GarminAuthError,
    GarminConnectionError,
    GarminError,
    GarminRateLimitError,
)

__all__ = [
    "GarminApiError",
    "GarminAuthError",
    "GarminConnectionError",
    "GarminError",
    "GarminPersonalClient",
    "GarminRateLimitError",
    "JsonObject",
]
