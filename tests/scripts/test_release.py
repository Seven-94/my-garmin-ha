"""Release preparation: tag check, release notes and the HACS zip."""

import json
import zipfile
from pathlib import Path

import pytest

from scripts.release import (
    ReleaseError,
    build_zip,
    check_tag,
    main,
    release_notes,
)

ROOT = Path(__file__).resolve().parent.parent.parent

CHANGELOG = """# Changelog

## [Unreleased]

### Added

- Something not released yet.

## [1.2.0] - 2026-12-01

### Added

- Feature A.

### Fixed

- Bug B.

## [1.1.0] - 2026-11-20

- Older entry.

[Unreleased]: https://example.invalid/compare/v1.2.0...HEAD
"""


def _component(root: Path, version: str = "1.2.0") -> Path:
    component = root / "custom_components" / "my_garmin"
    (component / "translations").mkdir(parents=True)
    (component / "__pycache__").mkdir()
    (component / "manifest.json").write_text(
        json.dumps({"domain": "my_garmin", "version": version}), encoding="utf-8"
    )
    (component / "__init__.py").write_text('"""Init."""\n', encoding="utf-8")
    (component / "translations" / "en.json").write_text("{}", encoding="utf-8")
    (component / "__pycache__" / "x.cpython-314.pyc").write_bytes(b"\0")
    return component


def test_check_tag_accepts_the_manifest_version() -> None:
    assert check_tag("v1.2.0", {"version": "1.2.0"}) == "1.2.0"


@pytest.mark.parametrize("tag", ["1.2.0", "v1.2.1", "v1.2", "release-1.2.0"])
def test_check_tag_rejects_a_tag_that_is_not_v_and_the_manifest_version(
    tag: str,
) -> None:
    with pytest.raises(ReleaseError, match=r"v1\.2\.0"):
        check_tag(tag, {"version": "1.2.0"})


def test_check_tag_rejects_a_manifest_without_version() -> None:
    with pytest.raises(ReleaseError, match="version"):
        check_tag("v1.2.0", {})


def test_release_notes_are_the_version_section_without_its_heading() -> None:
    notes = release_notes(CHANGELOG, "1.2.0")

    assert notes == "### Added\n\n- Feature A.\n\n### Fixed\n\n- Bug B.\n"


def test_release_notes_of_the_oldest_version_stop_at_the_link_list() -> None:
    assert release_notes(CHANGELOG, "1.1.0") == "- Older entry.\n"


@pytest.mark.parametrize("version", ["1.3.0", "1.2", "Unreleased"])
def test_release_notes_require_a_released_section(version: str) -> None:
    with pytest.raises(ReleaseError, match="CHANGELOG"):
        release_notes(CHANGELOG, version)


def test_release_notes_reject_an_empty_section() -> None:
    changelog = "## [1.2.0] - 2026-12-01\n\n## [1.1.0] - 2026-11-20\n\n- Old.\n"

    with pytest.raises(ReleaseError, match="empty"):
        release_notes(changelog, "1.2.0")


def test_zip_holds_the_component_files_at_its_root(tmp_path: Path) -> None:
    component = _component(tmp_path)
    archive = tmp_path / "dist" / "my_garmin.zip"

    build_zip(component, archive)

    with zipfile.ZipFile(archive) as zipped:
        names = zipped.namelist()
        assert names == ["__init__.py", "manifest.json", "translations/en.json"]
        manifest = json.loads(zipped.read("manifest.json"))
    assert manifest["version"] == "1.2.0"


def test_zip_is_reproducible(tmp_path: Path) -> None:
    component = _component(tmp_path)

    build_zip(component, tmp_path / "a.zip")
    build_zip(component, tmp_path / "b.zip")

    assert (tmp_path / "a.zip").read_bytes() == (tmp_path / "b.zip").read_bytes()


def test_zip_stores_files_uncompressed(tmp_path: Path) -> None:
    # Deflate output depends on the zlib build (zlib or zlib-ng): stored
    # entries give the same bytes on the CI runner and on any machine.
    component = _component(tmp_path)
    archive = tmp_path / "my_garmin.zip"

    build_zip(component, archive)

    with zipfile.ZipFile(archive) as zipped:
        for info in zipped.infolist():
            assert info.compress_type == zipfile.ZIP_STORED, info.filename
            assert info.compress_size == info.file_size, info.filename


def test_main_writes_the_zip_and_the_notes(tmp_path: Path) -> None:
    _component(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    out = tmp_path / "dist"

    assert main(["v1.2.0", "--root", str(tmp_path), "--out", str(out)]) == 0

    assert (
        (out / "release-notes.md").read_text(encoding="utf-8").startswith("### Added")
    )
    assert zipfile.is_zipfile(out / "my_garmin.zip")


def test_main_fails_without_writing_on_a_wrong_tag(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _component(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    out = tmp_path / "dist"

    assert main(["v9.9.9", "--root", str(tmp_path), "--out", str(out)]) == 1

    assert not out.exists()
    assert "v1.2.0" in caplog.text


def test_hacs_manifest_points_at_the_release_zip() -> None:
    hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))

    assert hacs["zip_release"] is True
    assert hacs["filename"] == "my_garmin.zip"
