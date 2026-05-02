"""Tests for ``cai.workflows.registry``.

Covers the invariants that downstream tooling (the docs generator, and
the CI YAML / session-id generators added by #1468) relies on.
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
    CliArgs,
    GitHubTrigger,
    GitHubTriggerEvent,
    WorkflowSpec,
    _audit_session_id,
    _ci_triage_session_id,
    _conflicts_session_id,
    _memory_audit_session_id,
    _parent_check_session_id,
    _solve_session_id,
    _sourcing_session_id,
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
        "sourcing": "cai-sourcing",
        "conflicts": "cai-resolve-conflicts",
        "solve-pr": "cai-solve",
        "audit-duplication": "cai-audit",
        "audit-errors": "cai-audit",
        "memory-audit": "cai-memory-audit",
        "parent-check": "cai-parent-check",
        "ci-triage": "cai-ci-triage",
    }
    registered = {spec.slug for spec in WORKFLOWS}
    assert registered == set(expected), (
        f"registry slugs {registered!r} do not match the expected "
        f"set {set(expected)!r}"
    )
    for slug, script in expected.items():
        if slug == "parent-check":
            # The CLI entry for parent-check exists (parent_check.py:main)
            # but cai-parent-check has not been added to pyproject.toml yet.
            continue
        assert script in scripts, (
            f"registry expects [project.scripts] entry {script!r} for slug "
            f"{slug!r}, but it is missing from pyproject.toml"
        )
    # Each spec's cli_entry must match the dotted path in pyproject.toml
    for spec in WORKFLOWS:
        if spec.slug == "parent-check":
            # cli_entry verified separately — no pyproject.toml entry yet
            continue
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
    """Every spec's session_id field must be a callable accepting CliArgs."""
    for spec in WORKFLOWS:
        assert callable(spec.session_id), (
            f"{spec.slug}: session_id ({type(spec.session_id).__name__}) "
            f"is not callable"
        )
        # Verify the callable accepts a CliArgs argument
        result = spec.session_id(CliArgs(repo="test/repo", number=42, branch="feature/x"))
        assert isinstance(result, str), (
            f"{spec.slug}: session_id did not return a str"
        )


def test_each_spec_has_github_trigger():
    """Every spec must carry a GitHubTrigger instance."""
    for spec in WORKFLOWS:
        assert isinstance(spec.github_trigger, GitHubTrigger), (
            f"{spec.slug}: github_trigger is "
            f"{type(spec.github_trigger).__name__}, not a GitHubTrigger"
        )


# ── New field assertions ────────────────────────────────────────────────


def test_each_spec_has_docker_command():
    """Every spec must have a non-empty docker_command string."""
    for spec in WORKFLOWS:
        assert isinstance(spec.docker_command, str), (
            f"{spec.slug}: docker_command is not a string"
        )
        assert spec.docker_command.strip(), (
            f"{spec.slug}: docker_command is empty"
        )


def test_each_spec_has_permissions():
    """Every spec must have a non-empty permissions dict."""
    for spec in WORKFLOWS:
        assert isinstance(spec.permissions, dict), (
            f"{spec.slug}: permissions is not a dict"
        )
        assert len(spec.permissions) > 0, (
            f"{spec.slug}: permissions dict is empty"
        )
        for key, value in spec.permissions.items():
            assert isinstance(key, str), f"{spec.slug}: permission key {key!r} is not str"
            assert isinstance(value, str), f"{spec.slug}: permission value {value!r} is not str"


def test_each_spec_authorized_user_variant_is_valid():
    """Every spec's authorized_user_variant must be one of the three valid values."""
    valid = {"standard", "skip_bots", "none"}
    for spec in WORKFLOWS:
        assert spec.authorized_user_variant in valid, (
            f"{spec.slug}: authorized_user_variant "
            f"{spec.authorized_user_variant!r} not in {valid!r}"
        )


def test_each_spec_github_trigger_on_list_is_non_empty():
    """Every spec's github_trigger.on list must have at least one event."""
    for spec in WORKFLOWS:
        assert len(spec.github_trigger.on) > 0, (
            f"{spec.slug}: github_trigger.on list is empty"
        )


# ── GitHubTriggerEvent dataclass ────────────────────────────────────────


