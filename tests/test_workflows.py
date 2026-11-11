from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


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
