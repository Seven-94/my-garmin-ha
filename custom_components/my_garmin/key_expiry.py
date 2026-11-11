"""Warn before the personal API key expires.

Garmin does not expose the expiry date: the user enters it in the config flow.
A repair issue appears a week before that date and turns into an error once it
has passed; entering a new key and date (reconfigure) removes it.
"""

from datetime import date, datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from .const import CONF_KEY_EXPIRES, DOMAIN

ISSUE_ID = "key_expiry"
WARNING_DAYS = 7


def key_expires(entry: ConfigEntry) -> date | None:
    """The expiry date the user entered, if it can be read."""
    value = entry.data.get(CONF_KEY_EXPIRES)
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


@callback
def async_check_key_expiry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Create, update or delete the repair issue for today's date."""
    expires = key_expires(entry)
    if expires is None:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_ID)
        return
    days_left = (expires - dt_util.now().date()).days
    if days_left > WARNING_DAYS:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_ID)
        return
    # The key still works on its expiry day: it has expired the day after.
    expired = days_left < 0
    placeholders = {"expires": expires.isoformat()}
    if not expired:
        placeholders["days"] = str(days_left)
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_ID,
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR if expired else ir.IssueSeverity.WARNING,
        translation_key="key_expired" if expired else "key_expiring",
        translation_placeholders=placeholders,
    )


@callback
def async_track_key_expiry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Check now, then every day just after midnight, until the entry unloads."""
    async_check_key_expiry(hass, entry)

    @callback
    def _daily(now: datetime) -> None:
        del now
        async_check_key_expiry(hass, entry)

    entry.async_on_unload(
        async_track_time_change(hass, _daily, hour=0, minute=0, second=5)
    )


@callback
def async_remove_key_expiry_issue(hass: HomeAssistant) -> None:
    ir.async_delete_issue(hass, DOMAIN, ISSUE_ID)