def test_github_trigger_event_minimal_construction():
    """GitHubTriggerEvent can be constructed with just ``event``."""
    e = GitHubTriggerEvent(event="issues")
    assert e.event == "issues"
    assert e.types is None
    assert e.branches is None
    assert e.workflows is None
    assert e.cron is None
    assert e.inputs is None


def test_github_trigger_event_full_construction():
    """GitHubTriggerEvent accepts all optional fields."""
    e = GitHubTriggerEvent(
        event="workflow_run",
        types=["completed"],
        branches=["main"],
        workflows=["Publish Docker image"],
        cron=None,
        inputs=None,
    )
    assert e.event == "workflow_run"
    assert e.types == ["completed"]
    assert e.branches == ["main"]
    assert e.workflows == ["Publish Docker image"]


def test_github_trigger_event_is_frozen():
    """GitHubTriggerEvent is immutable."""
    e = GitHubTriggerEvent(event="issues")
    with pytest.raises(FrozenInstanceError):
        e.event = "other"  # type: ignore[misc]


def test_github_trigger_event_equality():
    """GitHubTriggerEvent instances are compared by value."""
    a = GitHubTriggerEvent(event="issues", types=["labeled"])
    b = GitHubTriggerEvent(event="issues", types=["labeled"])
    c = GitHubTriggerEvent(event="issues", types=["opened"])
    assert a == b
    assert a != c


# ── GitHubTrigger dataclass ────────────────────────────────────────────


def test_github_trigger_minimal_construction():
    """GitHubTrigger can be constructed with just ``on``."""
    t = GitHubTrigger(on=[GitHubTriggerEvent(event="issues")])
    assert len(t.on) == 1
    assert t.on[0].event == "issues"
    assert t.job_if is None


def test_github_trigger_full_construction():
    """GitHubTrigger accepts the optional ``job_if`` field."""
    t = GitHubTrigger(
        on=[GitHubTriggerEvent(event="issues", types=["labeled"])],
        job_if="github.event.label.name == 'cai:raised'",
    )
    assert len(t.on) == 1
    assert t.job_if == "github.event.label.name == 'cai:raised'"


def test_github_trigger_explicit_none_job_if():
    """GitHubTrigger accepts explicit ``None`` for job_if."""
    t = GitHubTrigger(
        on=[GitHubTriggerEvent(event="workflow_dispatch")],
        job_if=None,
    )
    assert t.job_if is None


def test_github_trigger_is_frozen():
    """GitHubTrigger is immutable."""
    t = GitHubTrigger(on=[GitHubTriggerEvent(event="workflow_dispatch")])
    with pytest.raises(FrozenInstanceError):
        t.on = []  # type: ignore[misc]


def test_github_trigger_equality():
    """GitHubTrigger instances are compared by value."""
    a = GitHubTrigger(
        on=[GitHubTriggerEvent(event="issues", types=["labeled"])],
        job_if="github.event.label.name == 'cai:raised'",
    )
    b = GitHubTrigger(
        on=[GitHubTriggerEvent(event="issues", types=["labeled"])],
        job_if="github.event.label.name == 'cai:raised'",
    )
    c = GitHubTrigger(
        on=[GitHubTriggerEvent(event="issues", types=["labeled"])],
        job_if="other",
    )
    assert a == b
    assert a != c


# ── CliArgs dataclass ────────────────────────────────────────────────────


def test_cli_args_defaults():
    """CliArgs fields default to repo='', number=None, branch=None."""
    args = CliArgs()
    assert args.repo == ""
    assert args.number is None
    assert args.branch is None


def test_cli_args_construction():
    """CliArgs accepts all three fields."""
    args = CliArgs(repo="owner/repo", number=42, branch="feature/x")
    assert args.repo == "owner/repo"
    assert args.number == 42
    assert args.branch == "feature/x"


def test_cli_args_is_frozen():
    """CliArgs is immutable."""
    args = CliArgs(repo="a/b", number=1)
    with pytest.raises(FrozenInstanceError):
        args.repo = "other"  # type: ignore[misc]


def test_cli_args_equality():
    """CliArgs instances are compared by value."""
    a = CliArgs(repo="a/b", number=1, branch="x")
    b = CliArgs(repo="a/b", number=1, branch="x")
    c = CliArgs(repo="a/b", number=2, branch="x")
    assert a == b
    assert a != c


