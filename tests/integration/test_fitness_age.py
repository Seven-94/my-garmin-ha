"""Fitness age sensors: the age, its achievable and previous values, and why not.

`fitness_age_synthetic.json` is built from the API schema, with synthetic values.
The list form of the response is tested against the client, not here.
"""

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, EntityCategory
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.my_garmin.api import GarminConnectionError
from custom_components.my_garmin.const import DOMAIN
from tests.integration.conftest import PROFILE_ID, load_fixture

AGE_KEYS = ("fitness_age", "achievable_fitness_age", "previous_fitness_age")
# Garmin's enum values, as the API spec lists them, and their states.
INVALID_REASONS = {
    "USER_EMPTY_AGE": "user_empty_age",
    "USER_UNDER_AGE": "user_under_age",
    "USER_OVER_AGE": "user_over_age",
    "USER_PREGNANT": "user_pregnant",
}
COMPONENTS = {
    "body_fat": "bodyFat",
    "vigorous_days_avg": "vigorousDaysAvg",
    "rhr": "rhr",
    "vigorous_minutes_avg": "vigorousMinutesAvg",
}
DISABLED_BY_DEFAULT = {"previous_fitness_age"}
ENABLED_BY_DEFAULT = {
    "fitness_age",
    "achievable_fitness_age",
    "fitness_age_invalid_reason",
}


@pytest.fixture
def fitness(mock_client: MagicMock) -> dict[str, Any]:
    data = load_fixture("fitness_age_synthetic.json")
    mock_client.get_fitness_age.return_value = data
    return data


