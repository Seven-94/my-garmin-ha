import json
import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _run_commands(workflow: Path) -> list[str]:
    return [
        str(step["run"])
        for step in _load(workflow)["jobs"]["test"]["steps"]
        if "run" in step
    ]


def test_ci_audits_all_workflows_with_zizmor() -> None:
    commands = _run_commands(CI_WORKFLOW)
    assert any("zizmor" in cmd and ".github/workflows" in cmd for cmd in commands)


def test_ci_runs_tests_lint_and_type_check() -> None:
    commands = " ".join(_run_commands(CI_WORKFLOW))
    for tool in ("pytest", "ruff check", "ruff format --check", "ty check"):
        assert tool in commands


def test_workflows_default_to_no_permissions() -> None:
    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        assert _load(workflow)["permissions"] == {}, workflow.name


def test_no_dangerous_triggers() -> None:
    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        # PyYAML reads the unquoted key `on` as the boolean True.
        triggers = _load(workflow)[True]
        assert "pull_request_target" not in triggers, workflow.name
        assert "workflow_run" not in triggers, workflow.name


def _uses(workflow: Path) -> list[str]:
    return [
        str(step["uses"])
        for job in _load(workflow)["jobs"].values()
        for step in job.get("steps", [])
        if "uses" in step
    ]


def test_actions_are_pinned_by_commit_sha() -> None:
    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        for action in _uses(workflow):
            ref = action.rpartition("@")[2]
            assert re.fullmatch(r"[0-9a-f]{40}", ref), (workflow.name, action)


def test_ci_validates_with_hassfest_and_hacs() -> None:
    actions = _uses(CI_WORKFLOW)
    assert any(a.startswith("home-assistant/actions/hassfest@") for a in actions)
    assert any(a.startswith("hacs/action@") for a in actions)


# The one job allowed to write: it creates the GitHub release of a tag.
WRITERS = {("release.yml", "release"): {"contents": "write"}}


def test_every_job_reads_only_but_the_release() -> None:
    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        for name, job in _load(workflow)["jobs"].items():
            expected = WRITERS.get((workflow.name, name), {"contents": "read"})
            assert job["permissions"] == expected, (workflow.name, name)


def test_releases_start_from_a_version_tag_only() -> None:
    triggers = _load(RELEASE_WORKFLOW)[True]

    assert triggers == {"push": {"tags": ["v*"]}}


def test_release_job_builds_with_the_tested_script() -> None:
    steps = _load(RELEASE_WORKFLOW)["jobs"]["release"]["steps"]
    commands = " ".join(str(step["run"]) for step in steps if "run" in step)

    assert "scripts.release" in commands
    assert "gh release create" in commands


def test_hacs_manifest_names_the_integration() -> None:
    manifest = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "My Garmin"
    assert "homeassistant" in manifest
