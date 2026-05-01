from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from pydantic_ai.usage import UsageLimits

from cai.workflows.github_workflow_review import GitHubWorkflowReviewNode, _deps, _github_workflow_review_agent
from cai.workflows.state import GitHubWorkflowReviewOutput, ImplementOutput


def _run(node, state):
    ctx = MagicMock()
    ctx.state = state
    return asyncio.run(node.run(ctx))


# ---------------------------------------------------------------------------
# Agent cache
# ---------------------------------------------------------------------------


@patch("cai.workflows.github_workflow_review.build_deep_agent")
@patch("cai.workflows.github_workflow_review.parse_agent_md")
def test_github_workflow_review_agent_cache(mock_parse, mock_build):
    """_github_workflow_review_agent is cached (same object on second call)."""
    mock_parse.return_value = (MagicMock(), "instructions")
    mock_build.return_value = MagicMock()
    agent1 = _github_workflow_review_agent()
    agent2 = _github_workflow_review_agent()
    assert agent1 is agent2


# ---------------------------------------------------------------------------
# _deps
# ---------------------------------------------------------------------------


def test_deps_builds_deep_agent_deps(tmp_path):
    """_deps creates a DeepAgentDeps with LocalBackend rooted at the given path."""
    deps = _deps(tmp_path)
    assert deps.backend is not None
    assert str(deps.backend.root_dir) == str(tmp_path)
    assert str(tmp_path) in [str(d) for d in deps.backend._allowed_directories]


# ---------------------------------------------------------------------------
# GitHubWorkflowReviewNode
# ---------------------------------------------------------------------------


@patch("cai.workflows.github_workflow_review._github_workflow_review_agent")
def test_github_workflow_review_node_returns_test_sanity_node(mock_agent, state):
    """GitHubWorkflowReviewNode.run() returns TestSanityNode()."""
    state.implement_output = ImplementOutput(
        summary="Updated deploy workflow.",
        commit_message="fix: update deploy workflow",
        required_checks=[],
        replies=[],
    )

    mock_agent_instance = MagicMock()
    mock_agent.return_value = mock_agent_instance

    async def mock_run(prompt, *args, **kwargs):
        class MockResult:
            output = GitHubWorkflowReviewOutput(
                summary="No issues found.",
                commit_message="",
            )
        return MockResult()

    mock_agent_instance.run.side_effect = mock_run

    from cai.workflows.test_runner import TestSanityNode
    result = _run(GitHubWorkflowReviewNode(), state)

    assert isinstance(result, TestSanityNode)


@patch("cai.workflows.github_workflow_review._github_workflow_review_agent")
def test_github_workflow_review_node_stores_output(mock_agent, state):
    """GitHubWorkflowReviewNode stores the agent result on state.github_workflow_review_output."""
    state.implement_output = ImplementOutput(
        summary="Updated deploy workflow.",
        commit_message="fix: update deploy workflow",
        required_checks=[],
        replies=[],
    )

    mock_agent_instance = MagicMock()
    mock_agent.return_value = mock_agent_instance

    async def mock_run(prompt, *args, **kwargs):
        class MockResult:
            output = GitHubWorkflowReviewOutput(
                summary="- Fixed missing permissions block in deploy.yml",
                commit_message="fix: add permissions to deploy workflow",
            )
        return MockResult()

    mock_agent_instance.run.side_effect = mock_run

    _run(GitHubWorkflowReviewNode(), state)

    assert state.github_workflow_review_output is not None
    assert state.github_workflow_review_output.summary == "- Fixed missing permissions block in deploy.yml"
    assert state.github_workflow_review_output.commit_message == "fix: add permissions to deploy workflow"


