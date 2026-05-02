"""Tests for the cai-audit-deps GitHub Actions workflow file.

Validates the structure of ``.github/workflows/cai-audit-deps.yml``, a
hand-written scheduled workflow that runs ``cai-audit --mode deps`` on
the 1st of every month.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "cai-audit-deps.yml"


@pytest.fixture(scope="module")
def workflow() -> dict:
    """Parse and return the workflow YAML."""
    assert WORKFLOW_PATH.exists(), f"Workflow file not found: {WORKFLOW_PATH}"
    raw = WORKFLOW_PATH.read_text()
    return yaml.safe_load(raw)


# ── Name ─────────────────────────────────────────────────────────────────


def test_workflow_name(workflow: dict):
    """The workflow must have the expected display name."""
    assert workflow.get("name") == "CAI Audit Deps"


# ── Triggers ─────────────────────────────────────────────────────────────


def test_has_schedule_trigger(workflow: dict):
    """A schedule trigger must be present with the monthly cron expression."""
    on = workflow.get("on", {})
    assert "schedule" in on
    assert isinstance(on["schedule"], list)
    assert len(on["schedule"]) >= 1
    assert on["schedule"][0].get("cron") == "0 0 1 * *"


def test_has_workflow_dispatch_trigger(workflow: dict):
    """A workflow_dispatch trigger must be present for manual runs."""
    on = workflow.get("on", {})
    assert "workflow_dispatch" in on


def test_workflow_dispatch_has_no_inputs(workflow: dict):
    """The workflow_dispatch trigger must not define any inputs."""
    dispatch = workflow.get("on", {}).get("workflow_dispatch", {})
    if isinstance(dispatch, dict):
        assert "inputs" not in dispatch


# ── Jobs ─────────────────────────────────────────────────────────────────


def test_has_single_audit_job(workflow: dict):
    """There must be exactly one job named 'audit'."""
    jobs = workflow.get("jobs", {})
    assert list(jobs.keys()) == ["audit"]


def test_audit_job_runs_on_ubuntu(workflow: dict):
    """The audit job must run on ubuntu-latest."""
    job = workflow.get("jobs", {}).get("audit", {})
    assert job.get("runs-on") == "ubuntu-latest"


def test_audit_job_has_no_if_condition(workflow: dict):
    """The audit job must NOT have an ``if:`` condition (no gating)."""
    job = workflow.get("jobs", {}).get("audit", {})
    assert "if" not in job, "audit job should not have a conditional gate"


def test_audit_job_has_no_needs(workflow: dict):
    """The audit job must NOT have a ``needs:`` declaration (no dependency)."""
    job = workflow.get("jobs", {}).get("audit", {})
    assert "needs" not in job, "audit job should not depend on another job"


def test_audit_job_has_no_strategy(workflow: dict):
    """The audit job must NOT have a ``strategy:`` block (no matrix)."""
    job = workflow.get("jobs", {}).get("audit", {})
    assert "strategy" not in job, "audit job should not have a strategy/matrix block"


def test_audit_job_permissions_are_contents_read(workflow: dict):
    """The job must request only contents: read permission."""
    perms = workflow.get("jobs", {}).get("audit", {}).get("permissions", {})
    assert perms == {"contents": "read"}


# ── Steps ────────────────────────────────────────────────────────────────


def test_audit_job_has_expected_steps(workflow: dict):
    """The job must contain exactly three steps in order."""
    steps = workflow.get("jobs", {}).get("audit", {}).get("steps", [])
    assert len(steps) == 3

    # Step 1: Checkout
    assert steps[0].get("name") == "Checkout repository"
    assert steps[0].get("uses") == "actions/checkout@v4"

    # Step 2: Stage cai config
    assert steps[1].get("name") == "Stage cai config for container"
    assert steps[1].get("uses") == "./.github/actions/setup-cai"
    assert steps[1].get("with", {}).get("cai_github_app_pem") == "${{ secrets.CAI_GITHUB_APP_PEM }}"
    assert steps[1].get("with", {}).get("cai_app_env") == "${{ secrets.CAI_APP_ENV }}"

    # Step 3: Run CAI Audit Deps
    assert steps[2].get("name") == "Run CAI Audit Deps"


def test_docker_run_command(workflow: dict):
    """The docker run command must use cai-audit --mode deps with the repo placeholder."""
    steps = workflow.get("jobs", {}).get("audit", {}).get("steps", [])
    run_script = steps[2].get("run", "")

    assert "docker run --rm" in run_script
    assert 'cai-audit --repo "${{ github.repository }}" --mode deps' in run_script


def test_docker_run_has_volume_mount(workflow: dict):
    """The docker run command must mount the cai config volume."""
    steps = workflow.get("jobs", {}).get("audit", {}).get("steps", [])
    run_script = steps[2].get("run", "")

    assert '-v "${RUNNER_TEMP}/cai-config:/home/cai/.config/cai:ro"' in run_script


def test_docker_run_has_required_secrets(workflow: dict):
    """All four required secrets must be passed as environment variables."""
    steps = workflow.get("jobs", {}).get("audit", {}).get("steps", [])
    run_script = steps[2].get("run", "")

    assert 'OPENROUTER_API_KEY="${{ secrets.OPENROUTER_API_KEY }}"' in run_script
    assert 'LANGFUSE_SECRET_KEY="${{ secrets.LANGFUSE_SECRET_KEY }}"' in run_script
    assert 'LANGFUSE_PUBLIC_KEY="${{ secrets.LANGFUSE_PUBLIC_KEY }}"' in run_script
    assert 'LANGFUSE_BASE_URL="${{ secrets.LANGFUSE_BASE_URL }}"' in run_script


def test_docker_uses_correct_image(workflow: dict):
    """The Docker image must be docker.io/robotsix/cai:latest."""
    steps = workflow.get("jobs", {}).get("audit", {}).get("steps", [])
    run_script = steps[2].get("run", "")

    assert "docker.io/robotsix/cai:latest" in run_script


# ── Structural properties that must NOT be present ──────────────────────


def test_no_concurrency_block(workflow: dict):
    """The workflow must not have a top-level concurrency block."""
    assert "concurrency" not in workflow


def test_no_gate_or_check_job(workflow: dict):
    """The workflow must not have a separate gate/check job."""
    jobs = workflow.get("jobs", {})
    assert "check" not in jobs


def test_no_authorized_user_step(workflow: dict):
    """The workflow must not verify authorized users."""
    steps = workflow.get("jobs", {}).get("audit", {}).get("steps", [])
    step_names = [s.get("name") for s in steps]
    assert "Verify authorized user" not in step_names


# ── Edge cases ───────────────────────────────────────────────────────────


def test_workflow_file_parses_as_valid_yaml():
    """The workflow file must be syntactically valid YAML."""
    raw = WORKFLOW_PATH.read_text()
    try:
        yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        pytest.fail(f"Workflow file is not valid YAML: {exc}")


def test_workflow_file_does_not_contain_tabs():
    """YAML should not contain tab characters (YAML forbids tabs)."""
    raw = WORKFLOW_PATH.read_text()
    assert "\t" not in raw, "Workflow file contains tab characters"


@pytest.mark.parametrize(
    "expected_string",
    [
        "actions/checkout@v4",
        "./.github/actions/setup-cai",
        "docker.io/robotsix/cai:latest",
        "cai-audit",
    ],
)
def test_workflow_contains_expected_refs(workflow: dict, expected_string: str):
    """Key action references and binary names must appear somewhere in the workflow."""
    raw_yaml = yaml.dump(workflow)
    assert expected_string in raw_yaml


def test_cron_key_is_quoted_in_yaml():
    """The ``cron`` key must be quoted (``"cron":``) in the raw YAML.

    Quoting prevents PyYAML from treating the value as a special type and
    matches the convention described in the implementation summary.
    """
    raw = WORKFLOW_PATH.read_text()
    assert '"cron":' in raw, (
        "Expected the cron key to be quoted as '\"cron\":' "
        "to prevent PyYAML misinterpretation"
    )


def test_runs_on_key_is_quoted_in_yaml():
    """The ``runs-on`` key must be quoted (``"runs-on":``) in the raw YAML.

    Quoting prevents PyYAML from interpreting ``runs-on`` as a substring
    match hazard, matching the convention enforced by the test suite.
    """
    raw = WORKFLOW_PATH.read_text()
    assert '"runs-on":' in raw, (
        "Expected the runs-on key to be quoted as '\"runs-on\":'"
    )


def test_on_key_is_quoted_in_yaml():
    """The ``on`` trigger key must be quoted (``"on":``) in the raw YAML.

    PyYAML interprets an unquoted ``on:`` as the boolean ``True``, which
    would make the workflow dict unreadable via ``workflow.get("on", {})``.
    Quoting it as ``"on":`` forces string-key parsing.
    """
    raw = WORKFLOW_PATH.read_text()
    assert '"on":' in raw, (
        "Expected the trigger key to be quoted as '\"on\":' "
        "to prevent PyYAML from parsing it as True"
    )
    # Ensure there is no unquoted 'on:' variant at a YAML key position
    assert 'on:\n' not in raw and 'on: ' not in raw, (
        "Unquoted 'on:' found — PyYAML will parse it as boolean True"
    )
