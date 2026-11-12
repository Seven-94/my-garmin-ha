"""Nutrition sensors: today's carbohydrates, protein and fat, and the goals.

`nutrition_synthetic.json` is built from the API schema, with synthetic values.
The calories consumed of the same response are not read: the daily summary
sensor already shows them. Meals, foods and their names never leave the
coordinator.
"""

import json
import logging
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.my_garmin.api import GarminConnectionError
from custom_components.my_garmin.const import DOMAIN
from custom_components.my_garmin.sensor import NUTRITION_SENSORS
from tests.integration.conftest import PROFILE_ID, load_fixture

# Sensor key, and the Garmin field it shows in the day's content, in g.
CONSUMED = {
    "nutrition_carbs": "carbs",
    "nutrition_protein": "protein",
    "nutrition_fat": "fat",
}
# Sensor key, and the Garmin field it shows in the day's goals.
GOALS = {
    "nutrition_calorie_goal": "calories",
    "nutrition_carbs_goal": "carbs",
    "nutrition_protein_goal": "protein",
    "nutrition_fat_goal": "fat",
    "nutrition_adjusted_calorie_goal": "adjustedCalories",
    "nutrition_adjusted_carbs_goal": "adjustedCarbs",
    "nutrition_adjusted_protein_goal": "adjustedProtein",
    "nutrition_adjusted_fat_goal": "adjustedFat",
}
CALORIE_GOALS = {"nutrition_calorie_goal", "nutrition_adjusted_calorie_goal"}
KEYS = {**CONSUMED, **GOALS}
# What a food log says about the person: never in a state, attribute or log.
PRIVATE_TEXT = ("Synthetic food name", "Synthetic brand", "Synthetic meal name")


@pytest.fixture
def nutrition(mock_client: MagicMock) -> dict[str, Any]:
    data = load_fixture("nutrition_synthetic.json")
    mock_client.get_nutrition.return_value = data
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


def _entity_id(hass: HomeAssistant, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{PROFILE_ID}_{key}"
    )
    assert entity_id is not None, key
    return entity_id


def _state(hass: HomeAssistant, key: str) -> State:
    state = hass.states.get(_entity_id(hass, key))
    assert state is not None, key
    return state


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
async def test_nutrition_sensors_read_today(
    hass: HomeAssistant, nutrition: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    content = nutrition["dailyNutritionContent"]
    for key, field in CONSUMED.items():
        assert float(_state(hass, key).state) == content[field], key
    goals = nutrition["dailyNutritionGoals"]
    for key, field in GOALS.items():
        assert float(_state(hass, key).state) == goals[field], key


@pytest.mark.usefixtures("summary", "nutrition")
async def test_today_is_requested(
    hass: HomeAssistant, mock_client: MagicMock, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    mock_client.get_nutrition.assert_awaited_with(dt_util.now().date())


@pytest.mark.usefixtures("summary", "nutrition", "entity_registry_enabled_by_default")
async def test_units_and_classes(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    for key in CONSUMED:
        attributes = _state(hass, key).attributes
        assert attributes["unit_of_measurement"] == "g", key
        assert attributes["device_class"] == "weight", key
        # Logged food can be deleted: not a total that only grows.
        assert attributes["state_class"] == "measurement", key
    for key in GOALS:
        attributes = _state(hass, key).attributes
        unit = "kcal" if key in CALORIE_GOALS else "g"
        assert attributes["unit_of_measurement"] == unit, key
        # A target, not a measurement: no long-term statistics.
        assert "state_class" not in attributes, key


@pytest.mark.usefixtures("summary", "nutrition")
async def test_nutrition_sensors_are_disabled_by_default(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    registry = er.async_get(hass)

    for key in KEYS:
        entry = registry.async_get(_entity_id(hass, key))
        assert entry is not None, key
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION, key


def test_summary_calories_consumed_are_not_duplicated() -> None:
    assert {d.key for d in NUTRITION_SENSORS} == set(KEYS)


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
async def test_meals_and_foods_never_leave_the_coordinator(
    hass: HomeAssistant,
    nutrition: dict[str, Any],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    await _setup(hass, config_entry)

    data = config_entry.runtime_data.nutrition.data
    assert set(data) == {"dailyNutritionContent", "dailyNutritionGoals"}
    assert data["dailyNutritionGoals"] == nutrition["dailyNutritionGoals"]
    states = json.dumps(
        [state.as_dict() for state in hass.states.async_all()], default=str
    )
    for text in PRIVATE_TEXT:
        assert text not in json.dumps(data), text
        assert text not in states, text
        assert text not in caplog.text, text


@pytest.mark.usefixtures("summary", "nutrition")
async def test_diagnostics_hold_no_nutrition_value(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    config_entry: MockConfigEntry,
) -> None:
    await _setup(hass, config_entry)

    result: dict[str, Any] = await get_diagnostics_for_config_entry(
        hass, hass_client, config_entry
    )

    assert result["coordinators"]["nutrition"]["data"] == {}
    dumped = json.dumps(result)
    for text in PRIVATE_TEXT:
        assert text not in dumped, text


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
@pytest.mark.parametrize(
    "answer",
    [
        {},
        {"dailyNutritionContent": None, "dailyNutritionGoals": None},
        {"dailyNutritionContent": {"carbs": None}, "dailyNutritionGoals": {}},
    ],
    ids=["null-body", "null-sections", "null-values"],
)
async def test_nothing_logged_is_unknown_without_logs(
    hass: HomeAssistant,
    mock_client: MagicMock,
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
    answer: dict[str, Any],
) -> None:
    mock_client.get_nutrition.return_value = answer
    caplog.set_level(logging.WARNING)

    await _setup(hass, config_entry)
    await config_entry.runtime_data.nutrition.async_refresh()
    await hass.async_block_till_done()

    for key in KEYS:
        assert _state(hass, key).state == STATE_UNKNOWN, key
    assert not [r for r in caplog.records if r.name.startswith("custom_components")]


async def test_nutrition_polls_hourly_and_after_a_watch_sync(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    nutrition: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    del nutrition
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data.nutrition
    assert coordinator.update_interval == timedelta(hours=1)
    calls = mock_client.get_nutrition.await_count

    summary["lastSyncTimestampGMT"] = "2026-11-10T07:40:00.0"
    await config_entry.runtime_data.daily.async_refresh()
    await hass.async_block_till_done()

    assert mock_client.get_nutrition.await_count == calls + 1


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_nutrition_failure_does_not_block_other_sensors(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    mock_client.get_nutrition.side_effect = GarminConnectionError("down")

    await _setup(hass, config_entry)

    assert _state(hass, "steps").state == str(summary["totalSteps"])
    for key in KEYS:
        assert _state(hass, key).state == STATE_UNAVAILABLE, key
