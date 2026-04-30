"""Single source of truth for cai workflow metadata.

Each user-facing ``cai-*`` CLI is implemented as a ``pydantic_graph.Graph``.
This module collects the metadata downstream tooling needs (docs page
slug, nav order, mermaid graph) so the docs generator and any future code
generator (CI YAML, session-id strategy, …) all read from one place
instead of duplicating it.

Only the fields needed by ``scripts/gen_workflow_graphs.py`` are
populated for now. Later sub-issues of #1468 extend the spec with
``cli_entry``, ``session_id``, and ``github_trigger`` to drive the rest
of the per-workflow boilerplate.
"""
from __future__ import annotations

from dataclasses import dataclass

from pydantic_graph import Graph

from cai.workflows.audit import audit_graph
from cai.workflows.conflicts import conflicts_graph
from cai.workflows.fsm import solve_graph


@dataclass(frozen=True)
class WorkflowSpec:
    slug: str
    title: str
    nav_order: int
    blurb: str
    graph: Graph


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
    ),
    WorkflowSpec(
        slug="audit",
        title="cai-audit",
        nav_order=2,
        blurb=(
            "Runs the audit agent against recent Langfuse traces and files "
            "proposed improvements as GitHub issues."
        ),
        graph=audit_graph,
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
    ),
]


def by_slug(slug: str) -> WorkflowSpec:
    """Return the spec with ``slug``; raise ``KeyError`` if unknown."""
    for spec in WORKFLOWS:
        if spec.slug == slug:
            return spec
    raise KeyError(slug)