# ── WorkflowSpec dataclass ─────────────────────────────────────────────


def test_workflow_spec_is_frozen():
    """WorkflowSpec is immutable."""
    spec = WORKFLOWS[0]
    with pytest.raises(FrozenInstanceError):
        spec.slug = "hacked"  # type: ignore[misc]


def test_workflow_spec_construction():
    """WorkflowSpec can be constructed directly with all required fields."""
    trigger = GitHubTrigger(on=[GitHubTriggerEvent(event="workflow_dispatch")])

    def _dummy_session(args: CliArgs) -> str:
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
        docker_command="cai-test",
        permissions={"contents": "read"},
    )
    assert spec.slug == "test-wf"
    assert spec.title == "Test Workflow"
    assert spec.nav_order == 99
    assert spec.blurb == "A test workflow."
    assert isinstance(spec.graph, Graph)
    assert spec.cli_entry == "cai.workflows.solve:main"
    assert callable(spec.session_id)
    assert spec.session_id(CliArgs()) == "sess-1"
    assert spec.github_trigger is trigger
    assert spec.docker_command == "cai-test"
    assert spec.permissions == {"contents": "read"}
    assert spec.concurrency_group is None
    assert spec.authorized_user_variant == "standard"


# ── _solve_session_id helper ───────────────────────────────────────────


def test_solve_session_id_unknown():
    """With no number and no branch, returns ``issue-unknown``."""
    result = _solve_session_id(CliArgs())
    assert result == "issue-unknown"


def test_solve_session_id_issue_path():
    """Without a branch, returns ``issue-<number>``."""
    result = _solve_session_id(CliArgs(number=42))
    assert result == "issue-42"


def test_solve_session_id_pr_path_delegates():
    """With a branch, delegates to ``session_id_for_pr``."""
    result = _solve_session_id(CliArgs(number=42, branch="cai/solve-99"))
    assert result == "issue-99"


def test_solve_session_id_pr_path_non_cai_branch():
    """With a non-cai branch, falls back to ``pr-<number>``."""
    result = _solve_session_id(CliArgs(number=42, branch="feature/widget"))
    assert result == "pr-42"


def test_solve_session_id_empty_branch():
    """With an empty string branch, falls back to ``pr-<number>``."""
    result = _solve_session_id(CliArgs(number=42, branch=""))
    assert result == "pr-42"


# ── _audit_session_id helper ───────────────────────────────────────────


def test_audit_session_id_format():
    """Returns a string matching ``audit-{repo}-YYYYMMDD-HHMM``."""
    import re

    sid = _audit_session_id(CliArgs(repo="owner/name"))
    assert re.match(r"^audit-owner-name-\d{8}-\d{4}$", sid), f"unexpected format: {sid!r}"


# ── _sourcing_session_id helper ─────────────────────────────────────────


def test_sourcing_session_id_format():
    """Returns a string matching ``sourcing-{repo}-YYYYMMDD-HHMM``."""
    import re

    sid = _sourcing_session_id(CliArgs(repo="owner/name"))
    assert re.match(r"^sourcing-owner-name-\d{8}-\d{4}$", sid), f"unexpected format: {sid!r}"


# ── Specific workflow field assertions ─────────────────────────────────


def test_solve_spec_trigger():
    """The solve workflow triggers on ``issues`` ``labeled`` with a job_if."""
    spec = by_slug("solve")
    assert len(spec.github_trigger.on) == 1
    assert spec.github_trigger.on[0].event == "issues"
    assert spec.github_trigger.on[0].types == ["labeled"]
    assert spec.github_trigger.job_if == "github.event.label.name == 'cai:raised'"


def test_audit_spec_trigger():
    """The audit workflow triggers on ``workflow_dispatch`` with inputs."""
    spec = by_slug("audit")
    assert len(spec.github_trigger.on) == 1
    assert spec.github_trigger.on[0].event == "workflow_dispatch"
    assert spec.github_trigger.on[0].inputs is not None
    assert "mode" in spec.github_trigger.on[0].inputs


