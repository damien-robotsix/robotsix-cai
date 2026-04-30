"""Tests for ``cai.workflows.registry``.

Covers the invariants that downstream tooling (the docs generator, and
later the CI YAML / session-id generators added by #1468) relies on.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from pydantic_graph import Graph

from cai.workflows.registry import WORKFLOWS, WorkflowSpec, by_slug

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


def test_by_slug_returns_matching_spec():
    spec = by_slug("solve")
    assert isinstance(spec, WorkflowSpec)
    assert spec.slug == "solve"


def test_by_slug_raises_for_unknown_slug():
    with pytest.raises(KeyError):
        by_slug("does-not-exist")
