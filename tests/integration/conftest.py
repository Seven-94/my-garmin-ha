"""Fixtures for the Home Assistant side: a mocked Garmin client and a config entry."""

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_API_KEY
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.my_garmin.const import CONF_KEY_EXPIRES, DOMAIN

# Obviously fake values: no test ever talks to Garmin.
API_KEY = "fake-api-key-for-tests"  # pragma: allowlist secret
OTHER_API_KEY = "another-fake-key"  # pragma: allowlist secret
PROFILE_ID = 10_000_001
KEY_EXPIRES = "2099-01-31"


@pytest.fixture
def mock_client() -> Iterator[MagicMock]:
    """Replace the Garmin client wherever the integration creates one."""
    client = MagicMock()
    client.get_daily_summary = AsyncMock(return_value={"userProfileId": PROFILE_ID})
    client.get_user_profile = AsyncMock(return_value={"displayName": "Anonymised"})
    with (
        patch(
            "custom_components.my_garmin.config_flow.GarminPersonalClient",
            return_value=client,
        ) as flow_factory,
        patch(
            "custom_components.my_garmin.GarminPersonalClient",
            new=flow_factory,
        ),
    ):
        client.factory = flow_factory
        yield client


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="My Garmin",
        unique_id=str(PROFILE_ID),
        data={CONF_API_KEY: API_KEY, CONF_KEY_EXPIRES: KEY_EXPIRES},
    )
