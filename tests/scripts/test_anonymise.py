"""Anonymisation of captured Garmin responses before they become fixtures."""

import json
from pathlib import Path

import pytest

from scripts.anonymise import (
    Anonymiser,
    Json,
    anonymise_directory,
    find_leaks,
    main,
)

SHIFT = 28
DAY_MS = 86_400_000


def number(doc: Json, key: str, *indexes: int) -> float:
    """Read a number from an anonymised document, checking the shapes on the way."""
    assert isinstance(doc, dict)
    value = doc[key]
    for index in indexes:
        assert isinstance(value, list)
        value = value[index]
    assert isinstance(value, int | float)
    assert not isinstance(value, bool)
    return float(value)


@pytest.fixture
def anonymiser() -> Anonymiser:
    return Anonymiser(shift_days=SHIFT, seed="tests")


def test_ids_are_replaced_consistently_across_documents(
    anonymiser: Anonymiser,
) -> None:
    profile = anonymiser.anonymise({"profileId": 987654321, "id": 987654321})
    activity = anonymiser.anonymise(
        {"activityId": 22334455667, "ownerId": 987654321, "uuid": "a1b2-c3d4"}
    )

    assert isinstance(profile, dict)
    assert isinstance(activity, dict)
    assert profile["profileId"] != 987654321
    assert profile["profileId"] == profile["id"] == activity["ownerId"]
    assert activity["activityId"] not in (22334455667, profile["profileId"])
    assert isinstance(activity["activityId"], int)
    assert activity["uuid"] != "a1b2-c3d4"
    assert isinstance(activity["uuid"], str)


def test_type_ids_are_kept(anonymiser: Anonymiser) -> None:
    # Activity types are shared catalogue values, not personal identifiers.
    doc: Json = {
        "activityType": {"typeId": 1, "parentTypeId": 17, "typeKey": "running"}
    }

    assert anonymiser.anonymise(doc) == doc


def test_names_emails_and_urls_are_replaced(anonymiser: Anonymiser) -> None:
    result = anonymiser.anonymise(
        {
            "displayName": "jdoe42",
            "fullName": "Jane Doe",
            "activityName": "Morning run in Springfield",
            "emailAddress": "jane@example.org",
            "message": "contact: jane@example.org",
            "profileImageUrlLarge": "https://cdn.example.org/jdoe42.png",
        }
    )

    text = json.dumps(result)
    for original in ("jdoe42", "Jane Doe", "Springfield", "jane@example.org"):
        assert original not in text


def test_gps_coordinates_are_replaced(anonymiser: Anonymiser) -> None:
    result = anonymiser.anonymise(
        {"startLatitude": 43.6123, "startLongitude": 1.4321, "lat": 43.6, "lon": 1.4}
    )

    assert isinstance(result, dict)
    assert 43.6123 not in result.values()
    assert result["startLatitude"] == result["lat"]
    assert result["startLongitude"] == result["lon"]


def test_demographic_fields_are_replaced(anonymiser: Anonymiser) -> None:
    result = anonymiser.anonymise(
        {
            "userData": {"birthDate": "1981-03-04", "gender": "MALE", "height": 181.0},
            "chronologicalAge": 45,
        }
    )

    text = json.dumps(result)
    assert "1981-03-04" not in text
    assert "181.0" not in text
    assert isinstance(result, dict)
    assert result["chronologicalAge"] != 45


def test_dates_and_timestamps_are_shifted(anonymiser: Anonymiser) -> None:
    result = anonymiser.anonymise(
        {
            "calendarDate": "2026-11-10",
            "startTimeLocal": "2026-11-10T06:12:00.0",
            "startTimeGMT": "2026-11-10 05:12:00",
            "lastSyncTimestampGMT": 1_794_290_000_000,
            "startTimeInSeconds": 1_794_290_000,
            "bodyBatteryValuesArray": [[1_794_290_000_000, 42]],
        }
    )

    assert isinstance(result, dict)
    assert result["calendarDate"] == "2026-12-08"
    assert result["startTimeLocal"] == "2026-12-08T06:12:00.0"
    assert result["startTimeGMT"] == "2026-12-08 05:12:00"
    assert result["lastSyncTimestampGMT"] == 1_794_290_000_000 + SHIFT * DAY_MS
    assert result["startTimeInSeconds"] == 1_794_290_000 + SHIFT * 86_400
    shifted = 1_794_290_000_000 + SHIFT * DAY_MS
    assert number(result, "bodyBatteryValuesArray", 0, 0) == shifted
    # The level next to the timestamp is a measurement: synthetic.
    assert number(result, "bodyBatteryValuesArray", 0, 1) != 42


