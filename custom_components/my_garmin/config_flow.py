"""Config flow: one Garmin account, identified by its profile ID."""

import logging
from collections.abc import Mapping
from datetime import date
from typing import Any

import probatio
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_API_KEY
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    DateSelector,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util import dt as dt_util

from .api import (
    GarminApiError,
    GarminAuthError,
    GarminConnectionError,
    GarminPersonalClient,
    GarminRateLimitError,
)
from .const import CONF_KEY_EXPIRES, DOMAIN, TITLE

_LOGGER = logging.getLogger(__name__)

SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_API_KEY): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        probatio.Required(CONF_KEY_EXPIRES): DateSelector(),
    }
)


class MyGarminConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add, re-authenticate or reconfigure the Garmin account."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            profile_id, errors = await self._async_validate(user_input)
            if profile_id is not None:
                await self.async_set_unique_id(profile_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=TITLE, data=user_input)
        return self.async_show_form(step_id="user", data_schema=SCHEMA, errors=errors)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        del entry_data
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            profile_id, errors = await self._async_validate(user_input)
            if profile_id is not None:
                await self.async_set_unique_id(profile_id)
                # A key of another Garmin account would silently swap the data.
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(), data_updates=user_input
                )
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=SCHEMA, errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            profile_id, errors = await self._async_validate(user_input)
            if profile_id is not None:
                await self.async_set_unique_id(profile_id)
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry, data_updates=user_input
                )
        # The key is never shown back: only the expiry date is suggested.
        schema = self.add_suggested_values_to_schema(
            SCHEMA, {CONF_KEY_EXPIRES: entry.data.get(CONF_KEY_EXPIRES)}
        )
        return self.async_show_form(
            step_id="reconfigure", data_schema=schema, errors=errors
        )

    async def _async_validate(
        self, user_input: dict[str, Any]
    ) -> tuple[str | None, dict[str, str]]:
        """Return the account's profile ID, or the errors to show."""
        today = dt_util.now().date()
        if date.fromisoformat(user_input[CONF_KEY_EXPIRES]) < today:
            return None, {CONF_KEY_EXPIRES: "expiry_in_past"}

        client = GarminPersonalClient(
            async_get_clientsession(self.hass), user_input[CONF_API_KEY]
        )
        try:
            # The profile endpoint carries no account ID; the daily summary does.
            summary = await client.get_daily_summary(today)
        except GarminAuthError:
            return None, {"base": "invalid_auth"}
        except GarminConnectionError, GarminRateLimitError:
            return None, {"base": "cannot_connect"}
        except GarminApiError as err:
            _LOGGER.warning("Unexpected answer from Garmin: HTTP %s", err.status)
            return None, {"base": "unknown"}
        except Exception:
            _LOGGER.exception("Unexpected error while checking the Garmin key")
            return None, {"base": "unknown"}

        profile_id = summary.get("userProfileId")
        if not isinstance(profile_id, int) or isinstance(profile_id, bool):
            return None, {"base": "no_profile_id"}
        return str(profile_id), {}
