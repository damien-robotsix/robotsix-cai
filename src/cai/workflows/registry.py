"""Single source of truth for cai workflow metadata.

Each user-facing ``cai-*`` CLI is implemented as a ``pydantic_graph.Graph``.
This module collects the metadata downstream tooling needs (docs page
slug, nav order, mermaid graph, CI YAML generator) so the docs generator
and the CI YAML generator all read from one place instead of duplicating it.

Every field on every entry is populated so that downstream tooling
(docs, CI YAML, session-id generation, GitHub event routing) has a
single source of truth.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic_graph import Graph

from cai.log.observability import session_id_for_pr
from cai.workflows.audit import audit_graph
from cai.workflows.conflicts import conflicts_graph
from cai.workflows.fsm import solve_graph
from cai.workflows.sourcing import sourcing_graph
from cai.workflows.memory_audit import memory_audit_graph
from cai.workflows.parent_check import parent_check_graph
from cai.workflows.ci_triage import ci_triage_graph


@dataclass(frozen=True)
class GitHubTriggerEvent:
    """A single trigger event within a workflow's ``on:`` block."""

    event: str
    types: list[str] | None = None
    branches: list[str] | None = None
    workflows: list[str] | None = None
    cron: str | None = None
    inputs: dict[str, dict[str, object]] | None = None


@dataclass(frozen=True)
class GitHubTrigger:
    """The complete trigger configuration for a workflow.

    ``on`` lists every trigger event. ``job_if`` is the job-level
    conditional (e.g. ``github.event.label.name == 'cai:raised'``)
    applied to the primary job for simple-shape workflows.
    """

    on: list[GitHubTriggerEvent]
    job_if: str | None = None


@dataclass(frozen=True)
class CliArgs:
    """Structured CLI inputs shared across workflow entry points for session-id
    construction.
    """
    repo: str = ""
    number: int | None = None
    branch: str | None = None


@dataclass(frozen=True)
class WorkflowSpec:
    slug: str
    title: str
    nav_order: int
    blurb: str
    graph: Graph
    cli_entry: str
    session_id: Callable[[CliArgs], str]
    github_trigger: GitHubTrigger
    docker_command: str
    permissions: dict[str, str]
    concurrency_group: str | None = None
    authorized_user_variant: str = "standard"


def _solve_session_id(args: CliArgs) -> str:
    """Return ``issue-{number}`` for issue runs; delegate to
    ``session_id_for_pr`` when a branch is supplied (PR path)."""
    if args.number is None:
        return "issue-unknown"
    if args.branch is None:
        return f"issue-{args.number}"
    return session_id_for_pr(args.number, args.branch)


def _audit_session_id(args: CliArgs) -> str:
    """Return a repo-qualified, minute-rounded session id for audit runs."""
    repo_slug = args.repo.replace("/", "-")
    return f"audit-{repo_slug}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}"


def _sourcing_session_id(args: CliArgs) -> str:
    """Return a repo-qualified, minute-rounded session id for sourcing runs."""
    repo_slug = args.repo.replace("/", "-")
    return f"sourcing-{repo_slug}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}"


def _memory_audit_session_id(args: CliArgs) -> str:
    """Return a timestamp-based session id for the memory audit workflow."""
    return f"memory-audit-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}"


def _parent_check_session_id(args: CliArgs) -> str:
    return f"parent-check-{args.number}"


def _conflicts_session_id(args: CliArgs) -> str:
    """Return a session id for conflict-resolution runs.

    When a PR number is available, use ``conflicts-{repo}#{number}``;
    otherwise fall back to a minute-rounded timestamp.
    """
    repo_slug = args.repo.replace("/", "-")
    if args.number is not None:
        return f"conflicts-{repo_slug}#{args.number}"
    return f"conflicts-{repo_slug}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}"


def _ci_triage_session_id(args: CliArgs) -> str:
    """Return a timestamp-based session id matching the pattern in ``ci_triage.py``."""
    return f"ci-triage-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"


