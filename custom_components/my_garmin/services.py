"""Services: backfill long-term statistics from Garmin's daily history."""

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
from homeassistant.exceptions import ServiceValidationError

from .const import DOMAIN
from .coordinator import MyGarminData
from .statistics import DEFAULT_DAYS, MAX_DAYS, async_backfill

SERVICE_BACKFILL_STATISTICS: Final = "backfill_statistics"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
ATTR_DAYS: Final = "days"

BACKFILL_SCHEMA: Final = probatio.Schema(
    {
        probatio.Optional(ATTR_CONFIG_ENTRY_ID): str,
        probatio.Optional(ATTR_DAYS, default=DEFAULT_DAYS): probatio.All(
            probatio.Coerce(int), probatio.Range(min=1, max=MAX_DAYS)
        ),
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


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    hass.services.async_register(
        DOMAIN,
        SERVICE_BACKFILL_STATISTICS,
        _async_backfill_statistics,
        schema=BACKFILL_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
