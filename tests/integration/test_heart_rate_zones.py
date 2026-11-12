"""Heart rate zone sensors: the zones of the DEFAULT sport, refreshed every six hours.

`heart_rate_zones_synthetic.json` is built from the API schema, with synthetic
values.
"""

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.my_garmin.api import GarminConnectionError
from custom_components.my_garmin.const import DOMAIN
from tests.integration.conftest import PROFILE_ID, load_fixture

# Sensor key, and the Garmin field of the sport's zones it shows, in bpm.
ZONE_FIELDS = {
    "zones_max_heart_rate": "maxHeartRateUsed",
    "zones_lactate_threshold_heart_rate": "lactateThresholdHeartRateUsed",
    "zones_resting_heart_rate": "restingHeartRateUsed",
    "zones_zone_1_floor": "zone1Floor",
    "zones_zone_2_floor": "zone2Floor",
    "zones_zone_3_floor": "zone3Floor",
    "zones_zone_4_floor": "zone4Floor",
    "zones_zone_5_floor": "zone5Floor",
}
DISABLED_BY_DEFAULT = {"zones_lactate_threshold_heart_rate", "zones_training_method"}
TRAINING_METHODS = ["hr_max", "lactate_threshold", "hr_reserve", "custom"]


@pytest.fixture
def zones(mock_client: MagicMock) -> list[dict[str, Any]]:
    data = load_fixture("heart_rate_zones_synthetic.json")
    mock_client.get_heart_rate_zones.return_value = data
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


def _sport(zones: list[dict[str, Any]], sport: str) -> dict[str, Any]:
    return next(zone for zone in zones if zone["sport"] == sport)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_zone_sensors_read_the_default_sport(
    hass: HomeAssistant, zones: list[dict[str, Any]], config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    default = _sport(zones, "DEFAULT")
    for key, field in ZONE_FIELDS.items():
        assert float(_state(hass, key).state) == default[field], key
    training = _state(hass, "zones_training_method")
    assert training.state == default["trainingMethod"].lower()


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_the_default_sport_wins_over_the_first_entry(
    hass: HomeAssistant,
    mock_client: MagicMock,
    zones: list[dict[str, Any]],
    config_entry: MockConfigEntry,
) -> None:
    mock_client.get_heart_rate_zones.return_value = list(reversed(zones))

    await _setup(hass, config_entry)

    default = _sport(zones, "DEFAULT")
    assert (
        float(_state(hass, "zones_max_heart_rate").state) == default["maxHeartRateUsed"]
    )


async def test_the_first_sport_is_used_without_a_default(
    hass: HomeAssistant,
    mock_client: MagicMock,
    zones: list[dict[str, Any]],
    config_entry: MockConfigEntry,
) -> None:
    running = _sport(zones, "RUNNING")
    mock_client.get_heart_rate_zones.return_value = [running]

    await _setup(hass, config_entry)

    assert (
        float(_state(hass, "zones_max_heart_rate").state) == running["maxHeartRateUsed"]
    )


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_other_sports_are_attributes_of_the_zones(
    hass: HomeAssistant, zones: list[dict[str, Any]], config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    running = _sport(zones, "RUNNING")
    attributes = _state(hass, "zones_max_heart_rate").attributes
    assert attributes["sport"] == "default"
    assert attributes["other_sports"] == [
        {
            "sport": "running",
            "training_method": running["trainingMethod"].lower(),
            "max_heart_rate": running["maxHeartRateUsed"],
            "lactate_threshold_heart_rate": running["lactateThresholdHeartRateUsed"],
            "resting_heart_rate": running["restingHeartRateUsed"],
            "zone_1_floor": running["zone1Floor"],
            "zone_2_floor": running["zone2Floor"],
            "zone_3_floor": running["zone3Floor"],
            "zone_4_floor": running["zone4Floor"],
            "zone_5_floor": running["zone5Floor"],
        }
    ]


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize("auto_update", [True, False])
async def test_resting_zone_reports_whether_garmin_updates_it(
    hass: HomeAssistant,
    zones: list[dict[str, Any]],
    config_entry: MockConfigEntry,
    auto_update: bool,
) -> None:
    _sport(zones, "DEFAULT")["restingHrAutoUpdateUsed"] = auto_update

    await _setup(hass, config_entry)

    attributes = _state(hass, "zones_resting_heart_rate").attributes
    assert attributes["resting_heart_rate_auto_update"] is auto_update


@pytest.mark.usefixtures("zones", "entity_registry_enabled_by_default")
async def test_zone_sensors_are_diagnostic_settings_in_bpm(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    registry = er.async_get(hass)

    for key in ZONE_FIELDS:
        attributes = _state(hass, key).attributes
        assert attributes["unit_of_measurement"] == "bpm", key
        # A setting, not a measurement: no long-term statistics.
        assert "state_class" not in attributes, key
        entry = registry.async_get(_entity_id(hass, key))
        assert entry is not None, key
        assert entry.entity_category is er.EntityCategory.DIAGNOSTIC, key

    training = _state(hass, "zones_training_method").attributes
    assert training["device_class"] == "enum"
    assert training["options"] == TRAINING_METHODS


@pytest.mark.usefixtures("zones")
async def test_niche_zone_sensors_are_disabled_by_default(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    registry = er.async_get(hass)

    for key in {*ZONE_FIELDS, "zones_training_method"}:
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{PROFILE_ID}_{key}"
        )
        assert entity_id is not None, key
        entry = registry.async_get(entity_id)
        assert entry is not None, key
        disabled = entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert disabled is (key in DISABLED_BY_DEFAULT), key


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_no_zones_is_unknown_without_logs(
    hass: HomeAssistant,
    mock_client: MagicMock,
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The mocked client answers [] for the endpoint: what Garmin's null means.
    del mock_client
    caplog.set_level(logging.WARNING)

    await _setup(hass, config_entry)

    for key in {*ZONE_FIELDS, "zones_training_method"}:
        assert _state(hass, key).state == STATE_UNKNOWN, key
    assert not [r for r in caplog.records if r.name.startswith("custom_components")]


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(
    ("key", "field", "value"),
    [
        ("zones_lactate_threshold_heart_rate", "lactateThresholdHeartRateUsed", None),
        ("zones_training_method", "trainingMethod", None),
        ("zones_training_method", "trainingMethod", ""),
    ],
)
async def test_null_or_empty_setting_is_unknown_without_logs(
    hass: HomeAssistant,
    zones: list[dict[str, Any]],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
    key: str,
    field: str,
    value: str | None,
) -> None:
    _sport(zones, "DEFAULT")[field] = value
    caplog.set_level(logging.WARNING)

    await _setup(hass, config_entry)

    assert _state(hass, key).state == STATE_UNKNOWN
    assert not [r for r in caplog.records if r.name.startswith("custom_components")]


@pytest.mark.usefixtures("summary")
async def test_zones_failure_does_not_block_other_sensors(
    hass: HomeAssistant,
    mock_client: MagicMock,
    summary: dict[str, Any],
    config_entry: MockConfigEntry,
) -> None:
    mock_client.get_heart_rate_zones.side_effect = GarminConnectionError("down")

    await _setup(hass, config_entry)

    assert _state(hass, "steps").state == str(summary["totalSteps"])
    assert _state(hass, "zones_max_heart_rate").state == STATE_UNAVAILABLE