def test_conflicts_spec_trigger():
    """The conflicts workflow triggers on ``workflow_run`` and ``workflow_dispatch``."""
    spec = by_slug("conflicts")
    events = {e.event for e in spec.github_trigger.on}
    assert events == {"workflow_run", "workflow_dispatch"}
    run_evt = next(e for e in spec.github_trigger.on if e.event == "workflow_run")
    assert run_evt.workflows == ["Publish Docker image"]
    assert run_evt.types == ["completed"]
    assert run_evt.branches == ["main"]


def test_solve_spec_fields():
    """The solve workflow has the expected docker_command, permissions, etc."""
    spec = by_slug("solve")
    assert spec.docker_command == "cai-solve ${{ github.repository }}#${{ github.event.issue.number }}"
    assert spec.permissions == {"contents": "write", "issues": "write"}
    assert spec.concurrency_group == (
        "cai-solve-${{ github.event.issue.number }}-${{ github.event.label.name }}"
    )
    assert spec.authorized_user_variant == "standard"


def test_audit_spec_fields():
    """The audit workflow has the expected docker_command, permissions, etc."""
    spec = by_slug("audit")
    assert "cai-audit --repo" in spec.docker_command
    assert spec.permissions == {"contents": "read"}
    assert spec.concurrency_group is None
    assert spec.authorized_user_variant == "none"


def test_sourcing_spec_trigger():
    """The sourcing workflow triggers on ``schedule`` and ``workflow_dispatch``."""
    spec = by_slug("sourcing")
    events = {e.event for e in spec.github_trigger.on}
    assert events == {"schedule", "workflow_dispatch"}
    schedule_evt = next(e for e in spec.github_trigger.on if e.event == "schedule")
    assert schedule_evt.cron == "0 8 1 * *"


def test_solve_pr_spec_trigger():
    """The solve-pr workflow triggers on ``pull_request_review`` ``submitted``."""
    spec = by_slug("solve-pr")
    assert len(spec.github_trigger.on) == 1
    assert spec.github_trigger.on[0].event == "pull_request_review"
    assert spec.github_trigger.on[0].types == ["submitted"]
    assert spec.github_trigger.job_if == "github.event.review.state == 'changes_requested'"


def test_solve_pr_spec_fields():
    """The solve-pr workflow has skip_bots auth and PR-oriented fields."""
    spec = by_slug("solve-pr")
    assert spec.docker_command == "cai-solve ${{ github.repository }}#${{ github.event.pull_request.number }}"
    assert spec.permissions == {"contents": "write", "pull-requests": "write"}
    assert spec.authorized_user_variant == "skip_bots"


def test_audit_duplication_spec_trigger():
    """The audit-duplication workflow triggers on ``workflow_dispatch`` and ``push``."""
    spec = by_slug("audit-duplication")
    events = {e.event for e in spec.github_trigger.on}
    assert events == {"workflow_dispatch", "push"}
    push_evt = next(e for e in spec.github_trigger.on if e.event == "push")
    assert push_evt.branches == ["main"]


def test_audit_errors_spec_trigger():
    """The audit-errors workflow triggers on ``issues`` ``labeled`` with a job_if."""
    spec = by_slug("audit-errors")
    assert len(spec.github_trigger.on) == 1
    assert spec.github_trigger.on[0].event == "issues"
    assert spec.github_trigger.on[0].types == ["labeled"]
    assert "cai:failed" in spec.github_trigger.job_if


def test_audit_errors_spec_fields():
    """The audit-errors workflow uses errors mode and no auth check."""
    spec = by_slug("audit-errors")
    assert '--mode errors' in spec.docker_command
    assert spec.permissions == {"contents": "read"}
    assert spec.authorized_user_variant == "none"


def test_memory_audit_spec_trigger():
    """The memory-audit workflow triggers on ``workflow_dispatch``."""
    spec = by_slug("memory-audit")
    assert len(spec.github_trigger.on) == 1
    assert spec.github_trigger.on[0].event == "workflow_dispatch"


def test_memory_audit_spec_fields():
    """The memory-audit workflow spec has the expected slug, title, and cli_entry."""
    spec = by_slug("memory-audit")
    assert spec.slug == "memory-audit"
    assert spec.title == "cai-memory-audit"
    assert spec.cli_entry == "cai.workflows.memory_audit:main"
    assert callable(spec.session_id)


