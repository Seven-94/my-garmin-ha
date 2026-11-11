"""Turn raw Garmin captures into anonymised test fixtures.

Usage, from the repository root:

    uv run python -m scripts.anonymise

Reads captures/*.json (ignored by Git), writes tests/fixtures/*.json and lists
every JSON key seen in captures/keys.txt, so the rules can be reviewed. Nothing
is written when a leak check finds an original value in the output; leaks are
reported by JSON path only, never by value.

Rules, by key name: identifiers are renumbered consistently across files;
names, e-mail addresses, URLs, GPS coordinates and demographic fields are
replaced; every date and epoch timestamp is shifted by the same whole number of
weeks. Every measurement (steps, heart rate, weight, sleep...) is replaced by a
synthetic value with the same type, sign, number of digits and decimals: no
real health data reaches the repository, yet parsers see the same formats.
Null, zero and negative values are kept, as Garmin uses them for "not
measured"; so are catalogue numbers (type IDs, indexes, versions, formats).
Times that reveal habits (sleep, activities, latest readings, sync, weigh-in)
also move by a random 1 to 4 hours, one offset per JSON object, so that start
and end, GMT and local stay coherent.
"""

import argparse
import hashlib
import json
import logging
import re
import secrets
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Literal

_LOGGER = logging.getLogger(__name__)

type Json = bool | int | float | str | list[Json] | dict[str, Json] | None

# camelCase identifiers: profileId, activityId, userProfilePK, uuid...
_ID_KEY = re.compile(r"(?:^id$|Id$|ID$|PK$|Pk$|^uuid$|Uuid$|[Ss]erial)")
# Catalogue values (activityTypeId, timeZoneId) are not personal.
_KEPT_ID_KEY = re.compile(r"[Tt]ype|[Tt]imeZone")
_NAME_KEY = re.compile(r"(?:[Nn]ame$|^email|[Ee]mail[A-Z]|^description$|^notes?$)")
# Settings named like names: a weekday is not a person.
_KEPT_NAME_KEY = re.compile(r"^dayName$")
_URL_KEY = re.compile(r"[Uu]rl")
_LATITUDE_KEY = re.compile(r"(?:[Ll]atitude|^lat$|Lat$)")
_LONGITUDE_KEY = re.compile(r"(?:[Ll]ongitude|^lon$|^lng$|Lon$|Lng$)")
# Numbers that describe the data rather than the body: kept as they are.
_KEPT_NUMBER_KEY = re.compile(r"(?:[Tt]ype|[Ii]ndex|^sortOrder$|[Vv]ersion|[Ff]ormat)")
_EPOCH_SECONDS_KEY = re.compile(r"(?i)(?:time|date|stamp|epoch|seconds)")
# Times of day that tell when someone sleeps, trains, syncs or weighs in.
# Day boundaries (startTimestampGMT, wellnessStartTime...) are not listed.
_EVENT_KEY = re.compile(
    r"(?:[Ss]leep(?:Start|End)Timestamp|^beginTimestamp$"
    r"|^(?:start|end)Time(?:GMT|Local)$|^latest\w*Time"
    r"|^(?:uploadedDate|lastUpdateDate|lastUpdated|lastSyncTimestampGMT"
    r"|timestampGMT|date)$)"
)
_DATETIME = re.compile(r"^(\d{4}-\d{2}-\d{2})([T ])(\d{2}:\d{2}:\d{2})(.*)$")
_DATE_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2})(?=$|[T ])")
_DATE_ANYWHERE = re.compile(r"\d{4}-\d{2}-\d{2}")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

DEMOGRAPHICS: dict[str, Json] = {
    "birthDate": "1985-06-15",
    "gender": "FEMALE",
    "height": 170.0,
    # The fitness age response gives the real age: as revealing as a birth date.
    "chronologicalAge": 40,
}
FAKE_NAME = "Anonymised"
FAKE_EMAIL = "user@example.com"
FAKE_URL = "https://example.com/anonymised"
# Royal Observatory, Greenwich: an obviously public, made-up location.
FAKE_LATITUDE = 51.4779
FAKE_LONGITUDE = -0.0015
FIRST_FAKE_ID = 10_000_000

# Epoch ranges between 2001 and 2052: no step count or duration reaches them.
_EPOCH_MS = (1.0e12, 2.6e12)
_EPOCH_S = (1.0e9, 2.6e9)
# Shorter strings are checked for exact matches only, or every short word
# would count as a leak.
_MIN_SUBSTRING_LEN = 6
# Small numeric "ids" are list indexes, not identifiers.
_MIN_ID = 1_000
_MAX_DECIMALS = 6
_EVENT_OFFSET_MINUTES = (60, 240)

CAPTURE_INDEX = "index.json"


class _Rule(Enum):
    NONE = auto()


type _RuleResult = Json | Literal[_Rule.NONE]


