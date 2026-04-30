"""Tests for ``cai.workflows.registry``.

Covers the invariants that downstream tooling (the docs generator, and
later the CI YAML / session-id generators added by #1468) relies on.
"""
from __future__ import annotations

import importlib
import tomllib
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from pydantic_graph import Graph

from cai.workflows.registry import (
    WORKFLOWS,
    GitHubTrigger,
    WorkflowSpec,
    _audit_session_id,
    _solve_session_id,
    by_slug,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_each_spec_has_a_pydantic_graph_instance():
    for spec in WORKFLOWS:
        assert isinstance(spec.graph, Graph), (
            f"{spec.slug}: graph attribute is {type(spec.graph)!r}, "
            "not a pydantic_graph.Graph"
        )


def test_slugs_are_unique():
    slugs = [spec.slug for spec in WORKFLOWS]
    assert len(slugs) == len(set(slugs)), f"duplicate slugs: {slugs}"


def test_nav_orders_are_unique():
    orders = [spec.nav_order for spec in WORKFLOWS]
    assert len(orders) == len(set(orders)), f"duplicate nav_orders: {orders}"


def test_registry_covers_user_facing_cli_scripts():
    """Every ``cai-<slug>`` style script in pyproject.toml should be backed
    by a registry entry, and vice versa.

    Sub-graph-only CLIs (e.g. ``cai-issue``, ``cai-app-init``) are excluded
    deliberately — they're plumbing, not user-facing workflows.
    """
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    scripts = pyproject["project"]["scripts"]

    expected = {
        "solve": "cai-solve",
        "audit": "cai-audit",
        "conflicts": "cai-resolve-conflicts",
    }
    registered = {spec.slug for spec in WORKFLOWS}
    assert registered == set(expected), (
        f"registry slugs {registered!r} do not match the expected user-facing "
        f"set {set(expected)!r}"
    )
    for slug, script in expected.items():
        assert script in scripts, (
            f"registry expects [project.scripts] entry {script!r} for slug "
            f"{slug!r}, but it is missing from pyproject.toml"
        )
    # Each spec's cli_entry must match the dotted path in pyproject.toml
    for spec in WORKFLOWS:
        script_name = expected[spec.slug]
        assert scripts[script_name] == spec.cli_entry, (
            f"{spec.slug}: cli_entry {spec.cli_entry!r} does not match "
            f"[project.scripts] {script_name} = {scripts[script_name]!r}"
        )


def test_by_slug_returns_matching_spec():
    spec = by_slug("solve")
    assert isinstance(spec, WorkflowSpec)
    assert spec.slug == "solve"


def test_by_slug_raises_for_unknown_slug():
    with pytest.raises(KeyError):
        by_slug("does-not-exist")


def test_each_spec_cli_entry_is_importable():
    """Every spec's cli_entry must resolve to a callable via importlib."""
    for spec in WORKFLOWS:
        module_path, _, attr = spec.cli_entry.partition(":")
        mod = importlib.import_module(module_path)
        target = getattr(mod, attr)
        assert callable(target), (
            f"{spec.slug}: {spec.cli_entry} resolved to "
            f"{type(target).__name__}, which is not callable"
        )


def test_each_spec_session_id_is_callable():
    """Every spec's session_id field must be callable."""
    for spec in WORKFLOWS:
        assert callable(spec.session_id), (
            f"{spec.slug}: session_id ({type(spec.session_id).__name__}) "
            f"is not callable"
        )


def test_each_spec_has_github_trigger():
    """Every spec must carry a GitHubTrigger instance."""
    for spec in WORKFLOWS:
        assert isinstance(spec.github_trigger, GitHubTrigger), (
            f"{spec.slug}: github_trigger is "
            f"{type(spec.github_trigger).__name__}, not a GitHubTrigger"
        )


# ── GitHubTrigger dataclass ────────────────────────────────────────────


def test_github_trigger_minimal_construction():
    """GitHubTrigger can be constructed with just ``kind``."""
    t = GitHubTrigger(kind="issue_label")
    assert t.kind == "issue_label"
    assert t.label is None
    assert t.workflows is None


def test_github_trigger_full_construction():
    """GitHubTrigger accepts all optional fields."""
    t = GitHubTrigger(kind="workflow_run", label="cai:raised", workflows=["ci.yml"])
    assert t.kind == "workflow_run"
    assert t.label == "cai:raised"
    assert t.workflows == ["ci.yml"]


def test_github_trigger_explicit_none_fields():
    """GitHubTrigger accepts explicit ``None`` for optional fields."""
    t = GitHubTrigger(kind="workflow_dispatch", label=None, workflows=None)
    assert t.kind == "workflow_dispatch"
    assert t.label is None
    assert t.workflows is None


def test_github_trigger_is_frozen():
    """GitHubTrigger is immutable."""
    t = GitHubTrigger(kind="workflow_dispatch")
    with pytest.raises(FrozenInstanceError):
        t.kind = "other"  # type: ignore[misc]


def test_github_trigger_equality():
    """GitHubTrigger instances are compared by value."""
    a = GitHubTrigger(kind="issue_label", label="cai:raised")
    b = GitHubTrigger(kind="issue_label", label="cai:raised")
    c = GitHubTrigger(kind="issue_label", label="other")
    assert a == b
    assert a != c


# ── WorkflowSpec dataclass ─────────────────────────────────────────────


def test_workflow_spec_is_frozen():
    """WorkflowSpec is immutable."""
    spec = WORKFLOWS[0]
    with pytest.raises(FrozenInstanceError):
        spec.slug = "hacked"  # type: ignore[misc]


def test_workflow_spec_construction():
    """WorkflowSpec can be constructed directly with all seven fields."""
    trigger = GitHubTrigger(kind="workflow_dispatch")

    def _dummy_session() -> str:
        return "sess-1"

    spec = WorkflowSpec(
        slug="test-wf",
        title="Test Workflow",
        nav_order=99,
        blurb="A test workflow.",
        graph=WORKFLOWS[0].graph,
        cli_entry="cai.workflows.solve:main",
        session_id=_dummy_session,
        github_trigger=trigger,
    )
    assert spec.slug == "test-wf"
    assert spec.title == "Test Workflow"
    assert spec.nav_order == 99
    assert spec.blurb == "A test workflow."
    assert isinstance(spec.graph, Graph)
    assert spec.cli_entry == "cai.workflows.solve:main"
    assert callable(spec.session_id)
    assert spec.session_id() == "sess-1"
    assert spec.github_trigger is trigger


# ── _solve_session_id helper ───────────────────────────────────────────


def test_solve_session_id_issue_path():
    """Without a branch, returns ``issue-<number>``."""
    result = _solve_session_id(42)
    assert result == "issue-42"


def test_solve_session_id_pr_path_delegates():
    """With a branch, delegates to ``session_id_for_pr``."""
    result = _solve_session_id(42, branch="cai/solve-99")
    assert result == "issue-99"


def test_solve_session_id_pr_path_non_cai_branch():
    """With a non-cai branch, falls back to ``pr-<number>``."""
    result = _solve_session_id(42, branch="feature/widget")
    assert result == "pr-42"


def test_solve_session_id_empty_branch():
    """With an empty string branch, falls back to ``pr-<number>``."""
    result = _solve_session_id(42, branch="")
    assert result == "pr-42"


# ── _audit_session_id helper ───────────────────────────────────────────


def test_audit_session_id_format():
    """Returns a string matching ``audit-YYYYMMDD-HHMMSS``."""
    import re

    sid = _audit_session_id()
    assert re.match(r"^audit-\d{8}-\d{6}$", sid), f"unexpected format: {sid!r}"


# ── Specific workflow field assertions ─────────────────────────────────


def test_solve_spec_trigger():
    """The solve workflow triggers on ``issue_label`` ``cai:raised``."""
    spec = by_slug("solve")
    assert spec.github_trigger.kind == "issue_label"
    assert spec.github_trigger.label == "cai:raised"


def test_audit_spec_trigger():
    """The audit workflow triggers on ``workflow_dispatch``."""
    spec = by_slug("audit")
    assert spec.github_trigger.kind == "workflow_dispatch"


def test_conflicts_spec_trigger():
    """The conflicts workflow triggers on ``workflow_run`` with a dependency list."""
    spec = by_slug("conflicts")
    assert spec.github_trigger.kind == "workflow_run"
    assert spec.github_trigger.workflows == ["Publish Docker image"]
