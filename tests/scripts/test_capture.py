"""Capture of raw Garmin responses, run here against the fake Garmin server."""

import json
import logging
from datetime import date
from pathlib import Path

import pytest

from custom_components.my_garmin.api import GarminAuthError, GarminPersonalClient
from scripts import capture as capture_module
from scripts.capture import capture, endpoints, main
from tests.api.fake_garmin import FAKE_API_KEY, FakeGarmin, Reply

DAY = date(2026, 11, 10)
ACTIVITIES_PATH = "/activitylist-service/activities/search/activities"


def test_endpoints_use_the_day_and_short_ranges() -> None:
    by_name = {e.name: e for e in endpoints(DAY)}

    assert by_name["daily_summary"].params == {"calendarDate": "2026-11-10"}
    assert by_name["sleep"].params == {"date": "2026-11-10"}
    assert by_name["hrv_week"].path == "/hrv-service/hrv/daily/2026-11-04/2026-11-10"
    assert (
        by_name["weight"].path == "/weight-service/weight/range/2026-10-12/2026-11-10"
    )
    # weight/all answers HTTP 400: never call it.
    assert all("/weight/all/" not in e.path for e in by_name.values())
    assert len({e.name for e in endpoints(DAY)}) == len(endpoints(DAY))


async def test_capture_writes_one_file_per_endpoint_and_an_index(
    client: GarminPersonalClient, fake_garmin: FakeGarmin, tmp_path: Path
) -> None:
    fake_garmin.data_replies[ACTIVITIES_PATH] = [
        Reply(body=json.dumps([{"activityId": 123456789}]))
    ]

    await capture(client, DAY, tmp_path, pause=0)

    names = {e.name for e in endpoints(DAY)} | {"activity_detail"}
    assert {p.stem for p in tmp_path.glob("*.json")} == names | {"index"}
    detail = json.loads((tmp_path / "activity_detail.json").read_text())
    assert detail["path"] == "/activity-service/activity/123456789"
    index = json.loads((tmp_path / "index.json").read_text())
    assert {entry["name"] for entry in index} == names
    assert {entry["status"] for entry in index} == {"ok"}


@pytest.mark.parametrize("activities", [[], [{"activityId": "not-a-number"}], [1]])
async def test_capture_skips_the_detail_without_a_usable_activity(
    client: GarminPersonalClient,
    fake_garmin: FakeGarmin,
    tmp_path: Path,
    activities: list[object],
) -> None:
    fake_garmin.data_replies[ACTIVITIES_PATH] = [Reply(body=json.dumps(activities))]

    await capture(client, DAY, tmp_path, pause=0)

    assert not (tmp_path / "activity_detail.json").exists()


async def test_capture_records_api_errors_and_goes_on(
    client: GarminPersonalClient, fake_garmin: FakeGarmin, tmp_path: Path
) -> None:
    fake_garmin.data_replies["/hrv-service/hrv/2026-11-10"] = [Reply(status=404)]

    await capture(client, DAY, tmp_path, pause=0)

    assert not (tmp_path / "hrv_night.json").exists()
    index = json.loads((tmp_path / "index.json").read_text())
    statuses = {entry["name"]: entry["status"] for entry in index}
    assert statuses["hrv_night"] == "HTTP 404"
    assert statuses["profile"] == "ok"
    # No activity in the list: no detail to fetch.
    assert "activity_detail" not in statuses


async def test_capture_stops_on_a_rejected_key(
    client: GarminPersonalClient, fake_garmin: FakeGarmin, tmp_path: Path
) -> None:
    fake_garmin.token_replies.append(Reply(status=401))

    with pytest.raises(GarminAuthError):
        await capture(client, DAY, tmp_path, pause=0)
    assert list(tmp_path.iterdir()) == []


async def test_capture_never_writes_or_logs_the_key_or_token(
    client: GarminPersonalClient,
    fake_garmin: FakeGarmin,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    await capture(client, DAY, tmp_path, pause=0)

    written = "".join(p.read_text() for p in tmp_path.iterdir())
    for secret in (FAKE_API_KEY, *fake_garmin.issued):
        assert secret not in written
        assert secret not in caplog.text


def test_main_refuses_an_empty_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capture_module.getpass, "getpass", lambda _prompt: "  ")

    assert main([]) == 1


def test_main_runs_the_capture_with_the_typed_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, object] = {}

    async def fake_run(day: date, out_dir: Path, api_key: str) -> None:
        # Stands in for the real run, which would call Garmin.
        seen.update(day=day, out_dir=out_dir, api_key=api_key)

    monkeypatch.setattr(capture_module.getpass, "getpass", lambda _prompt: "k ")
    monkeypatch.setattr(capture_module, "_run", fake_run)

    code = main(["--date", "2026-11-10", "--out", str(tmp_path)])

    assert code == 0
    assert seen == {"day": DAY, "out_dir": tmp_path, "api_key": "k"}


def test_main_defaults_to_yesterday(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[date] = []

    async def fake_run(day: date, out_dir: Path, api_key: str) -> None:
        del out_dir, api_key
        seen.append(day)

    monkeypatch.setattr(capture_module.getpass, "getpass", lambda _prompt: "k")
    monkeypatch.setattr(capture_module, "_run", fake_run)

    main([])

    assert seen == [capture_module._yesterday()]
