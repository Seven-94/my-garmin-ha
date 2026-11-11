"""Weight sensors: the latest weigh-in and the weight goal.

`weight.json` is an anonymised real response (measurements are synthetic);
`weight_goal_synthetic.json` is built from the API schema.
"""

import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.my_garmin.api import GarminApiError, GarminConnectionError
from custom_components.my_garmin.const import DOMAIN
from tests.integration.conftest import PROFILE_ID, load_fixture

# Garmin sends masses in grams, whatever the spec says.
MASSES = {
    "weight": "weight",
    "bone_mass": "boneMass",
    "muscle_mass": "muscleMass",
    "weight_change": "weightDelta",
}
READINGS = {
    "bmi": "bmi",
    "body_fat": "bodyFat",
    "body_water": "bodyWater",
    "visceral_fat": "visceralFat",
    "metabolic_age": "metabolicAge",
    "physique_rating": "physiqueRating",
}
# Null in the capture: synthetic values, shaped after the API spec.
SYNTHETIC_FIELDS = {"visceralFat": 7, "metabolicAge": 33.0, "physiqueRating": 5}
DISABLED_BY_DEFAULT = {
    "bone_mass",
    "visceral_fat",
    "metabolic_age",
    "physique_rating",
    "weight_goal_low",
    "weight_goal_high",
}
ENABLED_BY_DEFAULT = {
    "weight",
    "bmi",
    "body_fat",
    "body_water",
    "muscle_mass",
    "weight_change",
    "weight_goal",
}


def latest_weigh_in(data: dict[str, Any]) -> dict[str, Any]:
    days = data["dailyWeightSummaries"]
    return max((day["latestWeight"] for day in days), key=lambda w: w["calendarDate"])


@pytest.fixture
def weights(mock_client: MagicMock) -> dict[str, Any]:
    data = load_fixture("weight.json")
    for day in data["dailyWeightSummaries"]:
        day["latestWeight"].update(SYNTHETIC_FIELDS)
    mock_client.get_weight_range.return_value = data
    return data


@pytest.fixture
def goal(mock_client: MagicMock) -> dict[str, Any]:
    data = load_fixture("weight_goal_synthetic.json")
    mock_client.get_weight_goal.return_value = data
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


