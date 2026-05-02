from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from pydantic_ai.usage import UsageLimits

from cai.workflows.pydantic_ai_review import (
    PydanticAIReviewNode,
    _deps,
    _pydantic_ai_review_agent,
)
from cai.workflows.state import ImplementOutput, PydanticAIReviewOutput


def _run(node, state):
    ctx = MagicMock()
    ctx.state = state
    return asyncio.run(node.run(ctx))


# ---------------------------------------------------------------------------
# Agent cache
# ---------------------------------------------------------------------------


@patch("cai.workflows.pydantic_ai_review.build_deep_agent")
@patch("cai.workflows.pydantic_ai_review.parse_agent_md")
def test_pydantic_ai_review_agent_cache(mock_parse, mock_build):
    """_pydantic_ai_review_agent is cached (same object on second call)."""
    mock_parse.return_value = (MagicMock(), "instructions")
    mock_build.return_value = MagicMock()
    agent1 = _pydantic_ai_review_agent()
    agent2 = _pydantic_ai_review_agent()
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
# PydanticAIReviewNode
# ---------------------------------------------------------------------------


@patch("cai.workflows.pydantic_ai_review._pydantic_ai_review_agent")
def test_pydantic_ai_review_node_returns_test_sanity_node(mock_agent, state):
    """PydanticAIReviewNode.run() returns TestSanityNode()."""
    state.implement_output = ImplementOutput(
        summary="Added pydantic-ai usage to solver.",
        commit_message="feat: use pydantic-ai agent",
        required_checks=[],
        replies=[],
    )

    mock_agent_instance = MagicMock()
    mock_agent.return_value = mock_agent_instance

    async def mock_run(prompt, *args, **kwargs):
        class MockResult:
            output = PydanticAIReviewOutput(
                summary="No issues found.",
                commit_message="",
            )
        return MockResult()

    mock_agent_instance.run.side_effect = mock_run

    from cai.workflows.test_runner import TestSanityNode
    result = _run(PydanticAIReviewNode(), state)

    assert isinstance(result, TestSanityNode)


@patch("cai.workflows.pydantic_ai_review._pydantic_ai_review_agent")
def test_pydantic_ai_review_node_stores_output(mock_agent, state):
    """PydanticAIReviewNode stores the agent result on state.pydantic_ai_review_output."""
    state.implement_output = ImplementOutput(
        summary="Added pydantic-ai usage to solver.",
        commit_message="feat: use pydantic-ai agent",
        required_checks=[],
        replies=[],
    )

    mock_agent_instance = MagicMock()
    mock_agent.return_value = mock_agent_instance

    async def mock_run(prompt, *args, **kwargs):
        class MockResult:
            output = PydanticAIReviewOutput(
                summary="- Fixed incorrect Agent construction in solver.py",
                commit_message="fix: correct Agent construction pattern",
            )
        return MockResult()

    mock_agent_instance.run.side_effect = mock_run

    _run(PydanticAIReviewNode(), state)

    assert state.pydantic_ai_review_output is not None
    assert state.pydantic_ai_review_output.summary == "- Fixed incorrect Agent construction in solver.py"
    assert state.pydantic_ai_review_output.commit_message == "fix: correct Agent construction pattern"


