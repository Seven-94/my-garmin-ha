"""Activity file and workout services.

Files are synthetic bytes with the right signatures; the workout follows the
API schema with synthetic values.
"""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.my_garmin import services
from custom_components.my_garmin.api import (
    GarminApiError,
    GarminAuthError,
    GarminConnectionError,
    GarminRateLimitError,
)
from custom_components.my_garmin.const import DOMAIN

DOWNLOAD = "download_activity_file"
WORKOUTS = "get_activity_workouts"
ZIP = b"PK\x03\x04" + b"synthetic zip" * 4
# A FIT file: header size, protocol, profile, data size, then ".FIT".
FIT = b"\x0e\x10\x00\x00\x00\x00\x00\x00.FIT" + b"synthetic fit"


@pytest.fixture
def media(hass: HomeAssistant, tmp_path: Path) -> Path:
    folder = tmp_path / "media"
    folder.mkdir()
    hass.config.media_dirs = {"local": str(folder)}
    hass.config.allowlist_external_dirs = {str(folder)}
    return folder


@pytest.fixture
async def loaded(
    hass: HomeAssistant, mock_client: MagicMock, config_entry: MockConfigEntry
) -> MagicMock:
    mock_client.get_activity_file.return_value = ZIP
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_client


async def _call(hass: HomeAssistant, service: str, **data: Any) -> dict[str, Any]:
    response = await hass.services.async_call(
        DOMAIN, service, data, blocking=True, return_response=True
    )
    assert isinstance(response, dict)
    return response


@pytest.mark.usefixtures("loaded")
async def test_download_writes_the_zip_named_after_the_id(
    hass: HomeAssistant, media: Path
) -> None:
    response = await _call(hass, DOWNLOAD, activity_id=123)

    target = media / "my_garmin" / "activity_123.zip"
    assert target.read_bytes() == ZIP
    assert response == {"path": str(target), "size": len(ZIP)}


async def test_a_raw_fit_file_keeps_its_extension(
    hass: HomeAssistant, loaded: MagicMock, media: Path
) -> None:
    loaded.get_activity_file.return_value = FIT

    response = await _call(hass, DOWNLOAD, activity_id="123")

    assert response["path"] == str(media / "my_garmin" / "activity_123.fit")
    loaded.get_activity_file.assert_awaited_with(123)


async def test_an_unexpected_file_is_not_written(
    hass: HomeAssistant, loaded: MagicMock, media: Path
) -> None:
    loaded.get_activity_file.return_value = b"<html>not a file</html>"

    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, DOWNLOAD, activity_id=123)

    assert err.value.translation_key == "unexpected_file"
    assert not (media / "my_garmin").exists()


@pytest.mark.usefixtures("loaded", "media")
@pytest.mark.parametrize(
    "activity_id",
    ["../123", "123/../../etc", "/etc/passwd", "12.5", 0, -1, True, "", None],
)
async def test_only_a_positive_integer_is_an_activity_id(
    hass: HomeAssistant, media: Path, activity_id: object
) -> None:
    with pytest.raises(Exception, match="activity_id"):
        await _call(hass, DOWNLOAD, activity_id=activity_id)

    assert list(media.rglob("*")) == []


@pytest.mark.usefixtures("loaded")
async def test_without_a_local_media_folder_nothing_is_written(
    hass: HomeAssistant,
) -> None:
    hass.config.media_dirs = {}

    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, DOWNLOAD, activity_id=123)

    assert err.value.translation_key == "media_unavailable"


@pytest.mark.usefixtures("loaded")
async def test_a_media_folder_outside_the_allowlist_is_refused(
    hass: HomeAssistant, media: Path
) -> None:
    hass.config.allowlist_external_dirs = set()

    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, DOWNLOAD, activity_id=123)

    assert err.value.translation_key == "media_unavailable"
    assert list(media.rglob("*")) == []


@pytest.mark.usefixtures("media")
@pytest.mark.parametrize(
    ("error", "key", "kind"),
    [
        (
            GarminApiError(404, "Garmin answered HTTP 404"),
            "activity_not_found",
            ServiceValidationError,
        ),
        (
            GarminApiError(500, "Garmin answered HTTP 500"),
            "garmin_error",
            HomeAssistantError,
        ),
        (
            GarminRateLimitError(retry_after=None),
            "rate_limited_retry",
            HomeAssistantError,
        ),
        (GarminConnectionError("down"), "cannot_connect", HomeAssistantError),
        (GarminAuthError("rejected"), "auth_failed", HomeAssistantError),
    ],
)
@pytest.mark.parametrize("service", [DOWNLOAD, WORKOUTS])
async def test_garmin_errors_are_explained(
    hass: HomeAssistant,
    loaded: MagicMock,
    error: Exception,
    key: str,
    kind: type[Exception],
    service: str,
) -> None:
    loaded.get_activity_file.side_effect = error
    loaded.get_activity_workouts.side_effect = error

    with pytest.raises(kind) as err:
        await _call(hass, service, activity_id=123)

    assert isinstance(err.value, HomeAssistantError)
    assert err.value.translation_key == key


async def test_workouts_are_returned_as_response_data(
    hass: HomeAssistant, loaded: MagicMock
) -> None:
    workouts = [{"workoutId": 1, "workoutName": "Synthetic workout"}]
    loaded.get_activity_workouts.return_value = workouts

    response = await _call(hass, WORKOUTS, activity_id=123)

    assert response == {"workouts": workouts}
    loaded.get_activity_workouts.assert_awaited_with(123)


async def test_no_workout_is_an_empty_list(
    hass: HomeAssistant, loaded: MagicMock
) -> None:
    loaded.get_activity_workouts.return_value = []

    assert await _call(hass, WORKOUTS, activity_id=123) == {"workouts": []}


@pytest.mark.parametrize(
    "name", ["../escape.zip", "sub/../../escape.zip", "/etc/escape.zip"]
)
def test_the_writer_never_leaves_its_folder(tmp_path: Path, name: str) -> None:
    # The services build names from integers; the writer checks anyway.
    folder = tmp_path / "media" / "my_garmin"

    with pytest.raises(HomeAssistantError):
        services._write(folder, name, ZIP)

    assert list(tmp_path.rglob("*.zip")) == []
