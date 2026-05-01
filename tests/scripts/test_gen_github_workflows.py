"""Tests for ``scripts/gen_github_workflows.py``.

Covers the shape-determination logic and Jinja template rendering that
produces ``.github/workflows/cai-*.yml`` from the registry entries.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from cai.workflows.registry import (
    GitHubTrigger,
    GitHubTriggerEvent,
    WorkflowSpec,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

# ── Load gen_github_workflows as a module (it lives under scripts/, not src/) ──

_gen_spec = importlib.util.spec_from_file_location(
    "gen_github_workflows",
    str(SCRIPTS_DIR / "gen_github_workflows.py"),
)
_gen_module = importlib.util.module_from_spec(_gen_spec)
# Prevent the module's __main__ guard from running; we just want its definitions.
with patch.object(sys, "exit", lambda _: None):
    _gen_spec.loader.exec_module(_gen_module)

_determine_shape = _gen_module._determine_shape


def _render(spec, shape):
    """Render a spec through the Jinja template (lazy import matches gen_github_workflows)."""
    from jinja2 import Environment, FileSystemLoader

    env = Environment(
        loader=FileSystemLoader(str(SCRIPTS_DIR / "templates")),
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        variable_start_string="<%",
        variable_end_string="%>",
    )
    template = env.get_template("cai_workflow.yml.j2")
    return template.render(spec=spec, shape=shape)


# ── Helpers ─────────────────────────────────────────────────────────────


def _make_spec(*, slug="test", events, job_if=None, docker_command="cai-test",
               permissions=None, concurrency_group=None,
               authorized_user_variant="none"):
    """Build a minimal WorkflowSpec for use in template rendering."""
    from cai.workflows.audit import audit_graph
    return WorkflowSpec(
        slug=slug,
        title=f"CAI {slug.title()}",
        nav_order=99,
        blurb="Test workflow.",
        graph=audit_graph,
        cli_entry="cai.workflows.audit:main",
        session_id=lambda: "test-session",
        github_trigger=GitHubTrigger(
            on=events,
            job_if=job_if,
        ),
        docker_command=docker_command,
        permissions=permissions or {"contents": "read"},
        concurrency_group=concurrency_group,
        authorized_user_variant=authorized_user_variant,
    )


# ── _determine_shape ────────────────────────────────────────────────────


@pytest.mark.parametrize("events,expected_shape", [
    ([GitHubTriggerEvent(event="issues", types=["labeled"])], "simple"),
    ([GitHubTriggerEvent(event="workflow_dispatch")], "simple"),
    ([GitHubTriggerEvent(event="schedule", cron="0 8 1 * *")], "simple"),
    ([GitHubTriggerEvent(event="pull_request_review", types=["submitted"])], "simple"),
    ([GitHubTriggerEvent(event="push", branches=["main"])], "gate"),
    ([GitHubTriggerEvent(event="workflow_run", workflows=["CI"], types=["completed"], branches=["main"])], "resolve"),
])
def test_determine_shape(events, expected_shape):
    """_determine_shape returns the correct shape for each trigger event."""
    spec = _make_spec(events=events)
    assert _determine_shape(spec) == expected_shape


def test_determine_shape_workflow_run_takes_precedence_over_push():
    """When both workflow_run and push are present, workflow_run wins."""
    spec = _make_spec(events=[
        GitHubTriggerEvent(event="push", branches=["main"]),
        GitHubTriggerEvent(event="workflow_run", workflows=["CI"], types=["completed"], branches=["main"]),
    ])
    assert _determine_shape(spec) == "resolve"


def test_determine_shape_push_with_other_events():
    """When push is present alongside non-workflow_run events, gate is returned."""
    spec = _make_spec(events=[
        GitHubTriggerEvent(event="workflow_dispatch"),
        GitHubTriggerEvent(event="push", branches=["main"]),
    ])
    assert _determine_shape(spec) == "gate"


# ── Template rendering: simple shape ────────────────────────────────────


def test_template_render_simple_minimal():
    """A simple-shape spec renders the basic job structure."""
    spec = _make_spec(
        slug="demo",
        events=[GitHubTriggerEvent(event="workflow_dispatch")],
        docker_command="cai-demo",
    )
    rendered = _render(spec, "simple")

    assert "name: CAI Demo" in rendered
    assert "workflow_dispatch:" in rendered
    assert "jobs:" in rendered
    assert "  demo:" in rendered
    assert "cai-demo" in rendered


def test_template_render_simple_with_job_if():
    """When job_if is set, the job includes an ``if:`` condition."""
    spec = _make_spec(
        slug="demo",
        events=[GitHubTriggerEvent(event="issues", types=["labeled"])],
        job_if="github.event.label.name == 'cai:raised'",
        docker_command="cai-demo",
    )
    rendered = _render(spec, "simple")

    assert "if: github.event.label.name == 'cai:raised'" in rendered


def test_template_render_simple_without_job_if_has_no_if_line():
    """When job_if is None, the rendered job has no ``if:`` line."""
    spec = _make_spec(
        slug="demo",
        events=[GitHubTriggerEvent(event="workflow_dispatch")],
        job_if=None,
        docker_command="cai-demo",
    )
    rendered = _render(spec, "simple")

    assert "    if:" not in rendered


def test_template_render_simple_with_concurrency():
    """When concurrency_group is set, the concurrency block is rendered."""
    spec = _make_spec(
        slug="demo",
        events=[GitHubTriggerEvent(event="issues", types=["labeled"])],
        concurrency_group="cai-demo-${{ github.event.issue.number }}",
        docker_command="cai-demo",
    )
    rendered = _render(spec, "simple")

    assert "concurrency:" in rendered
    assert "group: cai-demo-${{ github.event.issue.number }}" in rendered
    assert "cancel-in-progress: true" in rendered


def test_template_render_simple_without_concurrency_group():
    """When concurrency_group is None, no concurrency block is rendered."""
    spec = _make_spec(
        slug="demo",
        events=[GitHubTriggerEvent(event="workflow_dispatch")],
        concurrency_group=None,
        docker_command="cai-demo",
    )
    rendered = _render(spec, "simple")

    assert "concurrency:" not in rendered


def test_template_render_simple_standard_auth():
    """The standard authorized_user_variant renders the approved-users guard."""
    spec = _make_spec(
        slug="demo",
        events=[GitHubTriggerEvent(event="workflow_dispatch")],
        authorized_user_variant="standard",
        docker_command="cai-demo",
    )
    rendered = _render(spec, "simple")

    assert "Verify authorized user" in rendered
    assert "APPROVED_AI_USERS" in rendered
    assert "Unauthorized user" in rendered
    # Should NOT contain skip_bots logic
    assert "Skipping bot actor" not in rendered


def test_template_render_simple_skip_bots_auth():
    """The skip_bots variant renders the bot-skip guard before the approved-users check."""
    spec = _make_spec(
        slug="demo",
        events=[GitHubTriggerEvent(event="pull_request_review", types=["submitted"])],
        authorized_user_variant="skip_bots",
        docker_command="cai-demo",
    )
    rendered = _render(spec, "simple")

    assert "Verify authorized user" in rendered
    assert "Skipping bot actor" in rendered
    assert "APPROVED_AI_USERS" in rendered


def test_template_render_simple_none_auth():
    """The none variant renders no authorized-user verification step."""
    spec = _make_spec(
        slug="demo",
        events=[GitHubTriggerEvent(event="workflow_dispatch")],
        authorized_user_variant="none",
        docker_command="cai-demo",
    )
    rendered = _render(spec, "simple")

    assert "Verify authorized user" not in rendered


def test_template_render_simple_permissions():
    """Permissions are injected into the job permissions block."""
    spec = _make_spec(
        slug="demo",
        events=[GitHubTriggerEvent(event="workflow_dispatch")],
        permissions={"contents": "write", "pull-requests": "read"},
        docker_command="cai-demo",
    )
    rendered = _render(spec, "simple")

    assert "contents: write" in rendered
    assert "pull-requests: read" in rendered


def test_template_render_simple_issues_event():
    """An ``issues`` event renders types."""
    spec = _make_spec(
        slug="demo",
        events=[GitHubTriggerEvent(event="issues", types=["labeled", "opened"])],
        docker_command="cai-demo",
    )
    rendered = _render(spec, "simple")

    assert "  issues:" in rendered
    assert "types: [labeled, opened]" in rendered


def test_template_render_simple_schedule_event():
    """A ``schedule`` event renders the cron expression."""
    spec = _make_spec(
        slug="demo",
        events=[GitHubTriggerEvent(event="schedule", cron="0 8 1 * *")],
        docker_command="cai-demo",
    )
    rendered = _render(spec, "simple")

    assert "  schedule:" in rendered
    assert 'cron: "0 8 1 * *"' in rendered


def test_template_render_simple_workflow_dispatch_with_inputs():
    """A ``workflow_dispatch`` event with inputs renders the inputs block."""
    spec = _make_spec(
        slug="demo",
        events=[GitHubTriggerEvent(
            event="workflow_dispatch",
            inputs={
                "mode": {
                    "description": "Audit mode",
                    "required": True,
                    "default": "cost",
                    "type": "choice",
                    "options": ["cost", "errors", "architecture"],
                },
            },
        )],
        docker_command="cai-demo",
    )
    rendered = _render(spec, "simple")

    assert "workflow_dispatch:" in rendered
    assert "inputs:" in rendered
    assert "mode:" in rendered
    assert 'description: "Audit mode"' in rendered
    assert "required: true" in rendered
    assert "type: choice" in rendered
    assert "- cost" in rendered


def test_template_render_simple_workflow_dispatch_without_inputs():
    """A ``workflow_dispatch`` event without inputs has no inputs block."""
    spec = _make_spec(
        slug="demo",
        events=[GitHubTriggerEvent(event="workflow_dispatch")],
        docker_command="cai-demo",
    )
    rendered = _render(spec, "simple")

    assert "workflow_dispatch:" in rendered
    assert "inputs:" not in rendered


# ── Template rendering: gate shape ──────────────────────────────────────


def test_template_render_gate_has_check_and_audit_jobs():
    """The gate shape renders a check job and an audit job."""
    spec = _make_spec(
        slug="audit-duplication",
        events=[
            GitHubTriggerEvent(event="workflow_dispatch"),
            GitHubTriggerEvent(event="push", branches=["main"]),
        ],
        docker_command='cai-audit --mode duplication',
        permissions={"contents": "read"},
    )
    rendered = _render(spec, "gate")

    assert "  check:" in rendered
    assert "  audit:" in rendered
    assert "needs: check" in rendered
    assert "should_run" in rendered
    assert "TARGET_REPO" in rendered


def test_template_render_gate_has_gate_logic():
    """The gate shape includes the commit-count gating script."""
    spec = _make_spec(
        slug="audit-duplication",
        events=[GitHubTriggerEvent(event="push", branches=["main"])],
        docker_command="cai-audit --mode duplication",
    )
    rendered = _render(spec, "gate")

    assert "git rev-list --count HEAD" in rendered
    assert "count % 30" in rendered


def test_template_render_gate_no_concurrency_block():
    """The gate shape does not render a top-level concurrency block."""
    spec = _make_spec(
        slug="audit-duplication",
        events=[GitHubTriggerEvent(event="push", branches=["main"])],
        concurrency_group="cai-audit-dup",
        docker_command="cai-audit --mode duplication",
    )
    rendered = _render(spec, "gate")

    # Gate shape has its own concurrency in the template, but not top-level
    # The template for gate shape does NOT include a top-level concurrency block
    assert "concurrency:" not in rendered.split("on:")[0]


def test_template_render_gate_no_auth_step():
    """The gate shape does not render an authorized-user verification step."""
    spec = _make_spec(
        slug="audit-duplication",
        events=[GitHubTriggerEvent(event="push", branches=["main"])],
        authorized_user_variant="standard",
        docker_command="cai-audit --mode duplication",
    )
    rendered = _render(spec, "gate")

    assert "Verify authorized user" not in rendered


# ── Template rendering: resolve shape ───────────────────────────────────


def test_template_render_resolve_has_discover_and_resolve_jobs():
    """The resolve shape renders discover and resolve jobs with a matrix."""
    spec = _make_spec(
        slug="resolve-conflicts",
        events=[
            GitHubTriggerEvent(
                event="workflow_run",
                workflows=["Publish Docker image"],
                types=["completed"],
                branches=["main"],
            ),
            GitHubTriggerEvent(event="workflow_dispatch"),
        ],
        docker_command="cai-resolve-conflicts ${{ github.repository }}#${{ matrix.pr }}",
        permissions={"contents": "write", "pull-requests": "write"},
        concurrency_group="cai-resolve-conflicts",
    )
    rendered = _render(spec, "resolve")

    assert "  discover:" in rendered
    assert "  resolve:" in rendered
    assert "needs: discover" in rendered
    assert "strategy:" in rendered
    assert "matrix:" in rendered
    assert "CONFLICTING" in rendered


def test_template_render_resolve_has_workflow_run_trigger():
    """The resolve shape renders the workflow_run trigger details."""
    spec = _make_spec(
        slug="resolve-conflicts",
        events=[
            GitHubTriggerEvent(
                event="workflow_run",
                workflows=["Publish Docker image"],
                types=["completed"],
                branches=["main"],
            ),
        ],
        docker_command="cai-resolve-conflicts-test",
        permissions={"contents": "write"},
        concurrency_group="cai-resolve-conflicts",
    )
    rendered = _render(spec, "resolve")

    assert "workflow_run:" in rendered
    assert '"Publish Docker image"' in rendered
    assert "completed" in rendered


def test_template_render_resolve_has_concurrency():
    """The resolve shape always renders a top-level concurrency block."""
    spec = _make_spec(
        slug="resolve-conflicts",
        events=[
            GitHubTriggerEvent(
                event="workflow_run",
                workflows=["CI"],
                types=["completed"],
                branches=["main"],
            ),
        ],
        docker_command="cai-test",
        permissions={"contents": "write"},
        concurrency_group="cai-my-group",
    )
    rendered = _render(spec, "resolve")

    assert "concurrency:" in rendered
    assert "group: cai-my-group" in rendered


def test_template_render_resolve_no_auth_step():
    """The resolve shape does not render an authorized-user verification step."""
    spec = _make_spec(
        slug="resolve-conflicts",
        events=[
            GitHubTriggerEvent(
                event="workflow_run",
                workflows=["CI"],
                types=["completed"],
                branches=["main"],
            ),
        ],
        docker_command="cai-test",
        permissions={"contents": "write"},
        authorized_user_variant="standard",
    )
    rendered = _render(spec, "resolve")

    assert "Verify authorized user" not in rendered


# ── Template rendering: SKIP_SLUGS ──────────────────────────────────────


def test_skip_slugs_contains_audit_auto():
    """audit-auto is deliberately hand-written and skipped by the generator."""
    assert "audit-auto" in _gen_module.SKIP_SLUGS


# ── Template rendering: idempotency ─────────────────────────────────────


def test_template_roundtrip_for_solve_spec():
    """Rendering the solve spec produces output matching the existing YAML."""
    from cai.workflows.registry import by_slug

    spec = by_slug("solve")
    shape = _determine_shape(spec)

    rendered = _render(spec, shape)

    # Check key structural elements match the existing cai-solve.yml
    assert "name: CAI Solve" in rendered
    assert "  issues:" in rendered
    assert "types: [labeled]" in rendered
    assert "concurrency:" in rendered
    assert "cai-solve-${{ github.event.issue.number }}" in rendered
    assert "  solve:" in rendered
    assert "if: github.event.label.name == 'cai:raised'" in rendered
    assert "runs-on: ubuntu-latest" in rendered
    assert "contents: write" in rendered
    assert "issues: write" in rendered
    assert "Verify authorized user" in rendered
    assert "actions/checkout@v4" in rendered
    assert "docker.io/robotsix/cai:latest" in rendered
    assert spec.docker_command in rendered


def test_template_roundtrip_for_pr_review_spec():
    """Rendering the solve-pr spec produces the expected PR review workflow."""
    from cai.workflows.registry import by_slug

    spec = by_slug("solve-pr")
    shape = _determine_shape(spec)

    rendered = _render(spec, shape)

    assert "pull_request_review:" in rendered
    assert "types: [submitted]" in rendered
    assert "if: github.event.review.state == 'changes_requested'" in rendered
    assert "Skipping bot actor" in rendered
    assert "pull-requests: write" in rendered


def test_template_roundtrip_for_audit_errors_spec():
    """Rendering the audit-errors spec has the label filter in job_if."""
    from cai.workflows.registry import by_slug

    spec = by_slug("audit-errors")
    shape = _determine_shape(spec)

    rendered = _render(spec, shape)

    assert "cai:failed" in rendered
    assert spec.github_trigger.job_if in rendered
    assert "--mode errors" in rendered
    assert "Verify authorized user" not in rendered


def test_template_roundtrip_for_gate_spec():
    """Rendering the audit-duplication spec produces a gate-shaped workflow."""
    from cai.workflows.registry import by_slug

    spec = by_slug("audit-duplication")
    shape = _determine_shape(spec)

    rendered = _render(spec, shape)

    assert "  check:" in rendered
    assert "  audit:" in rendered
    assert "push:" in rendered
    assert "workflow_dispatch:" in rendered
    assert "TARGET_REPO" in rendered
    assert "--mode duplication" in rendered


def test_template_roundtrip_for_resolve_spec():
    """Rendering the conflicts spec produces a resolve-shaped workflow."""
    from cai.workflows.registry import by_slug

    spec = by_slug("conflicts")
    shape = _determine_shape(spec)

    rendered = _render(spec, shape)

    assert "  discover:" in rendered
    assert "  resolve:" in rendered
    assert "Publish Docker image" in rendered
    assert "CONFLICTING" in rendered
    assert "matrix:" in rendered
    assert "max-parallel: 1" in rendered


def test_template_roundtrip_for_parent_check_spec():
    """Rendering the parent-check spec produces a simple-shaped workflow with issues/closed trigger."""
    from cai.workflows.registry import by_slug

    spec = by_slug("parent-check")
    shape = _determine_shape(spec)

    assert shape == "simple"

    rendered = _render(spec, shape)

    assert "name: CAI Parent Check" in rendered
    assert "issues:" in rendered
    assert "types: [closed]" in rendered
    assert "contains(github.event.issue.labels.*.name, 'cai:sub-issue')" in rendered
    assert "  parent-check:" in rendered
    assert "issues: write" in rendered
    assert "Verify authorized user" not in rendered  # authorized_user_variant == "none"
    assert "concurrency:" not in rendered  # concurrency_group is None
    assert spec.docker_command in rendered