MEASUREMENTS: dict[str, int | float] = {
    "totalSteps": 8421,
    "restingHeartRate": 52,
    "sleepTimeSeconds": 27_000,
    "weight": 72_400.0,
    "bmi": 23.4,
    "bodyFat": 0.85,
    "lastSevenDaysAvgRestingHeartRate": 1_000_000_000,
}


def test_measurements_are_replaced_by_synthetic_values(
    anonymiser: Anonymiser,
) -> None:
    result = anonymiser.anonymise(dict(MEASUREMENTS))

    assert isinstance(result, dict)
    for key, original in MEASUREMENTS.items():
        synthetic = result[key]
        assert synthetic != original, key
        assert type(synthetic) is type(original), key


def test_synthetic_values_keep_the_scale_and_precision(
    anonymiser: Anonymiser,
) -> None:
    # Same number of integer digits and decimals: grams stay grams, a
    # percentage stays below 100, and parsers see the same formats.
    result = anonymiser.anonymise(dict(MEASUREMENTS))

    assert 1_000 <= number(result, "totalSteps") <= 9_999
    assert 10 <= number(result, "restingHeartRate") <= 99
    assert 10_000 <= number(result, "weight") < 100_000
    assert number(result, "weight").is_integer()
    assert 10 <= number(result, "bmi") < 100
    assert round(number(result, "bmi"), 1) == number(result, "bmi")
    assert 0 < number(result, "bodyFat") < 1


def test_not_measured_markers_are_kept(anonymiser: Anonymiser) -> None:
    # Garmin encodes "not measured" as null, 0 or a negative value.
    result = anonymiser.anonymise(
        {"weight": 0, "sleepTimeSeconds": None, "bodyBattery": -1, "spo2": 0.0}
    )

    assert isinstance(result, dict)
    assert result["weight"] == 0
    assert result["sleepTimeSeconds"] is None
    assert result["spo2"] == 0.0
    assert isinstance(result["bodyBattery"], int)
    assert -9 <= result["bodyBattery"] <= -1


def test_catalogue_numbers_and_settings_are_kept(anonymiser: Anonymiser) -> None:
    doc: Json = {
        "activityType": {"typeId": 1, "parentTypeId": 17, "typeKey": "running"},
        "firstDayOfWeek": {"dayId": 3, "dayName": "monday", "sortOrder": 3},
        "bodyBatteryValueDescriptorIndex": 1,
        "bodyBatteryVersion": 2.0,
        "fileFormat": {"formatId": 7, "formatKey": "fit"},
        "sleepScores": {"overall": {"qualifierKey": "GOOD"}},
        "timeZone": "Europe/Paris",
        "isValid": True,
        "nothing": None,
    }

    assert anonymiser.anonymise(doc) == doc


def test_same_seed_gives_the_same_fixtures() -> None:
    first = Anonymiser(shift_days=SHIFT, seed="a").anonymise(dict(MEASUREMENTS))
    second = Anonymiser(shift_days=SHIFT, seed="a").anonymise(dict(MEASUREMENTS))
    other = Anonymiser(shift_days=SHIFT, seed="b").anonymise(dict(MEASUREMENTS))

    assert first == second
    assert first != other


def test_find_leaks_reports_paths_not_values(anonymiser: Anonymiser) -> None:
    raw: Json = {"profileId": 987654321, "fullName": "Jane Doe", "day": "2026-11-10"}
    anonymiser.anonymise(raw)
    leaked: Json = {"copy": 987654321, "text": "by Jane Doe", "when": "2026-11-10"}

    leaks = find_leaks(anonymiser, {"doc.json": leaked})

    assert sorted(leaks) == ["doc.json: $.copy", "doc.json: $.text", "doc.json: $.when"]
    assert all("Jane" not in leak and "987654321" not in leak for leak in leaks)


def test_find_leaks_is_empty_for_clean_output(anonymiser: Anonymiser) -> None:
    raw: Json = {"profileId": 987654321, "fullName": "Jane Doe", "day": "2026-11-10"}
    clean = anonymiser.anonymise(raw)

    assert find_leaks(anonymiser, {"doc.json": clean}) == []


