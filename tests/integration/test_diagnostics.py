"""Diagnostics: the dump shared in a bug report holds no personal value."""

import json
import re
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.helpers.redact import REDACTED
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.my_garmin.api import GarminAuthError
from custom_components.my_garmin.const import CONF_KEY_EXPIRES, DOMAIN
from tests.integration.conftest import API_KEY, KEY_EXPIRES, PROFILE_ID, load_fixture

FIXTURE_NAMES = (
    "daily_summary.json",
    "sleep.json",
    "weight.json",
    "activities_synthetic.json",
)

# Identifiers, names, URLs, GPS positions and free text in the fixtures, listed
# by hand. The first test checks that every one of them is in the fixtures.
PERSONAL_KEYS = {
    "uuid",
    "userProfileId",
    "userDailySummaryId",
    "wellnessDescription",
    "id",
    "userProfilePK",
    "samplePk",
    "activityId",
    "activityUUID",
    "activityName",
    "description",
    "locationName",
    "ownerId",
    "ownerDisplayName",
    "ownerFullName",
    "ownerProfileImageUrlSmall",
    "ownerProfileImageUrlMedium",
    "ownerProfileImageUrlLarge",
    "startLatitude",
    "startLongitude",
    "endLatitude",
    "endLongitude",
}

# Synthetic values for fields the fixtures do not have: they must follow the
# rule by their key names alone, and menstrual and nutrition data must be gone.
FUTURE_FIELDS = {
    "futureAccountId": 20_000_002,
    "futureDisplayName": "Synthetic Name",
    "futureProfileUrl": "https://example.invalid/profile",
    "futureLatitude": 12.3456,
    "refreshToken": "synthetic-token-value",
    "goalName": "Synthetic goal",
    "jetLagTripName": "Synthetic trip",
    "menstrualCycleStart": "synthetic-cycle",
    "nutritionLog": {"food": "synthetic-food"},
}
REDACTED_FUTURE_FIELDS = {
    "futureAccountId",
    "futureDisplayName",
    "futureProfileUrl",
    "futureLatitude",
    "refreshToken",
    "goalName",
    "jetLagTripName",
}
EXCLUDED_FUTURE_FIELDS = {"menstrualCycleStart", "nutritionLog"}


def _pairs(node: object) -> Iterator[tuple[str, object]]:
    """Every (key, value) pair of a JSON document, at any depth."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key, value
            yield from _pairs(value)
    elif isinstance(node, list):
        for item in node:
            yield from _pairs(item)


def _appears(value: object, text: str) -> bool:
    """Whether the JSON token of a value occurs in a dump, as a whole token."""
    token = re.escape(json.dumps(value, ensure_ascii=False))
    return re.search(rf"(?<![\w.\-]){token}(?![\w.])", text) is not None


async def _set_up(
    hass: HomeAssistant,
    mock_client: MagicMock,
    config_entry: MockConfigEntry,
    *,
    daily_extra: dict[str, Any] | None = None,
) -> None:
    mock_client.get_daily_summary.return_value = {
        **load_fixture("daily_summary.json"),
        **(daily_extra or {}),
    }
    mock_client.get_daily_sleep.return_value = load_fixture("sleep.json")
    mock_client.get_weight_range.return_value = load_fixture("weight.json")
    mock_client.get_activities.return_value = load_fixture("activities_synthetic.json")
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_diagnostics_hold_no_identifier_name_url_gps_or_free_text(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_client: MagicMock,
    config_entry: MockConfigEntry,
) -> None:
    await _set_up(hass, mock_client, config_entry)

    result: dict[str, Any] = await get_diagnostics_for_config_entry(
        hass, hass_client, config_entry
    )

    pairs = [pair for name in FIXTURE_NAMES for pair in _pairs(load_fixture(name))]
    assert {key for key, _ in pairs} >= PERSONAL_KEYS
    personal = [(key, value) for key, value in pairs if key in PERSONAL_KEYS]
    secrets = [(CONF_API_KEY, API_KEY), ("profile ID", PROFILE_ID)]
    dumped = json.dumps(result, ensure_ascii=False)
    leaked = [
        key
        for key, value in [*personal, *secrets]
        if value not in (None, "") and _appears(value, dumped)
    ]
    assert leaked == []


async def test_diagnostics_keep_the_setup_state_and_measurements(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_client: MagicMock,
    config_entry: MockConfigEntry,
) -> None:
    await _set_up(hass, mock_client, config_entry)

    result: dict[str, Any] = await get_diagnostics_for_config_entry(
        hass, hass_client, config_entry
    )

    assert result["entry"] == {
        "data": {CONF_API_KEY: REDACTED, CONF_KEY_EXPIRES: KEY_EXPIRES},
        "options": {},
        "unique_id": REDACTED,
    }
    coordinators = result["coordinators"]
    assert set(coordinators) == {"daily", "sleep", "hrv", "weight", "activities"}
    daily = coordinators["daily"]
    assert daily["name"] == f"{DOMAIN} daily summary"
    assert daily["last_update_success"] is True
    assert daily["update_interval"] == "0:10:00"
    assert daily["last_exception"] is None
    assert daily["data"]["userProfileId"] == REDACTED
    daily_fixture = load_fixture("daily_summary.json")
    assert daily["data"]["dailyStepGoal"] == daily_fixture["dailyStepGoal"]
    sleep_fixture = load_fixture("sleep.json")
    sleep = coordinators["sleep"]["data"]
    assert sleep["sleepTimeSeconds"] == sleep_fixture["sleepTimeSeconds"]
    activities_fixture = load_fixture("activities_synthetic.json")
    assert len(coordinators["activities"]["data"]["activities"]) == len(
        activities_fixture
    )


async def test_entry_that_failed_to_load_still_reports_its_state(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_client: MagicMock,
    config_entry: MockConfigEntry,
) -> None:
    mock_client.get_daily_summary.side_effect = GarminAuthError("rejected")
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result: dict[str, Any] = await get_diagnostics_for_config_entry(
        hass, hass_client, config_entry
    )

    assert result == {
        "entry": {
            "data": {CONF_API_KEY: REDACTED, CONF_KEY_EXPIRES: KEY_EXPIRES},
            "options": {},
            "unique_id": REDACTED,
        },
        "coordinators": {},
    }


async def test_fields_unknown_to_the_fixtures_follow_the_same_rule(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_client: MagicMock,
    config_entry: MockConfigEntry,
) -> None:
    await _set_up(hass, mock_client, config_entry, daily_extra=FUTURE_FIELDS)

    result: dict[str, Any] = await get_diagnostics_for_config_entry(
        hass, hass_client, config_entry
    )

    daily = result["coordinators"]["daily"]["data"]
    assert {key: daily[key] for key in REDACTED_FUTURE_FIELDS} == dict.fromkeys(
        REDACTED_FUTURE_FIELDS, REDACTED
    )
    assert EXCLUDED_FUTURE_FIELDS.isdisjoint(daily)
    dumped = json.dumps(result, ensure_ascii=False)
    leaked = [key for key, value in _pairs(FUTURE_FIELDS) if _appears(value, dumped)]
    assert leaked == []
