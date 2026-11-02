"""Checks the chart and release automation this template gives Standard projects."""

import json
import re
import tomllib
from pathlib import Path

import pytest
import yaml
from conftest import RENDERABLE_ANSWERS, _render

STANDARD_ANSWERS = [
    a for a in RENDERABLE_ANSWERS if a.get("project_type", "Standard") == "Standard"
]
STANDARD_GITHUB_ANSWERS = [
    a for a in STANDARD_ANSWERS if a.get("developer_platform") == "GitHub"
]
STANDARD_AZDO_ANSWERS = [
    a for a in STANDARD_ANSWERS if a.get("developer_platform") == "Azure DevOps"
]
TEMPLATE_ANSWERS = [
    a for a in RENDERABLE_ANSWERS if a.get("project_type") == "Template"
]


def _repository(answers: dict) -> str:
    prefix = answers.get("acr_chart_prefix", "")
    return f"helm/{prefix}" if prefix else "helm"


@pytest.mark.parametrize("answers", STANDARD_ANSWERS, ids=lambda a: a["id"])
def test_knope_versions_the_chart(template_source, tmp_path: Path, answers):
    """Knope has to bump Chart.yaml's version with each release, starting in step.

    Knope reads each versioned file's version through its regex, so the regex has to
    find Chart.yaml's version line and nothing else, such as appVersion.
    """
    dest = tmp_path / "out"
    _render(template_source, dest, answers)
    chart_path = f"src/{answers['helm_chart_name']}/Chart.yaml"
    chart = (dest / chart_path).read_text(encoding="utf-8")
    knope = tomllib.loads((dest / "knope.toml").read_text(encoding="utf-8"))
    entry = next(
        f for f in knope["package"]["versioned_files"] if f["path"] == chart_path
    )
    # Knope's regex syntax names groups as (?<name>), Python's as (?P<name>).
    matches = re.findall(entry["regex"].replace("(?<", "(?P<"), chart)
    current = json.loads(
        (dest / ".config" / "knope-current-version.json").read_text(encoding="utf-8")
    )["version"]
    assert matches == [current]  # noqa: S101
    assert yaml.safe_load(chart)["name"] == answers["helm_chart_name"]  # noqa: S101


@pytest.mark.parametrize("answers", STANDARD_GITHUB_ANSWERS, ids=lambda a: a["id"])
def test_release_dispatches_chart_publish(template_source, tmp_path: Path, answers):
    """Publishing a release has to dispatch the chart workflow, which pushes to ACR.

    The tag Knope creates with GITHUB_TOKEN fires no `push` event, so nothing else
    publishes the chart. The publish job also needs `actions: write` to dispatch it.
    """
    dest = tmp_path / "out"
    _render(template_source, dest, answers)
    workflows = dest / ".github" / "workflows"
    publish = (workflows / "release-auto-publishrelease.yml").read_text(
        encoding="utf-8"
    )
    assert "gh workflow run helm-auto-publishrelease.yml" in publish  # noqa: S101
    assert "actions: write" in publish  # noqa: S101
    chart = yaml.safe_load(
        (workflows / "helm-auto-publishrelease.yml").read_text(encoding="utf-8")
    )
    assert chart["env"]["REPOSITORY"] == _repository(answers)  # noqa: S101
    assert chart["jobs"]["publish-chart"]["permissions"]["id-token"] == "write"  # noqa: S101


@pytest.mark.parametrize("answers", STANDARD_AZDO_ANSWERS, ids=lambda a: a["id"])
def test_release_pipeline_triggers_chart_publish(
    template_source, tmp_path: Path, answers
):
    """The chart pipeline has to follow the release pipeline by its registered name.

    A pipeline resource names its source pipeline by display name, so a mismatch with
    copier.yml's `az pipelines create` name means it never runs.
    """
    dest = tmp_path / "out"
    _render(template_source, dest, answers)
    pipeline = yaml.safe_load(
        (dest / ".azurepipelines" / "helm-auto-publishrelease.yml").read_text(
            encoding="utf-8"
        )
    )
    (source,) = pipeline["resources"]["pipelines"]
    assert source["source"] == (  # noqa: S101
        f"[{answers['repo_name']}] Release (Auto) - Prepare & Publish Release"
    )
    assert pipeline["variables"]["REPOSITORY"] == _repository(answers)  # noqa: S101
    task = pipeline["jobs"][0]["steps"][-1]
    assert task["inputs"]["azureSubscription"] == answers["acr_service_connection"]  # noqa: S101


@pytest.mark.parametrize("answers", TEMPLATE_ANSWERS, ids=lambda a: a["id"])
def test_helm_tasks_pass_only_known_flags(template_source, tmp_path: Path, answers):
    """Every flag the Helm test tasks pass has to be one the script's parser accepts.

    mise.toml.jinja's tasks vary by answer, and an unknown flag only fails when someone
    runs the task.
    """
    dest = tmp_path / "out"
    _render(template_source, dest, answers)
    tasks = tomllib.loads((dest / "mise.toml").read_text(encoding="utf-8"))["tasks"]
    script = (dest / "scripts" / "integration_test_helm.py").read_text(encoding="utf-8")
    names = [name for name in tasks if name.startswith("integration-test-helm-")]
    assert len(names) == 4  # noqa: S101
    for name in names:
        for flag in re.findall(r"(?<!\S)--[a-z-]+", tasks[name]["run"]):
            assert f'"{flag}"' in script, f"{name} passes unknown {flag}"  # noqa: S101
