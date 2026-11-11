import json
import re
from pathlib import Path
from typing import Any

import pytest

from custom_components.my_garmin import DOMAIN

INTEGRATION_DIR = Path(__file__).parent.parent / "custom_components" / DOMAIN

# Keys Home Assistant and HACS require in a custom integration manifest.
REQUIRED_KEYS = {
    "codeowners",
    "documentation",
    "domain",
    "iot_class",
    "issue_tracker",
    "name",
    "requirements",
    "version",
}


@pytest.fixture
def manifest() -> dict[str, Any]:
    return json.loads((INTEGRATION_DIR / "manifest.json").read_text(encoding="utf-8"))


def test_manifest_has_required_keys(manifest: dict[str, Any]) -> None:
    # A set difference makes a failure name the missing keys.
    assert REQUIRED_KEYS - manifest.keys() == set()


def test_manifest_domain_matches_folder_and_constant(manifest: dict[str, Any]) -> None:
    assert manifest["domain"] == DOMAIN == INTEGRATION_DIR.name


def test_manifest_version_is_semver(manifest: dict[str, Any]) -> None:
    # Home Assistant refuses to load a custom integration without a valid version.
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])


def test_manifest_has_no_runtime_requirement(manifest: dict[str, Any]) -> None:
    # Everything the integration needs (aiohttp included) ships with Home Assistant.
    assert manifest["requirements"] == []
