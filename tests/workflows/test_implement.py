from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from pydantic_ai.usage import UsageLimits

from cai.workflows.implement import ImplementNode
from cai.workflows.state import ExploreOutput
from cai.workflows.test_runner import TestNode


def _run(node, state):
    ctx = MagicMock()
    ctx.state = state
    return asyncio.run(node.run(ctx))


@patch("cai.workflows.implement._implement_agent")
@patch("cai.workflows.implement._conflicted_files")
@patch("cai.workflows.implement.checkout_branch")
def test_implement_node_request_limit(
    mock_checkout, mock_conflicted_files, mock_agent, state
):
    mock_conflicted_files.return_value = []
    
    mock_agent_instance = MagicMock()
    mock_agent.return_value = mock_agent_instance
    
    async def mock_run(*args, **kwargs):
        class MockResult:
            output = MagicMock()
        return MockResult()
    
    mock_agent_instance.run.side_effect = mock_run

    result = _run(ImplementNode(), state)

    assert isinstance(result, TestNode)
    mock_agent_instance.run.assert_called_once()
    
    _, kwargs = mock_agent_instance.run.call_args
    assert "usage_limits" in kwargs
    assert isinstance(kwargs["usage_limits"], UsageLimits)
    assert kwargs["usage_limits"].request_limit == 120


