"""Prepare a GitHub release: check the tag, extract the notes, zip the component.

Usage, from the repository root (the release workflow runs it on a tag push):

    uv run --no-project python -m scripts.release v1.2.0 --out dist

Writes dist/my_garmin.zip, the file HACS installs (see `filename` in
hacs.json), and dist/release-notes.md, the CHANGELOG section of the version.
Standard library only: the release job installs no dependency.
"""

import argparse
import json
import logging
import re
import sys
import zipfile
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

COMPONENT = Path("custom_components") / "my_garmin"
ZIP_NAME = "my_garmin.zip"
NOTES_NAME = "release-notes.md"
# Fixed: the same sources give the same bytes, whatever the checkout time.
_ZIP_DATE = (2026, 1, 1, 0, 0, 0)
_SKIPPED_PARTS = {"__pycache__"}
_SKIPPED_SUFFIXES = {".pyc", ".pyo"}


class ReleaseError(Exception):
    """The repository is not ready for this release."""


def check_tag(tag: str, manifest: dict[str, Any]) -> str:
    """The version to release; the tag must be "v" and the manifest version."""
    version = manifest.get("version")
    if not isinstance(version, str) or not version:
        raise ReleaseError("manifest.json has no version")
    if tag != f"v{version}":
        raise ReleaseError(
            f"Tag {tag} does not match manifest.json: expected v{version}"
        )
    return version


def release_notes(changelog: str, version: str) -> str:
    """The body of the `## [version] - YYYY-MM-DD` section of a Keep a Changelog.

    The date is required: an undated heading, such as [Unreleased], is not a
    release yet.
    """
    heading = re.compile(
        rf"^## \[{re.escape(version)}\] - [0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}[^\n]*$",
        re.MULTILINE,
    )
    start = heading.search(changelog)
    if start is None:
        raise ReleaseError(f"CHANGELOG.md has no dated section for {version}")
    rest = changelog[start.end() :]
    # The next version, or the link definitions at the end of the file.
    end = re.search(r"^(?:## |\[[^\]]+\]: )", rest, re.MULTILINE)
    body = (rest[: end.start()] if end else rest).strip()
    if not body:
        raise ReleaseError(f"The CHANGELOG.md section for {version} is empty")
    return body + "\n"


def _component_files(component: Path) -> list[Path]:
    return sorted(
        path
        for path in component.rglob("*")
        if path.is_file()
        and not _SKIPPED_PARTS.intersection(path.relative_to(component).parts)
        and path.suffix not in _SKIPPED_SUFFIXES
    )


def build_zip(component: Path, archive: Path) -> None:
    """Zip the component's files at the archive root, as HACS extracts them."""
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        for path in _component_files(component):
            info = zipfile.ZipInfo(
                path.relative_to(component).as_posix(), date_time=_ZIP_DATE
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zipped.writestr(info, path.read_bytes())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tag", help="the pushed tag, such as v1.2.0")
    parser.add_argument("--root", type=Path, default=Path())
    parser.add_argument("--out", type=Path, default=Path("dist"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    component = args.root / COMPONENT
    try:
        manifest = json.loads((component / "manifest.json").read_text("utf-8"))
        version = check_tag(args.tag, manifest)
        changelog = (args.root / "CHANGELOG.md").read_text(encoding="utf-8")
        notes = release_notes(changelog, version)
    except ReleaseError as err:
        _LOGGER.error("No release: %s", err)
        return 1

    build_zip(component, args.out / ZIP_NAME)
    (args.out / NOTES_NAME).write_text(notes, encoding="utf-8")
    _LOGGER.info("Release %s ready in %s", version, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