@pytest.fixture
def summary(mock_client: MagicMock) -> dict[str, Any]:
    data = load_fixture("daily_summary.json")
    data["lastSyncTimestampGMT"] = "2026-11-10T06:15:00.0"
    mock_client.get_daily_summary.return_value = data
    return data


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _state(hass: HomeAssistant, key: str) -> State:
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{PROFILE_ID}_{key}"
    )
    assert entity_id is not None, key
    state = hass.states.get(entity_id)
    assert state is not None, key
    return state


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
async def test_fitness_age_sensors_read_the_fixture(
    hass: HomeAssistant, fitness: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    assert float(_state(hass, "fitness_age").state) == pytest.approx(
        fitness["fitnessAge"]
    )
    assert float(_state(hass, "achievable_fitness_age").state) == pytest.approx(
        fitness["achievableFitnessAge"]
    )
    assert float(_state(hass, "previous_fitness_age").state) == pytest.approx(
        fitness["previousFitnessAge"]
    )


@pytest.mark.usefixtures("summary", "fitness", "entity_registry_enabled_by_default")
async def test_fitness_age_units_and_classes(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    for key in AGE_KEYS:
        attributes = _state(hass, key).attributes
        assert attributes["unit_of_measurement"] == "y", key
        assert attributes["state_class"] == "measurement", key
        # Not a duration: the age is a number of years, not a span of time.
        assert "device_class" not in attributes, key


@pytest.mark.usefixtures("summary", "fitness", "entity_registry_enabled_by_default")
async def test_components_are_attributes_of_the_fitness_age(
    hass: HomeAssistant, fitness: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    components = _state(hass, "fitness_age").attributes["components"]
    for name, field in COMPONENTS.items():
        expected = fitness["components"][field]
        assert components[name]["value"] == expected["value"], name
        assert components[name]["stale"] == expected["stale"], name
    assert (
        components["body_fat"]["target_value"]
        == (fitness["components"]["bodyFat"]["targetValue"])
    )


@pytest.mark.usefixtures("summary", "fitness", "entity_registry_enabled_by_default")
async def test_last_updated_is_an_attribute(
    hass: HomeAssistant, fitness: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    attributes = _state(hass, "fitness_age").attributes
    assert attributes["last_updated"] == fitness["lastUpdated"]


@pytest.mark.usefixtures("summary", "fitness", "entity_registry_enabled_by_default")
async def test_chronological_age_is_not_exposed(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, config_entry.entry_id)
    assert not [e.entity_id for e in entries if "chronolog" in e.unique_id.lower()]
    for key in (*AGE_KEYS, "fitness_age_invalid_reason"):
        assert not [
            name for name in _state(hass, key).attributes if "chronolog" in name
        ]


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
@pytest.mark.parametrize(("garmin", "state"), list(INVALID_REASONS.items()))
async def test_invalid_reason_is_shown_as_a_state(
    hass: HomeAssistant,
    mock_client: MagicMock,
    config_entry: MockConfigEntry,
    garmin: str,
    state: str,
) -> None:
    mock_client.get_fitness_age.return_value = {"invalidReason": garmin}

    await _setup(hass, config_entry)

    assert _state(hass, "fitness_age_invalid_reason").state == state


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
async def test_invalid_reason_lists_every_garmin_value(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    attributes = _state(hass, "fitness_age_invalid_reason").attributes
    assert attributes["device_class"] == "enum"
    assert attributes["options"] == list(INVALID_REASONS.values())


@pytest.mark.usefixtures("summary", "fitness")
async def test_invalid_reason_is_a_diagnostic_sensor(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{PROFILE_ID}_fitness_age_invalid_reason"
    )
    assert entity_id is not None
    entry = er.async_get(hass).async_get(entity_id)
    assert entry is not None
    assert entry.entity_category is EntityCategory.DIAGNOSTIC


@pytest.mark.usefixtures("summary", "fitness")
async def test_niche_fitness_age_sensors_are_disabled_by_default(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    registry = er.async_get(hass)

    for key in DISABLED_BY_DEFAULT | ENABLED_BY_DEFAULT:
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{PROFILE_ID}_{key}"
        )
        assert entity_id is not None, key
        entry = registry.async_get(entity_id)
        assert entry is not None, key
        disabled = entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert disabled is (key in DISABLED_BY_DEFAULT), key


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
async def test_no_fitness_age_at_set_up_is_unknown_without_logs(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The mocked client answers {} by default: Garmin's null body.
    caplog.set_level(logging.WARNING)

    await _setup(hass, config_entry)

    for key in (*AGE_KEYS, "fitness_age_invalid_reason"):
        assert _state(hass, key).state == STATE_UNKNOWN, key
    assert not [r for r in caplog.records if r.name.startswith("custom_components")]


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
async def test_empty_answer_makes_the_fitness_age_unknown_without_logs(
    hass: HomeAssistant,
    mock_client: MagicMock,
    fitness: dict[str, Any],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    await _setup(hass, config_entry)
    assert float(_state(hass, "fitness_age").state) == pytest.approx(
        fitness["fitnessAge"]
    )

    # Unlike the sleep and HRV nights, an empty answer is not the last known one.
    mock_client.get_fitness_age.return_value = {}
    await config_entry.runtime_data.fitness_age.async_refresh()
    await hass.async_block_till_done()

    for key in AGE_KEYS:
        assert _state(hass, key).state == STATE_UNKNOWN, key
    assert not [r for r in caplog.records if r.name.startswith("custom_components")]


async def test_a_new_watch_sync_refreshes_fitness_age(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    await _setup(hass, config_entry)
    calls = mock_client.get_fitness_age.await_count

    await config_entry.runtime_data.daily.async_refresh()
    await hass.async_block_till_done()
    assert mock_client.get_fitness_age.await_count == calls

    summary["lastSyncTimestampGMT"] = "2026-11-10T07:40:00.0"
    await config_entry.runtime_data.daily.async_refresh()
    await hass.async_block_till_done()
    assert mock_client.get_fitness_age.await_count == calls + 1


async def test_fitness_age_failure_does_not_block_other_sensors(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    mock_client.get_fitness_age.side_effect = GarminConnectionError("down")

    await _setup(hass, config_entry)

    assert _state(hass, "steps").state == str(summary["totalSteps"])
    assert _state(hass, "fitness_age").state == STATE_UNAVAILABLE
