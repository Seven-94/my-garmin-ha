"""Diagnostics for bug reports: the entry and its coordinators, redacted.

Coordinator data keeps its structure but loses every personal value:
identifiers and free text are redacted, measurements and times are replaced by
their shape.
"""

import dataclasses
import re
from collections.abc import Iterator, Mapping
from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from . import MyGarminConfigEntry
from .coordinator import MyGarminData

# Redaction rule: a key is redacted, at any depth, when its name matches one of
# these patterns. Matching names rather than listing fields means a field Garmin
# adds later is covered without a code change. Identifiers are redacted even
# when harmless (activity type IDs): the type key still says what it is.
_SENSITIVE = re.compile(
    r"(?:Id|ID|Pk|PK|UUID|uuid|[Nn]ame|[Dd]escription)$"
    r"|^id$"
    r"|(?i:token|secret|password|api_?key|cookie|authorization)"
    r"|(?i:e-?mail|url|latitude|longitude|location)"
)
# Menstrual and nutrition data are dropped with their whole subtree, key names
# included: the names alone say a lot about the person.
_EXCLUDED = re.compile(r"(?i:menstru|ovulat|pregnan|nutrition|food|meal|diet)")


# Times of day reveal habits (sleep, activities, weigh-ins): ISO dates and times
# are shown by their format only.
_TIME = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}|^[0-9]{1,2}:[0-9]{2}")
_DIGIT = re.compile(r"[0-9]")


def _shape(value: object) -> object:
    """Replace every measurement by its type and digits: 71350.0 -> <float 99999.9>.

    A dump is attached to a public issue: no health value may leave. The shape
    is what a parser bug needs (grams or kilograms, int or float, the format of
    a time). Zero, null and booleans stay, since Garmin uses them for "not
    measured"; so do catalogue codes, which are plain text.
    """
    if isinstance(value, Mapping):
        return {key: _shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_shape(item) for item in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int | float):
        if value == 0:
            return value
        return f"<{type(value).__name__} {_DIGIT.sub('9', str(value))}>"
    if isinstance(value, str) and _TIME.match(value):
        return f"<time {_DIGIT.sub('9', value)}>"
    return value


def _without_excluded(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            key: _without_excluded(item)
            for key, item in value.items()
            if not _EXCLUDED.search(key)
        }
    if isinstance(value, list):
        return [_without_excluded(item) for item in value]
    return value


def _key_names(value: object) -> Iterator[str]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield key
            yield from _key_names(item)
    elif isinstance(value, list):
        for item in value:
            yield from _key_names(item)


def _redacted(value: object) -> object:
    kept = _without_excluded(value)
    keys = {key for key in _key_names(kept) if _SENSITIVE.search(key)}
    return async_redact_data(kept, keys)


def _coordinator_report(coordinator: DataUpdateCoordinator[Any]) -> dict[str, Any]:
    error = coordinator.last_exception
    return {
        "name": coordinator.name,
        "last_update_success": coordinator.last_update_success,
        "update_interval": str(coordinator.update_interval),
        # The class alone: an error message can echo a URL or an account detail.
        "last_exception": None if error is None else type(error).__name__,
        "data": _shape(_redacted(coordinator.data)),
    }


def _coordinator_reports(entry: MyGarminConfigEntry) -> dict[str, dict[str, Any]]:
    # A set-up that failed never filled runtime_data: the entry is still reported.
    runtime: MyGarminData | None = getattr(entry, "runtime_data", None)
    if runtime is None:
        return {}
    reports: dict[str, dict[str, Any]] = {}
    for field in dataclasses.fields(runtime):
        value = getattr(runtime, field.name)
        if isinstance(value, DataUpdateCoordinator):
            reports[field.name] = _coordinator_report(value)
    return reports


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: MyGarminConfigEntry
) -> dict[str, Any]:
    del hass
    return {
        "entry": {
            "data": _redacted(entry.data),
            "options": _redacted(entry.options),
            # The Garmin profile ID: never shown.
            "unique_id": REDACTED,
        },
        "coordinators": _coordinator_reports(entry),
    }
