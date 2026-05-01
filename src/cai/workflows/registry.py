"""Single source of truth for cai workflow metadata.

Each user-facing ``cai-*`` CLI is implemented as a ``pydantic_graph.Graph``.
This module collects the metadata downstream tooling needs (docs page
slug, nav order, mermaid graph) so the docs generator and any future code
generator (CI YAML, session-id strategy, …) all read from one place
instead of duplicating it.

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


@dataclass(frozen=True)
class GitHubTrigger:
    kind: str
    label: str | None = None
    workflows: list[str] | None = None


@dataclass(frozen=True)
class WorkflowSpec:
    slug: str
    title: str
    nav_order: int
    blurb: str
    graph: Graph
    cli_entry: str
    session_id: Callable[..., str]
    github_trigger: GitHubTrigger


def _solve_session_id(number: int, branch: str | None = None) -> str:
    """Return ``issue-{number}`` for issue runs; delegate to
    ``session_id_for_pr`` when a branch is supplied (PR path)."""
    if branch is None:
        return f"issue-{number}"
    return session_id_for_pr(number, branch)


def _audit_session_id() -> str:
    """Return a timestamp-based session id matching the pattern in ``audit.py``."""
    return f"audit-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"


def _sourcing_session_id() -> str:
    """Return a timestamp-based session id matching the pattern in ``sourcing.py``."""
    return f"sourcing-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"


def _memory_audit_session_id() -> str:
    """Return a timestamp-based session id for the memory audit workflow."""
    return f"memory-audit-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"


WORKFLOWS: list[WorkflowSpec] = [
    WorkflowSpec(
        slug="solve",
        title="cai-solve",
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
        github_trigger=GitHubTrigger(kind="issue_label", label="cai:raised"),
    ),
    WorkflowSpec(
        slug="audit",
        title="cai-audit",
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
        github_trigger=GitHubTrigger(kind="workflow_dispatch"),
    ),
    WorkflowSpec(
        slug="sourcing",
        title="cai-sourcing",
        nav_order=4,
        blurb=(
            "Monthly scan of the open-source ecosystem for transferable "
            "tools, libraries, and frameworks. Surfaces findings as "
            "triageable GitHub issues."
        ),
        graph=sourcing_graph,
        cli_entry="cai.workflows.sourcing:main",
        session_id=_sourcing_session_id,
        github_trigger=GitHubTrigger(kind="schedule"),
    ),
    WorkflowSpec(
        slug="conflicts",
        title="cai-resolve-conflicts",
        nav_order=3,
        blurb=(
            "Rebases a pull request onto its base branch, asking the "
            "resolve_step agent to clear conflict markers commit-by-commit. "
            "Runs a sanity test pass after a non-trivial rebase before "
            "force-pushing the rewritten head."
        ),
        graph=conflicts_graph,
        cli_entry="cai.workflows.conflicts:main",
        session_id=session_id_for_pr,
        github_trigger=GitHubTrigger(kind="workflow_run", workflows=["Publish Docker image"]),
    ),
    WorkflowSpec(
        slug="memory-audit",
        title="cai-memory-audit",
        nav_order=5,
        blurb=(
            "Scans `.cai/memory/` entries, verifies their claims against "
            "the current codebase, and marks stale or superseded entries "
            "by updating their YAML frontmatter status fields."
        ),
        graph=memory_audit_graph,
        cli_entry="cai.workflows.memory_audit:main",
        session_id=_memory_audit_session_id,
        github_trigger=GitHubTrigger(kind="workflow_dispatch"),
    ),
]


def by_slug(slug: str) -> WorkflowSpec:
    """Return the spec with ``slug``; raise ``KeyError`` if unknown."""
    for spec in WORKFLOWS:
        if spec.slug == slug:
            return spec
    raise KeyError(slug)