@dataclass
class Anonymiser:
    """Anonymises documents one after the other, with one consistent mapping.

    The seed makes the synthetic measurements reproducible; it is unrelated to
    the real values, which cannot be recovered from the output.
    """

    shift_days: int
    seed: str = ""
    _draws: int = 0
    _ids: dict[int | str, int | str] = field(default_factory=dict)
    _originals: set[int | float | str] = field(default_factory=set)
    _original_dates: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.shift_days == 0 or self.shift_days % 7:
            msg = (
                "shift_days must be a non-zero number of whole weeks, "
                f"not {self.shift_days}"
            )
            raise ValueError(msg)

    def anonymise(self, doc: Json) -> Json:
        return self._walk(None, doc)

    def _walk(self, key: str | None, value: Json, minutes: int = 0) -> Json:
        """Anonymise value; minutes moves it when it is an event time."""
        if isinstance(value, dict):
            # One offset per object: an activity's start and end move together,
            # and two activities do not share an offset.
            offset = self._event_offset() if any(map(_EVENT_KEY.search, value)) else 0
            return {
                k: self._walk(k, v, offset if _EVENT_KEY.search(k) else 0)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self._walk(key, v, minutes) for v in value]
        if value is None or isinstance(value, bool):
            return value
        if key is not None:
            replaced = self._by_key(key, value)
            if replaced is not _Rule.NONE:
                if replaced != value:
                    self._remember(value)
                return replaced
        if isinstance(value, str):
            return self._string(value, minutes)
        return self._number(key, value, minutes)

    def _by_key(self, key: str, value: int | float | str) -> _RuleResult:
        """Return the replacement a key rule dictates, or _Rule.NONE."""
        if key in DEMOGRAPHICS:
            return DEMOGRAPHICS[key]
        if _ID_KEY.search(key) and not _KEPT_ID_KEY.search(key):
            return self._fake_id(value)
        if (
            isinstance(value, str)
            and _NAME_KEY.search(key)
            and not _KEPT_NAME_KEY.search(key)
        ):
            return FAKE_NAME
        if isinstance(value, str) and _URL_KEY.search(key):
            return FAKE_URL
        if not isinstance(value, str) and _LATITUDE_KEY.search(key):
            return FAKE_LATITUDE
        if not isinstance(value, str) and _LONGITUDE_KEY.search(key):
            return FAKE_LONGITUDE
        return _Rule.NONE

    def _fake_id(self, value: int | float | str) -> Json:
        if isinstance(value, float) or (
            isinstance(value, int) and abs(value) < _MIN_ID
        ):
            return value
        if value not in self._ids:
            number = FIRST_FAKE_ID + len(self._ids)
            self._ids[value] = number if isinstance(value, int) else f"anon-{number}"
        return self._ids[value]

    def _remember(self, value: int | float | str) -> None:
        # Only distinctive numbers are tracked: a replaced age of 41 would
        # otherwise flag every heart rate of 41 as a leak.
        if isinstance(value, str) or _is_distinctive(value):
            self._originals.add(value)
        if isinstance(value, str):
            self._original_dates.update(_DATE_ANYWHERE.findall(value))

    def _event_offset(self) -> int:
        low, high = _EVENT_OFFSET_MINUTES
        minutes = low + int(self._draw() * (high - low + 1))
        return minutes if self._draw() < 0.5 else -minutes

    def _string(self, value: str, minutes: int = 0) -> str:
        self._original_dates.update(_DATE_ANYWHERE.findall(value))
        for email in _EMAIL.findall(value):
            self._originals.add(email)
        value = _EMAIL.sub(FAKE_EMAIL, value)
        moment = _DATETIME.match(value)
        if moment is not None and minutes:
            when = datetime.fromisoformat(f"{moment[1]}T{moment[3]}")
            when += timedelta(days=self.shift_days, minutes=minutes)
            day, time = when.isoformat().split("T")
            return f"{day}{moment[2]}{time}{moment[4]}"
        match = _DATE_PREFIX.match(value)
        if match is None:
            return value
        shifted = date.fromisoformat(match[1]) + timedelta(days=self.shift_days)
        return shifted.isoformat() + value[match.end() :]

    def _number(
        self, key: str | None, value: int | float, minutes: int = 0
    ) -> int | float:
        if _EPOCH_MS[0] <= value <= _EPOCH_MS[1]:
            return value + self.shift_days * 86_400_000 + minutes * 60_000
        is_time_key = key is not None and _EPOCH_SECONDS_KEY.search(key)
        if is_time_key and _EPOCH_S[0] <= value <= _EPOCH_S[1]:
            return value + self.shift_days * 86_400 + minutes * 60
        if key is not None and _KEPT_NUMBER_KEY.search(key):
            return value
        return self._synthetic(value)

    def _synthetic(self, value: int | float) -> int | float:
        """A made-up value with the same type, sign, digits and decimals."""
        if value == 0:
            return value
        sign = -1 if value < 0 else 1
        magnitude = abs(value)
        whole = int(magnitude)
        low, high = (10 ** (len(str(whole)) - 1), 10 ** len(str(whole)))
        if whole == 0:
            low, high = 0, 1
        decimals = 0 if isinstance(value, int) else _decimals(magnitude)
        while True:
            drawn = round(low + self._draw() * (high - low), decimals)
            # Never 0 (that means "not measured"), never the original value.
            if low <= drawn < high and drawn not in (0, magnitude):
                break
        if isinstance(value, int):
            return sign * int(drawn)
        return sign * float(drawn)

    def _draw(self) -> float:
        """A reproducible number in [0, 1), derived from the seed only."""
        self._draws += 1
        digest = hashlib.blake2b(
            f"{self.seed}:{self._draws}".encode(), digest_size=8
        ).digest()
        return int.from_bytes(digest) / 2**64

    def leaks_in(self, doc: Json, path: str = "$") -> list[str]:
        """Return the JSON paths of the values that still hold an original."""
        if isinstance(doc, dict):
            return [
                leak for k, v in doc.items() for leak in self.leaks_in(v, f"{path}.{k}")
            ]
        if isinstance(doc, list):
            return [
                leak
                for i, v in enumerate(doc)
                for leak in self.leaks_in(v, f"{path}[{i}]")
            ]
        if doc is None or isinstance(doc, bool):
            return []
        return [path] if self._is_original(doc) else []

    def _is_original(self, value: int | float | str) -> bool:
        if value in self._originals:
            return True
        if not isinstance(value, str):
            return False
        if any(date_ in value for date_ in self._original_dates):
            return True
        return any(
            isinstance(original, str)
            and len(original) >= _MIN_SUBSTRING_LEN
            and original in value
            for original in self._originals
        )