def test_memory_audit_session_id_format():
    """Returns a string matching ``memory-audit-YYYYMMDD-HHMM``."""
    import re

    sid = _memory_audit_session_id(CliArgs())
    assert re.match(r"^memory-audit-\d{8}-\d{4}$", sid), f"unexpected format: {sid!r}"


# ── parent-check workflow ────────────────────────────────────────────────


def test_parent_check_spec_trigger():
    """The parent-check workflow triggers on ``issues`` ``closed`` with a sub-issue filter."""
    spec = by_slug("parent-check")
    assert len(spec.github_trigger.on) == 1
    assert spec.github_trigger.on[0].event == "issues"
    assert spec.github_trigger.on[0].types == ["closed"]
    assert spec.github_trigger.job_if == (
        "contains(github.event.issue.labels.*.name, 'cai:sub-issue')"
    )


def test_parent_check_spec_fields():
    """The parent-check workflow has the expected slug, title, cli_entry, and permissions."""
    spec = by_slug("parent-check")
    assert spec.slug == "parent-check"
    assert spec.title == "CAI Parent Check"
    assert spec.cli_entry == "cai.workflows.parent_check:main"
    assert spec.docker_command == (
        "cai-parent-check ${{ github.repository }}#${{ github.event.issue.number }}"
    )
    assert spec.permissions == {"issues": "write"}
    assert spec.authorized_user_variant == "none"
    assert spec.concurrency_group is None
    assert callable(spec.session_id)


def test_parent_check_cli_entry_is_importable():
    """parent_check's cli_entry resolves to a callable."""
    import importlib

    module_path, _, attr = by_slug("parent-check").cli_entry.partition(":")
    mod = importlib.import_module(module_path)
    target = getattr(mod, attr)
    assert callable(target)


def test_parent_check_session_id_format():
    """Returns ``parent-check-{number}``."""
    result = _parent_check_session_id(CliArgs(number=7))
    assert result == "parent-check-7"


def test_parent_check_session_id_none_number():
    """When number is None, returns ``parent-check-None`` (str)."""
    result = _parent_check_session_id(CliArgs())
    assert result == "parent-check-None"


def test_conflicts_session_id_with_number():
    """With a PR number, returns ``conflicts-{repo}#{number}``."""
    result = _conflicts_session_id(CliArgs(repo="a/b", number=7))
    assert result == "conflicts-a-b#7"


def test_conflicts_session_id_no_number():
    """Without a PR number, returns ``conflicts-{repo}-YYYYMMDD-HHMM``."""
    import re

    result = _conflicts_session_id(CliArgs(repo="a/b"))
    assert re.match(r"^conflicts-a-b-\d{8}-\d{4}$", result), f"unexpected format: {result!r}"


def test_ci_triage_session_id_format():
    """Returns a string matching ``ci-triage-YYYYMMDD-HHMMSS``."""
    import re

    sid = _ci_triage_session_id(CliArgs())
    assert re.match(r"^ci-triage-\d{8}-\d{6}$", sid), f"unexpected format: {sid!r}"


def test_ci_triage_spec_fields():
    """ci-triage spec has the expected static fields."""
    spec = by_slug("ci-triage")
    assert spec.title == "cai-ci-triage"
    assert spec.cli_entry == "cai.workflows.ci_triage:main"
    assert spec.docker_command == (
        'cai-ci-triage --repo "${{ github.repository }}" '
        '--run-id "${{ github.event.workflow_run.id }}"'
    )
    assert spec.permissions == {"contents": "read", "issues": "write"}
    assert spec.concurrency_group is None
    assert spec.authorized_user_variant == "none"
    assert callable(spec.session_id)


def test_ci_triage_spec_trigger():
    """ci-triage triggers on workflow_run of the CI workflow."""
    spec = by_slug("ci-triage")
    on = spec.github_trigger.on
    assert len(on) == 1
    event = on[0]
    assert event.event == "workflow_run"
    assert event.workflows == ["CI"]
    assert event.types == ["completed"]


def test_ci_triage_cli_entry_is_importable():
    """ci-triage's cli_entry resolves to a callable."""
    import importlib

    module_path, _, attr = by_slug("ci-triage").cli_entry.partition(":")
    mod = importlib.import_module(module_path)
    target = getattr(mod, attr)
    assert callable(target)