def _assert_weigh_in(
    hass: HomeAssistant, weigh_in: dict[str, Any], grams: float
) -> None:
    for key, field in MASSES.items():
        expected = weigh_in[field] * grams / 1000  # shown in kilograms
        assert float(_state(hass, key).state) == pytest.approx(expected), key
    for key, field in READINGS.items():
        assert float(_state(hass, key).state) == pytest.approx(weigh_in[field]), key


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
async def test_weight_sensors_read_the_latest_weigh_in(
    hass: HomeAssistant, weights: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    weigh_in = latest_weigh_in(weights)
    _assert_weigh_in(hass, weigh_in, grams=1)
    attributes = _state(hass, "weight").attributes
    measured_at = datetime.fromtimestamp(weigh_in["timestampGMT"] / 1000, tz=UTC)
    assert attributes["measured_at"] == measured_at.isoformat()
    assert attributes["source"] == weigh_in["sourceType"].lower()


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
async def test_masses_in_kilograms_as_the_spec_says(
    hass: HomeAssistant,
    mock_client: MagicMock,
    weights: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    weigh_in = latest_weigh_in(weights)
    in_kg = {**weigh_in, **{f: weigh_in[f] / 1000 for f in MASSES.values()}}
    mock_client.get_weight_range.return_value = {
        "dailyWeightSummaries": [{"latestWeight": in_kg}]
    }

    await _setup(hass, config_entry)

    _assert_weigh_in(hass, weigh_in, grams=1)


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
async def test_previous_weigh_in_when_none_in_the_range(
    hass: HomeAssistant,
    mock_client: MagicMock,
    weights: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    previous = {**weights["previousDateWeight"], **SYNTHETIC_FIELDS}
    mock_client.get_weight_range.return_value = {
        "dailyWeightSummaries": [],
        "previousDateWeight": previous,
    }

    await _setup(hass, config_entry)

    _assert_weigh_in(hass, previous, grams=1)


@pytest.mark.usefixtures("summary", "weights", "entity_registry_enabled_by_default")
async def test_zero_and_null_are_not_measured(
    hass: HomeAssistant, mock_client: MagicMock, config_entry: MockConfigEntry
) -> None:
    weigh_in = latest_weigh_in(mock_client.get_weight_range.return_value)
    weigh_in.update(bodyFat=0, boneMass=None, visceralFat=0, weightDelta=0)

    await _setup(hass, config_entry)

    assert _state(hass, "body_fat").state == STATE_UNKNOWN
    assert _state(hass, "bone_mass").state == STATE_UNKNOWN
    assert _state(hass, "visceral_fat").state == STATE_UNKNOWN
    # No change since the previous weigh-in is a genuine 0.
    assert float(_state(hass, "weight_change").state) == 0


@pytest.mark.usefixtures("summary", "entity_registry_enabled_by_default")
async def test_weight_goal(
    hass: HomeAssistant, goal: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    target = goal["userGoals"][-1]
    bounds = goal["userGoalRanges"][-1]
    state = _state(hass, "weight_goal")
    assert float(state.state) == pytest.approx(target["goalValue"] / 1000)
    assert state.attributes["category"] == target["userGoalCategory"].lower()
    assert state.attributes["tracking_period"] == target["trackingPeriodType"].lower()
    low = float(_state(hass, "weight_goal_low").state)
    assert low == pytest.approx(bounds["lowWeightGoalInGrams"] / 1000)
    high = float(_state(hass, "weight_goal_high").state)
    assert high == pytest.approx(bounds["highWeightGoalInGrams"] / 1000)


@pytest.mark.usefixtures(
    "summary", "weights", "goal", "entity_registry_enabled_by_default"
)
async def test_weight_units_and_classes(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    for key in (*MASSES, "weight_goal", "weight_goal_low", "weight_goal_high"):
        attributes = _state(hass, key).attributes
        assert attributes["device_class"] == "weight", key
        assert attributes["unit_of_measurement"] == "kg", key
    for key in MASSES:
        assert _state(hass, key).attributes["state_class"] == "measurement", key
    # A goal is not a measurement: no long-term statistics.
    assert "state_class" not in _state(hass, "weight_goal").attributes
    for key in ("body_fat", "body_water"):
        assert _state(hass, key).attributes["unit_of_measurement"] == "%", key
    assert _state(hass, "metabolic_age").attributes["unit_of_measurement"] == "y"


@pytest.mark.usefixtures("summary", "weights", "goal")
async def test_niche_weight_sensors_are_disabled_by_default(
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
async def test_no_weigh_in_is_unknown_without_logs(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The mocked client answers {} for both endpoints: Garmin's null body.
    caplog.set_level(logging.WARNING)

    await _setup(hass, config_entry)
    await config_entry.runtime_data.weight.async_refresh()
    await hass.async_block_till_done()

    for key in DISABLED_BY_DEFAULT | ENABLED_BY_DEFAULT:
        assert _state(hass, key).state == STATE_UNKNOWN, key
    assert not [r for r in caplog.records if r.name.startswith("custom_components")]


@pytest.mark.usefixtures("summary")
async def test_goal_error_keeps_the_weigh_in(
    hass: HomeAssistant,
    mock_client: MagicMock,
    weights: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    mock_client.get_weight_goal.side_effect = GarminApiError(404, "no goal")

    await _setup(hass, config_entry)

    expected = latest_weigh_in(weights)["weight"] / 1000
    assert float(_state(hass, "weight").state) == pytest.approx(expected)
    assert _state(hass, "weight_goal").state == STATE_UNKNOWN


async def test_a_new_watch_sync_refreshes_weight(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    await _setup(hass, config_entry)
    calls = mock_client.get_weight_range.await_count

    summary["lastSyncTimestampGMT"] = "2026-11-10T07:40:00.0"
    await config_entry.runtime_data.daily.async_refresh()
    await hass.async_block_till_done()

    assert mock_client.get_weight_range.await_count == calls + 1


async def test_weight_failure_does_not_block_other_sensors(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    mock_client.get_weight_range.side_effect = GarminConnectionError("down")

    await _setup(hass, config_entry)

    assert _state(hass, "steps").state == str(summary["totalSteps"])
    assert _state(hass, "weight").state == STATE_UNAVAILABLE
