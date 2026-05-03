from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from pydantic_ai.usage import UsageLimits

from cai.workflows.python_review import PythonReviewNode


def _run(node, state):
    ctx = MagicMock()
    ctx.state = state
    return asyncio.run(node.run(ctx))


# ---------------------------------------------------------------------------
# PythonReviewNode — request limit
# ---------------------------------------------------------------------------


@patch("cai.workflows.python_review._python_review_agent")
def test_python_review_node_request_limit(mock_agent_factory, state):
    """PythonReviewNode passes UsageLimits with request_limit=100 to the python_review agent."""
    agent_instance = MagicMock()
    mock_agent_factory.return_value = agent_instance

    async def mock_run(prompt, *args, **kwargs):
        result = MagicMock()
        result.output = MagicMock()
        return result

    agent_instance.run = MagicMock(side_effect=mock_run)

    state.implement_output = MagicMock()
    state.implement_output.summary = "changes"
    state.implement_output.commit_message = "fix: changes"

    _run(PythonReviewNode(), state)

    agent_instance.run.assert_called_once()

    _, kwargs = agent_instance.run.call_args
    assert "usage_limits" in kwargs
    assert isinstance(kwargs["usage_limits"], UsageLimits)
    assert kwargs["usage_limits"].request_limit == 100


@patch("cai.workflows.python_review._python_review_agent")
def test_python_review_node_stores_output(mock_agent_factory, state):
    """PythonReviewNode stores the agent result on state.python_review_output."""
    agent_instance = MagicMock()
    mock_agent_factory.return_value = agent_instance

    state.implement_output = MagicMock()
    state.implement_output.summary = "changes"
    state.implement_output.commit_message = "fix: changes"

    expected_output = MagicMock()
    expected_output.summary = "- Fixed import ordering in solver.py"
    expected_output.commit_message = "fix: sort imports in solver.py"

    async def mock_run(prompt, *args, **kwargs):
        result = MagicMock()
        result.output = expected_output
        return result

    agent_instance.run = MagicMock(side_effect=mock_run)

    from cai.workflows.github_workflow_review import GitHubWorkflowReviewNode
    result = _run(PythonReviewNode(), state)

    assert isinstance(result, GitHubWorkflowReviewNode)
    assert state.python_review_output is not None
    assert state.python_review_output.summary == expected_output.summary
    assert state.python_review_output.commit_message == expected_output.commit_message


@patch("cai.workflows.python_review._python_review_agent")
def test_python_review_node_prompt_includes_meta_summary_and_message(mock_agent_factory, state):
    """The prompt sent to the agent includes the issue metadata, implementation summary, and commit message."""
    agent_instance = MagicMock()
    mock_agent_factory.return_value = agent_instance

    state.implement_output = MagicMock()
    state.implement_output.summary = "Refactored auth module."
    state.implement_output.commit_message = "refactor: clean up auth module"

    captured_prompt = None

    async def mock_run(prompt, *args, **kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt
        result = MagicMock()
        result.output = MagicMock()
        return result

    agent_instance.run = MagicMock(side_effect=mock_run)

    _run(PythonReviewNode(), state)

    assert captured_prompt is not None
    assert "## Issue metadata" in captured_prompt
    assert "## Implementation summary" in captured_prompt
    assert "Refactored auth module." in captured_prompt
    assert "## Implementation commit message" in captured_prompt
    assert "refactor: clean up auth module" in captured_prompt


@patch("cai.workflows.python_review._python_review_agent")
def test_python_review_node_prompt_includes_reference_files_section(
    mock_agent_factory, state, tmp_path,
):
    """PythonReviewNode prompt includes the reference files section when
    reference_files is populated and files exist on disk."""
    agent_instance = MagicMock()
    mock_agent_factory.return_value = agent_instance

    state.implement_output = MagicMock()
    state.implement_output.summary = "changes"
    state.implement_output.commit_message = "fix: changes"

    # Create a real reference file
    ref_file = tmp_path / "src" / "auth.py"
    ref_file.parent.mkdir(parents=True, exist_ok=True)
    ref_file.write_text("def login():\n    pass\n")
    state.reference_files = ["src/auth.py"]

    captured_prompt = None

    async def mock_run(prompt, *args, **kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt
        result = MagicMock()
        result.output = MagicMock()
        return result

    agent_instance.run = MagicMock(side_effect=mock_run)

    _run(PythonReviewNode(), state)

    assert captured_prompt is not None
    assert "## Reference files" in captured_prompt
    assert "### src/auth.py" in captured_prompt


@patch("cai.workflows.python_review._python_review_agent")
def test_python_review_node_prompt_omits_reference_files_section_when_empty(
    mock_agent_factory, state,
):
    """PythonReviewNode prompt does NOT include a reference files section when
    reference_files is empty."""
    agent_instance = MagicMock()
    mock_agent_factory.return_value = agent_instance

    state.implement_output = MagicMock()
    state.implement_output.summary = "changes"
    state.implement_output.commit_message = "fix: changes"
    state.reference_files = []

    captured_prompt = None

    async def mock_run(prompt, *args, **kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt
        result = MagicMock()
        result.output = MagicMock()
        return result

    agent_instance.run = MagicMock(side_effect=mock_run)

    _run(PythonReviewNode(), state)

    assert captured_prompt is not None
    assert "## Reference files" not in captured_prompt


@patch("cai.workflows.python_review._python_review_agent")
def test_python_review_node_asserts_new_meta(mock_agent_factory, state):
    """When state.new_meta is None, PythonReviewNode.run raises AssertionError."""
    state.new_meta = None
    state.implement_output = MagicMock()

    with pytest.raises(AssertionError):
        _run(PythonReviewNode(), state)


@patch("cai.workflows.python_review._python_review_agent")
def test_python_review_node_asserts_implement_output(mock_agent_factory, state):
    """When state.implement_output is None, PythonReviewNode.run raises AssertionError."""
    state.implement_output = None

    with pytest.raises(AssertionError):
        _run(PythonReviewNode(), state)