def test_anonymise_directory_writes_fixtures_and_key_list(tmp_path: Path) -> None:
    captures = tmp_path / "captures"
    captures.mkdir()
    (captures / "profile.json").write_text(
        json.dumps({"profileId": 987654321, "fullName": "Jane Doe"})
    )
    (captures / "activities.json").write_text(json.dumps([{"activityId": 22334455}]))
    (captures / "index.json").write_text(json.dumps([{"name": "profile"}]))
    fixtures = tmp_path / "fixtures"

    leaks = anonymise_directory(captures, fixtures, shift_days=SHIFT)

    assert leaks == []
    written = json.loads((fixtures / "profile.json").read_text())
    assert "Jane Doe" not in json.dumps(written)
    # The capture index holds real dates and IDs: it never becomes a fixture.
    assert not (fixtures / "index.json").exists()
    assert (captures / "keys.txt").read_text().split() == [
        "activityId",
        "fullName",
        "profileId",
    ]


def test_anonymise_directory_writes_nothing_when_a_leak_remains(
    tmp_path: Path,
) -> None:
    captures = tmp_path / "captures"
    captures.mkdir()
    # The name also sits in a key the rules do not know: it must be caught.
    (captures / "profile.json").write_text(
        json.dumps({"fullName": "Jane Doe", "comment": "Jane Doe"})
    )
    fixtures = tmp_path / "fixtures"

    leaks = anonymise_directory(captures, fixtures, shift_days=SHIFT)

    assert leaks == ["profile.json: $.comment"]
    assert not fixtures.exists()


@pytest.mark.parametrize("shift_days", [0, 10, -3])
def test_shift_must_be_a_non_zero_number_of_whole_weeks(shift_days: int) -> None:
    # Whole weeks keep weekdays, which daily logic may rely on.
    with pytest.raises(ValueError, match="weeks"):
        Anonymiser(shift_days=shift_days)


def test_shift_can_go_back_in_time() -> None:
    assert Anonymiser(shift_days=-371).anonymise("2026-11-10") == "2025-11-04"


def test_small_and_float_ids_are_kept(anonymiser: Anonymiser) -> None:
    # Small "ids" are list indexes, and a float is no identifier.
    doc: Json = {"id": 3, "deviceId": 1.5}

    assert anonymiser.anonymise(doc) == doc


def test_find_leaks_walks_lists(anonymiser: Anonymiser) -> None:
    anonymiser.anonymise({"profileId": 987654321})

    leaks = find_leaks(anonymiser, {"doc.json": {"a": [True, None, 987654321]}})

    assert leaks == ["doc.json: $.a[2]"]


def test_main_writes_fixtures(tmp_path: Path) -> None:
    captures = tmp_path / "captures"
    captures.mkdir()
    (captures / "profile.json").write_text(json.dumps({"fullName": "Jane Doe"}))
    fixtures = tmp_path / "fixtures"

    code = main(["--captures", str(captures), "--fixtures", str(fixtures)])

    assert code == 0
    assert (fixtures / "profile.json").exists()


def test_main_fails_without_writing_when_a_leak_remains(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    captures = tmp_path / "captures"
    captures.mkdir()
    doc = {"fullName": "Jane Doe", "comment": "Jane Doe"}
    (captures / "profile.json").write_text(json.dumps(doc))
    fixtures = tmp_path / "fixtures"
    args = ["--captures", str(captures), "--fixtures", str(fixtures)]

    code = main([*args, "--shift-days", "-371"])

    assert code == 1
    assert not fixtures.exists()
    assert "profile.json: $.comment" in caplog.text
    assert "Jane Doe" not in caplog.text


def test_small_replaced_numbers_do_not_flag_equal_measurements(
    anonymiser: Anonymiser,
) -> None:
    # An age of 41 says nothing once replaced; a heart rate of 41 is no leak.
    anonymiser.anonymise({"chronologicalAge": 41, "height": 181.0})

    leaks = find_leaks(anonymiser, {"doc.json": {"minHeartRate": 41, "x": 181.0}})

    assert leaks == []


def test_distinctive_replaced_numbers_are_still_tracked(
    anonymiser: Anonymiser,
) -> None:
    anonymiser.anonymise({"startLatitude": 43.6123, "ownerId": 987654321})

    leaks = find_leaks(anonymiser, {"doc.json": {"a": 43.6123, "b": 987654321}})

    assert leaks == ["doc.json: $.a", "doc.json: $.b"]