def _decimals(value: float) -> int:
    text = repr(value)
    if value.is_integer() or "e" in text:
        return 0
    return min(len(text.split(".")[1]), _MAX_DECIMALS)


def _is_distinctive(value: int | float) -> bool:
    return abs(value) >= _MIN_ID or (
        isinstance(value, float) and not value.is_integer()
    )


def find_leaks(anonymiser: Anonymiser, docs: dict[str, Json]) -> list[str]:
    return [
        f"{name}: {leak}"
        for name, doc in docs.items()
        for leak in anonymiser.leaks_in(doc)
    ]


def _keys(doc: Json) -> set[str]:
    if isinstance(doc, dict):
        return set(doc) | {k for v in doc.values() for k in _keys(v)}
    if isinstance(doc, list):
        return {k for v in doc for k in _keys(v)}
    return set()


def anonymise_directory(
    captures: Path, fixtures: Path, *, shift_days: int, seed: str | None = None
) -> list[str]:
    """Anonymise every capture; write the fixtures only if no leak remains.

    Without a seed, a fresh random one is used and never stored.
    """
    if seed is None:
        seed = secrets.token_hex(16)
    anonymiser = Anonymiser(shift_days=shift_days, seed=seed)
    raw = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(captures.glob("*.json"))
        if path.name != CAPTURE_INDEX
    }
    keys = set().union(*(_keys(doc) for doc in raw.values()))
    (captures / "keys.txt").write_text("\n".join(sorted(keys)) + "\n", encoding="utf-8")

    # Every capture is anonymised before any check, so that a value replaced in
    # one file is also caught where it appears unreplaced in another.
    anonymised = {name: anonymiser.anonymise(doc) for name, doc in raw.items()}
    leaks = find_leaks(anonymiser, anonymised)
    if leaks:
        return leaks

    fixtures.mkdir(parents=True, exist_ok=True)
    for name, doc in anonymised.items():
        text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
        (fixtures / name).write_text(text, encoding="utf-8")
    return []


def _random_shift() -> int:
    # One to two years back, so that a shifted date never lands on another
    # captured date (captures span at most a few weeks), and the real dates
    # cannot be guessed from the fixtures.
    return -7 * (53 + secrets.randbelow(52))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--captures", type=Path, default=Path("captures"))
    parser.add_argument("--fixtures", type=Path, default=Path("tests/fixtures"))
    parser.add_argument(
        "--shift-days",
        type=int,
        default=None,
        help="whole weeks, in days (default: a random 1 to 2 years back)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    shift = args.shift_days if args.shift_days is not None else _random_shift()
    leaks = anonymise_directory(args.captures, args.fixtures, shift_days=shift)
    if leaks:
        _LOGGER.error("Nothing written: original values remain at")
        for leak in leaks:
            _LOGGER.error("  %s", leak)
        _LOGGER.error("Add a rule for these keys, then run again.")
        return 1
    _LOGGER.info("Fixtures written to %s", args.fixtures)
    _LOGGER.info("Review them, and the key list in %s/keys.txt", args.captures)
    return 0


if __name__ == "__main__":
    sys.exit(main())
