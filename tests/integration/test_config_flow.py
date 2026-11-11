"""Config flow: adding the account, re-authenticating and reconfiguring it."""

from unittest.mock import MagicMock

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.my_garmin.api import (
    GarminApiError,
    GarminAuthError,
    GarminConnectionError,
    GarminRateLimitError,
)
from custom_components.my_garmin.const import CONF_KEY_EXPIRES, DOMAIN
from tests.integration.conftest import (
    API_KEY,
    KEY_EXPIRES,
    OTHER_API_KEY,
    PROFILE_ID,
)

USER_INPUT = {CONF_API_KEY: API_KEY, CONF_KEY_EXPIRES: KEY_EXPIRES}


async def _start_user_flow(hass: HomeAssistant) -> str:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result["flow_id"]


async def test_user_flow_creates_the_entry(
    hass: HomeAssistant, mock_client: MagicMock
) -> None:
    flow_id = await _start_user_flow(hass)

    result = await hass.config_entries.flow.async_configure(flow_id, USER_INPUT)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "My Garmin"
    assert result["data"] == USER_INPUT
    assert result["result"].unique_id == str(PROFILE_ID)
    assert mock_client.factory.call_args.args[1] == API_KEY


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (GarminAuthError("rejected"), "invalid_auth"),
        (GarminConnectionError("down"), "cannot_connect"),
        (GarminRateLimitError(None), "cannot_connect"),
        (GarminApiError(500, "boom"), "unknown"),
        (RuntimeError("unexpected"), "unknown"),
    ],
)
async def test_user_flow_shows_errors_then_recovers(
    hass: HomeAssistant,
    mock_client: MagicMock,
    error: Exception,
    expected: str,
) -> None:
    flow_id = await _start_user_flow(hass)
    mock_client.get_daily_summary.side_effect = error

    result = await hass.config_entries.flow.async_configure(flow_id, USER_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}

    mock_client.get_daily_summary.side_effect = None
    result = await hass.config_entries.flow.async_configure(flow_id, USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_refuses_a_key_already_expired(
    hass: HomeAssistant, mock_client: MagicMock
) -> None:
    flow_id = await _start_user_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {**USER_INPUT, CONF_KEY_EXPIRES: "2000-01-01"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_KEY_EXPIRES: "expiry_in_past"}
    mock_client.get_daily_summary.assert_not_awaited()


async def test_user_flow_needs_a_profile_id(
    hass: HomeAssistant, mock_client: MagicMock
) -> None:
    flow_id = await _start_user_flow(hass)
    mock_client.get_daily_summary.return_value = {"totalSteps": 0}

    result = await hass.config_entries.flow.async_configure(flow_id, USER_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_profile_id"}


async def test_only_one_account_can_be_added(
    hass: HomeAssistant, mock_client: MagicMock, config_entry: MockConfigEntry
) -> None:
    del mock_client
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_reauth_replaces_the_key(
    hass: HomeAssistant, mock_client: MagicMock, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    new_input = {CONF_API_KEY: OTHER_API_KEY, CONF_KEY_EXPIRES: "2099-12-31"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], new_input
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data == new_input
    assert mock_client.factory.call_args.args[1] == OTHER_API_KEY


async def test_reauth_shows_errors(
    hass: HomeAssistant, mock_client: MagicMock, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    mock_client.get_daily_summary.side_effect = GarminAuthError("rejected")

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_reauth_refuses_another_account(
    hass: HomeAssistant, mock_client: MagicMock, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    mock_client.get_daily_summary.return_value = {"userProfileId": 42_424_242}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: OTHER_API_KEY, CONF_KEY_EXPIRES: KEY_EXPIRES}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert config_entry.data[CONF_API_KEY] == API_KEY


async def test_reconfigure_updates_the_key_and_expiry(
    hass: HomeAssistant, mock_client: MagicMock, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"

    new_input = {CONF_API_KEY: OTHER_API_KEY, CONF_KEY_EXPIRES: "2099-06-30"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], new_input
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data == new_input
    del mock_client


async def test_reconfigure_refuses_another_account(
    hass: HomeAssistant, mock_client: MagicMock, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reconfigure_flow(hass)
    mock_client.get_daily_summary.return_value = {"userProfileId": 42_424_242}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"


async def test_reconfigure_shows_errors(
    hass: HomeAssistant, mock_client: MagicMock, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reconfigure_flow(hass)
    mock_client.get_daily_summary.side_effect = GarminConnectionError("down")

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": "cannot_connect"}
