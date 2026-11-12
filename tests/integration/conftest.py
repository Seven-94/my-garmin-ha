"""Fixtures for the Home Assistant side: a mocked Garmin client and a config entry."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from homeassistant.const import CONF_API_KEY
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.my_garmin.const import CONF_KEY_EXPIRES, DOMAIN

# Obviously fake values: no test ever talks to Garmin.
API_KEY = "fake-api-key-for-tests"  # pragma: allowlist secret
OTHER_API_KEY = "another-fake-key"  # pragma: allowlist secret
PROFILE_ID = 10_000_001
KEY_EXPIRES = "2099-01-31"
FIXTURES = Path(__file__).parent.parent / "fixtures"


def load_fixture(name: str) -> Any:
    """An anonymised Garmin response, with synthetic measurements."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def mock_client() -> Iterator[MagicMock]:
    """Replace the Garmin client wherever the integration creates one."""
    client = MagicMock()
    client.get_daily_summary = AsyncMock(return_value={"userProfileId": PROFILE_ID})
    client.get_user_profile = AsyncMock(return_value={"displayName": "Anonymised"})
    # No night recorded unless a test says otherwise.
    client.get_daily_sleep = AsyncMock(return_value={})
    # Garmin's null body for a device without HRV, as the client returns it.
    client.get_hrv = AsyncMock(return_value={})
    client.get_weight_range = AsyncMock(return_value={})
    client.get_weight_goal = AsyncMock(return_value={})
    client.get_activities = AsyncMock(return_value=[])
    # Garmin's null body or empty list, as the client returns it.
    client.get_fitness_age = AsyncMock(return_value={})
    client.get_heart_rate_zones = AsyncMock(return_value=[])
    # Garmin's null body for a device without SpO2, as the client returns it.
    client.get_spo2 = AsyncMock(return_value={})
    # Garmin's null body, as the client returns it.
    client.get_daily_heart_rate = AsyncMock(return_value={})
    # No hydration logged: Garmin's null body or empty list.
    client.get_hydration = AsyncMock(return_value=[])
    # Nothing logged: Garmin's null body, as the client returns it.
    client.get_nutrition = AsyncMock(return_value={})
    # No cycle tracked: Garmin's null body, as the client returns it.
    client.get_menstrual_cycle = AsyncMock(return_value={})
    # No report yet: Garmin's null body or empty list.
    client.get_body_battery_reports = AsyncMock(return_value=[])
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
def entity_registry_enabled_by_default() -> Iterator[None]:
    """Create every entity enabled, even those disabled by default.

    Home Assistant core has this fixture, but its test plugin does not.
    """
    with patch(
        "homeassistant.helpers.entity.Entity.entity_registry_enabled_default",
        new_callable=PropertyMock,
        return_value=True,
    ):
        yield


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="My Garmin",
        unique_id=str(PROFILE_ID),
        data={CONF_API_KEY: API_KEY, CONF_KEY_EXPIRES: KEY_EXPIRES},
    )