@patch("cai.workflows.github_workflow_review._github_workflow_review_agent")
def test_github_workflow_review_node_prompt_includes_meta_summary_and_message(mock_agent, state):
    """The prompt sent to the agent includes the issue metadata, implementation summary, and commit message."""
    state.implement_output = ImplementOutput(
        summary="Updated CI workflow.",
        commit_message="chore: update CI workflow",
        required_checks=[],
        replies=[],
    )

    mock_agent_instance = MagicMock()
    mock_agent.return_value = mock_agent_instance

    captured_prompt = None

    async def mock_run(prompt, *args, **kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt

        class MockResult:
            output = GitHubWorkflowReviewOutput(
                summary="No issues found.",
                commit_message="",
            )
        return MockResult()

    mock_agent_instance.run.side_effect = mock_run

    _run(GitHubWorkflowReviewNode(), state)

    assert captured_prompt is not None
    assert "## Issue metadata" in captured_prompt
    assert "## Implementation summary" in captured_prompt
    assert "Updated CI workflow." in captured_prompt
    assert "## Implementation commit message" in captured_prompt
    assert "chore: update CI workflow" in captured_prompt


@patch("cai.workflows.github_workflow_review._github_workflow_review_agent")
def test_github_workflow_review_node_uses_request_limit_20(mock_agent, state):
    """GitHubWorkflowReviewNode passes UsageLimits with request_limit=20 to the agent."""
    state.implement_output = ImplementOutput(
        summary="Updated deploy workflow.",
        commit_message="fix: update deploy workflow",
        required_checks=[],
        replies=[],
    )

    mock_agent_instance = MagicMock()
    mock_agent.return_value = mock_agent_instance

    async def mock_run(prompt, *args, **kwargs):
        class MockResult:
            output = GitHubWorkflowReviewOutput(
                summary="No issues found.",
                commit_message="",
            )
        return MockResult()

    mock_agent_instance.run.side_effect = mock_run

    _run(GitHubWorkflowReviewNode(), state)

    mock_agent_instance.run.assert_called_once()
    _, kwargs = mock_agent_instance.run.call_args
    assert "usage_limits" in kwargs
    assert isinstance(kwargs["usage_limits"], UsageLimits)
    assert kwargs["usage_limits"].request_limit == 20


@patch("cai.workflows.github_workflow_review._github_workflow_review_agent")
def test_github_workflow_review_node_uses_deps(mock_agent, state):
    """GitHubWorkflowReviewNode passes deps from _deps(state.repo_root) to the agent."""
    state.implement_output = ImplementOutput(
        summary="Updated deploy workflow.",
        commit_message="fix: update deploy workflow",
        required_checks=[],
        replies=[],
    )

    mock_agent_instance = MagicMock()
    mock_agent.return_value = mock_agent_instance

    async def mock_run(prompt, *args, **kwargs):
        class MockResult:
            output = GitHubWorkflowReviewOutput(
                summary="No issues found.",
                commit_message="",
            )
        return MockResult()

    mock_agent_instance.run.side_effect = mock_run

    _run(GitHubWorkflowReviewNode(), state)

    mock_agent_instance.run.assert_called_once()
    _, kwargs = mock_agent_instance.run.call_args
    assert "deps" in kwargs
    assert str(kwargs["deps"].backend.root_dir) == str(state.repo_root)


@patch("cai.workflows.github_workflow_review._github_workflow_review_agent")
def test_github_workflow_review_node_asserts_new_meta(mock_agent, state):
    """When state.new_meta is None, GitHubWorkflowReviewNode.run raises AssertionError."""
    state.new_meta = None
    state.implement_output = ImplementOutput(
        summary="s", commit_message="c", required_checks=[], replies=[],
    )

    with pytest.raises(AssertionError):
        _run(GitHubWorkflowReviewNode(), state)


@patch("cai.workflows.github_workflow_review._github_workflow_review_agent")
def test_github_workflow_review_node_asserts_implement_output(mock_agent, state):
    """When state.implement_output is None, GitHubWorkflowReviewNode.run raises AssertionError."""
    state.implement_output = None

    with pytest.raises(AssertionError):
        _run(GitHubWorkflowReviewNode(), state)