@patch("cai.workflows.implement._implement_agent")
@patch("cai.workflows.implement._conflicted_files")
@patch("cai.workflows.implement.checkout_branch")
def test_prompt_includes_findings_when_present(
    mock_checkout, mock_conflicted_files, mock_agent, state,
):
    """When state.findings is set, the prompt includes the explore agent's findings."""
    mock_conflicted_files.return_value = []
    state.findings = ExploreOutput(
        summary="Architecture: layered. Key module: auth.",
        related_files=["src/auth.py"],
    )

    mock_agent_instance = MagicMock()
    mock_agent.return_value = mock_agent_instance

    captured_prompt = None

    async def mock_run(prompt, *args, **kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt
        class MockResult:
            output = MagicMock()
        return MockResult()

    mock_agent_instance.run.side_effect = mock_run

    _run(ImplementNode(), state)

    assert captured_prompt is not None
    assert "## Codebase findings (explore agent)" in captured_prompt
    assert "Architecture: layered. Key module: auth." in captured_prompt


@patch("cai.workflows.implement._implement_agent")
@patch("cai.workflows.implement._conflicted_files")
@patch("cai.workflows.implement.checkout_branch")
def test_prompt_omits_findings_when_none(
    mock_checkout, mock_conflicted_files, mock_agent, state,
):
    """When state.findings is None, the prompt does NOT include a findings section."""
    mock_conflicted_files.return_value = []
    state.findings = None

    mock_agent_instance = MagicMock()
    mock_agent.return_value = mock_agent_instance

    captured_prompt = None

    async def mock_run(prompt, *args, **kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt
        class MockResult:
            output = MagicMock()
        return MockResult()

    mock_agent_instance.run.side_effect = mock_run

    _run(ImplementNode(), state)

    assert captured_prompt is not None
    assert "## Codebase findings (explore agent)" not in captured_prompt


@patch("cai.workflows.implement._implement_agent")
@patch("cai.workflows.implement._conflicted_files")
@patch("cai.workflows.implement.checkout_branch")
def test_findings_appear_between_body_and_reference_files(
    mock_checkout, mock_conflicted_files, mock_agent, state, tmp_path,
):
    """The findings section sits between the issue body and reference files section."""
    mock_conflicted_files.return_value = []
    state.findings = ExploreOutput(
        summary="Key finding.",
        related_files=[],
    )

    # Create a real reference file so reference_files_section() returns content
    ref_file = tmp_path / "src" / "example.py"
    ref_file.parent.mkdir(parents=True, exist_ok=True)
    ref_file.write_text("def foo():\n    return 42\n")
    state.reference_files = ["src/example.py"]

    mock_agent_instance = MagicMock()
    mock_agent.return_value = mock_agent_instance

    captured_prompt = None

    async def mock_run(prompt, *args, **kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt
        class MockResult:
            output = MagicMock()
        return MockResult()

    mock_agent_instance.run.side_effect = mock_run

    _run(ImplementNode(), state)

    assert captured_prompt is not None
    body_idx = captured_prompt.index("## Issue body (implementation plan)")
    findings_idx = captured_prompt.index("## Codebase findings (explore agent)")
    ref_idx = captured_prompt.index("## Reference files")

    assert body_idx < findings_idx < ref_idx, (
        "Findings must appear after issue body and before reference files"
    )


@patch("cai.workflows.implement._implement_agent")
@patch("cai.workflows.implement._conflicted_files")
@patch("cai.workflows.implement.checkout_branch")
def test_prompt_includes_files_changed_instruction(
    mock_checkout, mock_conflicted_files, mock_agent, state,
):
    """The implement prompt instructs the agent to return files_changed."""
    mock_conflicted_files.return_value = []

    mock_agent_instance = MagicMock()
    mock_agent.return_value = mock_agent_instance

    captured_prompt = None

    async def mock_run(prompt, *args, **kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt
        class MockResult:
            output = MagicMock()
        return MockResult()

    mock_agent_instance.run.side_effect = mock_run

    _run(ImplementNode(), state)

    assert captured_prompt is not None
    assert "files_changed" in captured_prompt, (
        "Prompt should instruct the agent to return files_changed"
    )


@patch("cai.workflows.implement._implement_agent")
@patch("cai.workflows.implement._conflicted_files")
@patch("cai.workflows.implement.checkout_branch")
def test_reference_files_refreshed_from_files_changed(
    mock_checkout, mock_conflicted_files, mock_agent, state,
):
    """When implement returns files_changed, state.reference_files is updated."""
    mock_conflicted_files.return_value = []
    state.reference_files = ["old_file.py"]

    mock_agent_instance = MagicMock()
    mock_agent.return_value = mock_agent_instance

    async def mock_run(prompt, *args, **kwargs):
        class MockResult:
            output = MagicMock()
        MockResult.output.files_changed = ["src/a.py", "src/b.py"]
        return MockResult()

    mock_agent_instance.run.side_effect = mock_run

    _run(ImplementNode(), state)

    assert state.reference_files == ["src/a.py", "src/b.py"], (
        "state.reference_files should be replaced with files_changed from implement output"
    )


@patch("cai.workflows.implement._implement_agent")
@patch("cai.workflows.implement._conflicted_files")
@patch("cai.workflows.implement.checkout_branch")
def test_reference_files_not_refreshed_when_files_changed_empty(
    mock_checkout, mock_conflicted_files, mock_agent, state,
):
    """When implement returns empty files_changed, state.reference_files is unchanged."""
    mock_conflicted_files.return_value = []
    original_refs = ["old_file.py"]
    state.reference_files = list(original_refs)

    mock_agent_instance = MagicMock()
    mock_agent.return_value = mock_agent_instance

    async def mock_run(prompt, *args, **kwargs):
        class MockResult:
            output = MagicMock()
        MockResult.output.files_changed = []
        return MockResult()

    mock_agent_instance.run.side_effect = mock_run

    _run(ImplementNode(), state)

    assert state.reference_files == original_refs, (
        "state.reference_files should NOT be updated when files_changed is empty"
    )


# ---------------------------------------------------------------------------
# ImplementNode — push_validation_failure prompt injection
# ---------------------------------------------------------------------------


@patch("cai.workflows.implement._implement_agent")
@patch("cai.workflows.implement._conflicted_files")
@patch("cai.workflows.implement.checkout_branch")
def test_prompt_includes_push_validation_failure_when_set(
    mock_checkout, mock_conflicted_files, mock_agent, state,
):
    """When state.push_validation_failure is non-empty, the prompt includes
    a '## Pre-push validation failures to fix' section."""
    mock_conflicted_files.return_value = []

    agent_instance = MagicMock()
    mock_agent.return_value = agent_instance

    captured_prompt = None

    async def mock_run(prompt, *args, **kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt
        class MockResult:
            output = MagicMock()
        return MockResult()

    agent_instance.run.side_effect = mock_run

    state.push_validation_failure = (
        "Pre-push validation failed: empty scratch file(s) detected. "
        "Delete the following file(s) before retrying: tests/empty.py"
    )

    _run(ImplementNode(), state)

    assert captured_prompt is not None
    assert "## Pre-push validation failures to fix" in captured_prompt
    assert "empty scratch file" in captured_prompt
    assert "tests/empty.py" in captured_prompt


@patch("cai.workflows.implement._implement_agent")
@patch("cai.workflows.implement._conflicted_files")
@patch("cai.workflows.implement.checkout_branch")
def test_prompt_omits_push_validation_failure_when_empty(
    mock_checkout, mock_conflicted_files, mock_agent, state,
):
    """When state.push_validation_failure is empty, the prompt does NOT
    include the pre-push section."""
    mock_conflicted_files.return_value = []

    agent_instance = MagicMock()
    mock_agent.return_value = agent_instance

    captured_prompt = None

    async def mock_run(prompt, *args, **kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt
        class MockResult:
            output = MagicMock()
        return MockResult()

    agent_instance.run.side_effect = mock_run

    state.push_validation_failure = ""

    _run(ImplementNode(), state)

    assert captured_prompt is not None
    assert "## Pre-push validation failures to fix" not in captured_prompt