WORKFLOWS: list[WorkflowSpec] = [
    WorkflowSpec(
        slug="solve",
        title="CAI Solve",
        nav_order=1,
        blurb=(
            "Drives a GitHub issue or pull request through the same graph. "
            "Issues are explored, refined, implemented, and pushed as a new PR. "
            "PRs enter at the implement step with their unresolved review "
            "threads in the prompt, and the bundled fix is pushed in place."
        ),
        graph=solve_graph,
        cli_entry="cai.workflows.solve:main",
        session_id=_solve_session_id,
        github_trigger=GitHubTrigger(
            on=[GitHubTriggerEvent(event="issues", types=["labeled"])],
            job_if="github.event.label.name == 'cai:raised'",
        ),
        docker_command="cai-solve ${{ github.repository }}#${{ github.event.issue.number }}",
        permissions={"contents": "write", "issues": "write"},
        # Per-(issue, label) so a cai:audit/cai:human-review label-add does
        # not cancel an in-flight cai:raised solve on the same issue.
        concurrency_group="cai-solve-${{ github.event.issue.number }}-${{ github.event.label.name }}",
        authorized_user_variant="standard",
    ),
    WorkflowSpec(
        slug="audit",
        title="CAI Audit",
        nav_order=2,
        blurb=(
            "Runs an audit agent against Langfuse traces or a cloned repository, "
            "then files proposed improvements as GitHub issues.\n\n"
            "## Modes\n\n"
            "| Mode | Description |\n"
            "|---|---|\n"
            "| `cost` | Audits the most costly session of the last 10 issue-solving runs. |\n"
            "| `errors` | Audits the 10 most recent traces that contain error-level observations. |\n"
            "| `duplication` | Clones the repo, runs jscpd, and audits copy-paste findings. |\n"
            "| `architecture` | Clones the repo and audits structural health. |\n"
            "| `security` | Clones the repo and audits for common vulnerability patterns (hardcoded secrets, unsafe subprocess, injection vectors, insecure deserialization, etc.). |"
        ),
        graph=audit_graph,
        cli_entry="cai.workflows.audit:main",
        session_id=_audit_session_id,
        github_trigger=GitHubTrigger(
            on=[
                GitHubTriggerEvent(
                    event="workflow_dispatch",
                    inputs={
                        "mode": {
                            "description": "Audit mode",
                            "required": True,
                            "default": "cost",
                            "type": "choice",
                            "options": ["cost", "errors", "architecture"],
                        },
                        "repo": {
                            "description": "Target GitHub repository for issues (owner/repo)",
                            "required": True,
                            "default": "damien-robotsix/robotsix-cai",
                        },
                    },
                ),
            ],
        ),
        docker_command='cai-audit --repo "${{ github.event.inputs.repo }}" --mode "${{ github.event.inputs.mode }}"',
        permissions={"contents": "read"},
        authorized_user_variant="none",
    ),
    WorkflowSpec(
        slug="sourcing",
        title="CAI Sourcing",
        nav_order=4,
        blurb=(
            "Monthly scan of the open-source ecosystem for transferable "
            "tools, libraries, and frameworks. Surfaces findings as "
            "triageable GitHub issues."
        ),
        graph=sourcing_graph,
        cli_entry="cai.workflows.sourcing:main",
        session_id=_sourcing_session_id,
        github_trigger=GitHubTrigger(
            on=[
                GitHubTriggerEvent(event="schedule", cron="0 8 1 * *"),
                GitHubTriggerEvent(
                    event="workflow_dispatch",
                    inputs={
                        "repo": {
                            "description": "Target GitHub repository for issues (owner/repo)",
                            "required": True,
                            "default": "damien-robotsix/robotsix-cai",
                        },
                    },
                ),
            ],
        ),
        docker_command="cai-sourcing --repo \"${{ github.event.inputs.repo || 'damien-robotsix/robotsix-cai' }}\"",
        permissions={"contents": "read"},
        authorized_user_variant="none",
    ),
    WorkflowSpec(
        slug="conflicts",
        title="CAI Resolve Conflicts",
        nav_order=3,
        blurb=(
            "Rebases a pull request onto its base branch, asking the "
            "resolve_step agent to clear conflict markers commit-by-commit. "
            "Runs a sanity test pass after a non-trivial rebase before "
            "force-pushing the rewritten head."
        ),
        graph=conflicts_graph,
        cli_entry="cai.workflows.conflicts:main",
        session_id=_conflicts_session_id,
        github_trigger=GitHubTrigger(
            on=[
                GitHubTriggerEvent(
                    event="workflow_run",
                    workflows=["Publish Docker image"],
                    types=["completed"],
                    branches=["main"],
                ),
                GitHubTriggerEvent(event="workflow_dispatch"),
            ],
        ),
        docker_command="cai-resolve-conflicts ${{ github.repository }}#${{ matrix.pr }}",
        permissions={"contents": "write", "pull-requests": "write"},
        concurrency_group="cai-resolve-conflicts",
        authorized_user_variant="none",
    ),
    WorkflowSpec(
        slug="audit-errors",
        title="CAI Audit Errors",
        nav_order=5,
        blurb=(
            "Triggered when an issue is labeled ``cai:failed``. "
            "Audits the most recent error traces and files findings "
            "as GitHub issues."
        ),
        graph=audit_graph,
        cli_entry="cai.workflows.audit:main",
        session_id=_audit_session_id,
        github_trigger=GitHubTrigger(
            on=[GitHubTriggerEvent(event="issues", types=["labeled"])],
            job_if="github.event.label.name == 'cai:failed' && !contains(github.event.issue.labels.*.name, 'cai:audit')",
        ),
        docker_command='cai-audit --repo "${{ github.repository }}" --mode errors',
        permissions={"contents": "read"},
        # Single global slot — the audit reads "10 most recent failures" from
        # Langfuse, so parallel runs from a burst of cai:failed labels would
        # all examine the same data and race to file duplicate issues.
        concurrency_group="cai-audit-errors",
        authorized_user_variant="none",
    ),
    WorkflowSpec(
        slug="solve-pr",
        title="CAI Solve (PR review)",
        nav_order=6,
        blurb=(
            "Responds to 'changes requested' pull request reviews by "
            "implementing the requested fixes and pushing them in place."
        ),
        graph=solve_graph,
        cli_entry="cai.workflows.solve:main",
        session_id=_solve_session_id,
        github_trigger=GitHubTrigger(
            on=[GitHubTriggerEvent(event="pull_request_review", types=["submitted"])],
            job_if="github.event.review.state == 'changes_requested'",
        ),
        docker_command="cai-solve ${{ github.repository }}#${{ github.event.pull_request.number }}",
        permissions={"contents": "write", "pull-requests": "write"},
        concurrency_group="cai-solve-pr-${{ github.event.pull_request.number }}",
        authorized_user_variant="skip_bots",
    ),
    WorkflowSpec(
        slug="audit-duplication",
        title="CAI Audit Duplication",
        nav_order=7,
        blurb=(
            "Runs a duplication audit via jscpd on every 30th commit "
            "to main, or on manual dispatch. Files findings as GitHub issues."
        ),
        graph=audit_graph,
        cli_entry="cai.workflows.audit:main",
        session_id=_audit_session_id,
        github_trigger=GitHubTrigger(
            on=[
                GitHubTriggerEvent(
                    event="workflow_dispatch",
                    inputs={
                        "repo": {
                            "description": "Target GitHub repository (owner/repo)",
                            "required": True,
                            "default": "damien-robotsix/robotsix-cai",
                        },
                    },
                ),
                GitHubTriggerEvent(event="push", branches=["main"]),
            ],
        ),
        docker_command='cai-audit --repo "$TARGET_REPO" --mode duplication',
        permissions={"contents": "read"},
        authorized_user_variant="none",
    ),
    WorkflowSpec(
        slug="memory-audit",
        title="cai-memory-audit",
        nav_order=8,
        blurb=(
            "Scans `.cai/memory/` entries, verifies their claims against "
            "the current codebase, and marks stale or superseded entries "
            "by updating their YAML frontmatter status fields."
        ),
        graph=memory_audit_graph,
        cli_entry="cai.workflows.memory_audit:main",
        session_id=_memory_audit_session_id,
        github_trigger=GitHubTrigger(
            on=[GitHubTriggerEvent(event="workflow_dispatch")],
        ),
        docker_command="cai-memory-audit",
        permissions={"contents": "read"},
        authorized_user_variant="none",
    ),
    WorkflowSpec(
        slug="parent-check",
        title="CAI Parent Check",
        nav_order=9,
        blurb=(
            "Triggered when a sub-issue labeled ``cai:sub-issue`` is closed. "
            "Checks whether all sibling sub-issues of the parent are also closed "
            "and, if so, files a summary finding on the parent issue."
        ),
        graph=parent_check_graph,
        cli_entry="cai.workflows.parent_check:main",
        session_id=_parent_check_session_id,
        github_trigger=GitHubTrigger(
            on=[GitHubTriggerEvent(event="issues", types=["closed"])],
            job_if="contains(github.event.issue.labels.*.name, 'cai:sub-issue')",
        ),
        docker_command="cai-parent-check ${{ github.repository }}#${{ github.event.issue.number }}",
        permissions={"issues": "write"},
        authorized_user_variant="none",
    ),
    WorkflowSpec(
        slug="ci-triage",
        title="cai-ci-triage",
        nav_order=10,
        blurb=(
            "Triggered when the CI workflow completes. Fetches failed job logs, "
            "triages the root cause with an LLM agent, and files a cai:raised "
            "issue with findings."
        ),
        graph=ci_triage_graph,
        cli_entry="cai.workflows.ci_triage:main",
        session_id=_ci_triage_session_id,
        github_trigger=GitHubTrigger(
            on=[
                GitHubTriggerEvent(
                    event="workflow_run",
                    workflows=["CI"],
                    types=["completed"],
                ),
            ],
        ),
        docker_command='cai-ci-triage --repo "${{ github.repository }}" --run-id "${{ github.event.workflow_run.id }}"',
        permissions={"contents": "read", "issues": "write"},
        authorized_user_variant="none",
    ),
]


def by_slug(slug: str) -> WorkflowSpec:
    """Return the spec with ``slug``; raise ``KeyError`` if unknown."""
    for spec in WORKFLOWS:
        if spec.slug == slug:
            return spec
    raise KeyError(slug)
