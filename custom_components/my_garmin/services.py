"""Services: statistics backfill, activity files and workouts."""

from collections.abc import Awaitable
from http import HTTPStatus
from pathlib import Path
from typing import Final

import probatio
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util.json import JsonValueType

from .api import (
    GarminApiError,
    GarminAuthError,
    GarminConnectionError,
    GarminRateLimitError,
)
from .const import DOMAIN
from .coordinator import MyGarminData
from .statistics import DEFAULT_DAYS, MAX_DAYS, async_backfill

SERVICE_BACKFILL_STATISTICS: Final = "backfill_statistics"
SERVICE_DOWNLOAD_ACTIVITY_FILE: Final = "download_activity_file"
SERVICE_GET_ACTIVITY_WORKOUTS: Final = "get_activity_workouts"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
ATTR_DAYS: Final = "days"
ATTR_ACTIVITY_ID: Final = "activity_id"
# Files go to <local media folder>/my_garmin, named after the activity ID only.
MEDIA_FOLDER: Final = "my_garmin"
_ZIP_SIGNATURE: Final = b"PK\x03\x04"
_FIT_SIGNATURE: Final = b".FIT"  # bytes 8 to 11 of a FIT file header


def activity_id(value: object) -> int:
    """A positive integer, or a string of digits: nothing else becomes a path."""
    if isinstance(value, int) and not isinstance(value, bool):
        number = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        number = int(value)
    else:
        raise probatio.Invalid("activity_id must be a positive integer")
    if number <= 0:
        raise probatio.Invalid("activity_id must be a positive integer")
    return number


BACKFILL_SCHEMA: Final = probatio.Schema(
    {
        probatio.Optional(ATTR_CONFIG_ENTRY_ID): str,
        probatio.Optional(ATTR_DAYS, default=DEFAULT_DAYS): probatio.All(
            probatio.Coerce(int), probatio.Range(min=1, max=MAX_DAYS)
        ),
    }
)

ACTIVITY_SCHEMA: Final = probatio.Schema(
    {
        probatio.Optional(ATTR_CONFIG_ENTRY_ID): str,
        probatio.Required(ATTR_ACTIVITY_ID): activity_id,
    }
)


def _loaded_entry(call: ServiceCall) -> tuple[str, str, MyGarminData]:
    """The entry to backfill: the one named, or the only one there is."""
    entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
    entries = [
        entry
        for entry in call.hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
        and (entry_id is None or entry.entry_id == entry_id)
    ]
    if not entries or entries[0].unique_id is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="entry_not_loaded"
        )
    entry = entries[0]
    return str(entry.unique_id), entry.title, entry.runtime_data


async def _async_backfill_statistics(call: ServiceCall) -> ServiceResponse:
    hass = call.hass
    # A documented recorder API only: without the recorder there is nowhere
    # to write, and nothing is injected elsewhere.
    if "recorder" not in hass.config.components:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="recorder_unavailable"
        )
    profile_id, title, data = _loaded_entry(call)
    # One run at a time per account: two would double the requests to Garmin
    # and race on the cumulative sums.
    if data.backfill_lock.locked():
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="already_running"
        )
    async with data.backfill_lock:
        result = await async_backfill(
            hass,
            data.client,
            profile_id=profile_id,
            title=title,
            days=call.data[ATTR_DAYS],
        )
    return result.as_response()


def _error(key: str, err: Exception, *, user: bool = False) -> HomeAssistantError:
    kind = ServiceValidationError if user else HomeAssistantError
    error = kind(translation_domain=DOMAIN, translation_key=key)
    error.__cause__ = err
    return error


async def _from_garmin[T](request: Awaitable[T]) -> T:
    """Await a Garmin request; its errors become translated service errors."""
    try:
        return await request
    except GarminAuthError as err:
        raise _error("auth_failed", err) from err
    except GarminRateLimitError as err:
        raise _error("rate_limited_retry", err) from err
    except GarminConnectionError as err:
        raise _error("cannot_connect", err) from err
    except GarminApiError as err:
        if err.status == HTTPStatus.NOT_FOUND:
            raise _error("activity_not_found", err, user=True) from err
        raise _error("garmin_error", err) from err


def _extension(content: bytes) -> str:
    if content.startswith(_ZIP_SIGNATURE):
        return ".zip"
    if content[8:12] == _FIT_SIGNATURE:
        return ".fit"
    raise HomeAssistantError(
        translation_domain=DOMAIN, translation_key="unexpected_file"
    )


def _media_folder(hass: HomeAssistant) -> Path:
    local = hass.config.media_dirs.get("local")
    if local is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="media_unavailable"
        )
    folder = Path(local) / MEDIA_FOLDER
    if not hass.config.is_allowed_path(str(folder)):
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="media_unavailable"
        )
    return folder


def _write(folder: Path, name: str, content: bytes) -> Path:
    target = folder / name
    # Belt and braces: the name comes from an integer, but the file must land
    # in the folder whatever happens.
    if target.resolve().parent != folder.resolve():
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="unexpected_file"
        )
    folder.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{name}.part")
    partial.write_bytes(content)
    # Readers never see half a file.
    partial.replace(target)
    return target


async def _async_download_activity_file(call: ServiceCall) -> ServiceResponse:
    hass = call.hass
    folder = _media_folder(hass)
    _, _, data = _loaded_entry(call)
    number = call.data[ATTR_ACTIVITY_ID]
    content = await _from_garmin(data.client.get_activity_file(number))
    name = f"activity_{number}{_extension(content)}"
    target = await hass.async_add_executor_job(_write, folder, name, content)
    return {"path": str(target), "size": len(content)}


async def _async_get_activity_workouts(call: ServiceCall) -> ServiceResponse:
    _, _, data = _loaded_entry(call)
    number = call.data[ATTR_ACTIVITY_ID]
    workouts = await _from_garmin(data.client.get_activity_workouts(number))
    # A list is invariant: a new one, typed as JSON values, fits the response.
    response: list[JsonValueType] = [*workouts]
    return {"workouts": response}


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    hass.services.async_register(
        DOMAIN,
        SERVICE_BACKFILL_STATISTICS,
        _async_backfill_statistics,
        schema=BACKFILL_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DOWNLOAD_ACTIVITY_FILE,
        _async_download_activity_file,
        schema=ACTIVITY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_ACTIVITY_WORKOUTS,
        _async_get_activity_workouts,
        schema=ACTIVITY_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
