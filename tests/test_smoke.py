from importlib.metadata import version

from my_garmin_ha import __version__


def test_version_is_defined() -> None:
    assert isinstance(__version__, str)
    assert len(__version__) > 0


def test_version_matches_installed_metadata() -> None:
    assert __version__ == version("my-garmin-ha")