@patch("cai.workflows.pydantic_ai_review._pydantic_ai_review_agent")
def test_pydantic_ai_review_node_prompt_includes_meta_summary_and_message(mock_agent, state):
    """The prompt sent to the agent includes the issue metadata, implementation summary, and commit message."""
    state.implement_output = ImplementOutput(
        summary="Added pydantic-ai agent to solver.",
        commit_message="feat: use pydantic-ai agent",
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
            output = PydanticAIReviewOutput(
                summary="No issues found.",
                commit_message="",
            )
        return MockResult()

    mock_agent_instance.run.side_effect = mock_run

    _run(PydanticAIReviewNode(), state)

    assert captured_prompt is not None
    assert "## Issue metadata" in captured_prompt
    assert "## Implementation summary" in captured_prompt
    assert "Added pydantic-ai agent to solver." in captured_prompt
    assert "## Implementation commit message" in captured_prompt
    assert "feat: use pydantic-ai agent" in captured_prompt


@patch("cai.workflows.pydantic_ai_review._pydantic_ai_review_agent")
def test_pydantic_ai_review_node_uses_request_limit_50(mock_agent, state):
    """PydanticAIReviewNode passes UsageLimits with request_limit=50 to the agent."""
    state.implement_output = ImplementOutput(
        summary="Added pydantic-ai usage to solver.",
        commit_message="feat: use pydantic-ai agent",
        required_checks=[],
        replies=[],
    )

    mock_agent_instance = MagicMock()
    mock_agent.return_value = mock_agent_instance

    async def mock_run(prompt, *args, **kwargs):
        class MockResult:
            output = PydanticAIReviewOutput(
                summary="No issues found.",
                commit_message="",
            )
        return MockResult()

    mock_agent_instance.run.side_effect = mock_run

    _run(PydanticAIReviewNode(), state)

    mock_agent_instance.run.assert_called_once()
    _, kwargs = mock_agent_instance.run.call_args
    assert "usage_limits" in kwargs
    assert isinstance(kwargs["usage_limits"], UsageLimits)
    assert kwargs["usage_limits"].request_limit == 50


@patch("cai.workflows.pydantic_ai_review._pydantic_ai_review_agent")
def test_pydantic_ai_review_node_uses_deps(mock_agent, state):
    """PydanticAIReviewNode passes deps from _deps(state.repo_root) to the agent."""
    state.implement_output = ImplementOutput(
        summary="Added pydantic-ai usage to solver.",
        commit_message="feat: use pydantic-ai agent",
        required_checks=[],
        replies=[],
    )

    mock_agent_instance = MagicMock()
    mock_agent.return_value = mock_agent_instance

    async def mock_run(prompt, *args, **kwargs):
        class MockResult:
            output = PydanticAIReviewOutput(
                summary="No issues found.",
                commit_message="",
            )
        return MockResult()

    mock_agent_instance.run.side_effect = mock_run

    _run(PydanticAIReviewNode(), state)

    mock_agent_instance.run.assert_called_once()
    _, kwargs = mock_agent_instance.run.call_args
    assert "deps" in kwargs
    assert str(kwargs["deps"].backend.root_dir) == str(state.repo_root)


@patch("cai.workflows.pydantic_ai_review._pydantic_ai_review_agent")
def test_pydantic_ai_review_node_asserts_new_meta(mock_agent, state):
    """When state.new_meta is None, PydanticAIReviewNode.run raises AssertionError."""
    state.new_meta = None
    state.implement_output = ImplementOutput(
        summary="s", commit_message="c", required_checks=[], replies=[],
    )

    with pytest.raises(AssertionError):
        _run(PydanticAIReviewNode(), state)


@patch("cai.workflows.pydantic_ai_review._pydantic_ai_review_agent")
def test_pydantic_ai_review_node_asserts_implement_output(mock_agent, state):
    """When state.implement_output is None, PydanticAIReviewNode.run raises AssertionError."""
    state.implement_output = None

    with pytest.raises(AssertionError):
        _run(PydanticAIReviewNode(), state)


@patch("cai.workflows.pydantic_ai_review._pydantic_ai_review_agent")
def test_pydantic_ai_review_node_prompt_includes_reference_files_section(
    mock_agent, state, tmp_path,
):
    """PydanticAIReviewNode prompt includes the reference files section
    when reference_files is populated and files exist on disk."""
    state.implement_output = ImplementOutput(
        summary="Added pydantic-ai usage to solver.",
        commit_message="feat: use pydantic-ai agent",
        required_checks=[],
        replies=[],
    )

    # Create a real reference file
    ref_file = tmp_path / "src" / "solver.py"
    ref_file.parent.mkdir(parents=True, exist_ok=True)
    ref_file.write_text("from pydantic_ai import Agent\n")
    state.reference_files = ["src/solver.py"]

    mock_agent_instance = MagicMock()
    mock_agent.return_value = mock_agent_instance

    captured_prompt = None

    async def mock_run(prompt, *args, **kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt

        class MockResult:
            output = PydanticAIReviewOutput(
                summary="No issues found.",
                commit_message="",
            )
        return MockResult()

    mock_agent_instance.run.side_effect = mock_run

    _run(PydanticAIReviewNode(), state)

    assert captured_prompt is not None
    assert "## Reference files" in captured_prompt
    assert "### src/solver.py" in captured_prompt


@patch("cai.workflows.pydantic_ai_review._pydantic_ai_review_agent")
def test_pydantic_ai_review_node_prompt_omits_reference_files_section_when_empty(
    mock_agent, state,
):
    """PydanticAIReviewNode prompt does NOT include a reference files
    section when reference_files is empty."""
    state.implement_output = ImplementOutput(
        summary="Added pydantic-ai usage to solver.",
        commit_message="feat: use pydantic-ai agent",
        required_checks=[],
        replies=[],
    )
    state.reference_files = []

    mock_agent_instance = MagicMock()
    mock_agent.return_value = mock_agent_instance

    captured_prompt = None

    async def mock_run(prompt, *args, **kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt

        class MockResult:
            output = PydanticAIReviewOutput(
                summary="No issues found.",
                commit_message="",
            )
        return MockResult()

    mock_agent_instance.run.side_effect = mock_run

    _run(PydanticAIReviewNode(), state)

    assert captured_prompt is not None
    assert "## Reference files" not in captured_prompt
