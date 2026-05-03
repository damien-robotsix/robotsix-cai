from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from pydantic_ai.usage import UsageLimits

from cai.workflows.explore import ExploreNode
from cai.workflows.refine import RefineNode
from cai.workflows.state import SessionState


def _run(node, state):
    ctx = MagicMock()
    ctx.state = state
    return asyncio.run(node.run(ctx))



# ---------------------------------------------------------------------------
# ExploreNode — request limit
# ---------------------------------------------------------------------------


@patch("cai.workflows.explore._explore_agent")
def test_explore_node_request_limit(mock_agent_factory, state):
    """ExploreNode passes UsageLimits with request_limit=100 to the explore agent."""
    agent_instance = MagicMock()
    mock_agent_factory.return_value = agent_instance

    async def mock_run(prompt, *args, **kwargs):
        result = MagicMock()
        result.output = MagicMock()
        return result

    agent_instance.run = MagicMock(side_effect=mock_run)

    result = _run(ExploreNode(), state)

    assert isinstance(result, RefineNode)
    agent_instance.run.assert_called_once()

    _, kwargs = agent_instance.run.call_args
    assert "usage_limits" in kwargs
    assert isinstance(kwargs["usage_limits"], UsageLimits)
    assert kwargs["usage_limits"].request_limit == 100


# ---------------------------------------------------------------------------
# ExploreNode — session state injection
# ---------------------------------------------------------------------------


@patch("cai.workflows.explore._explore_agent")
def test_prompt_omits_prior_findings_when_no_session_state(mock_agent_factory, state):
    """When session_state is None, no 'Prior session findings' section appears."""
    state.session_state = None
    agent_instance = MagicMock()
    mock_agent_factory.return_value = agent_instance

    captured_prompt = None

    async def mock_run(prompt, *args, **kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt
        result = MagicMock()
        result.output = MagicMock()
        return result

    agent_instance.run = MagicMock(side_effect=mock_run)

    _run(ExploreNode(), state)

    assert captured_prompt is not None
    assert "## Prior session findings" not in captured_prompt


@patch("cai.workflows.explore._explore_agent")
def test_prompt_includes_prior_findings_when_session_state_has_them(mock_agent_factory, state):
    """When session_state has explore_findings, 'Prior session findings' appears."""
    state.session_state = SessionState(
        explore_findings="Found auth module.",
        explore_files=["src/auth.py"],
    )
    agent_instance = MagicMock()
    mock_agent_factory.return_value = agent_instance

    captured_prompt = None

    async def mock_run(prompt, *args, **kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt
        result = MagicMock()
        result.output = MagicMock()
        return result

    agent_instance.run = MagicMock(side_effect=mock_run)

    _run(ExploreNode(), state)

    assert captured_prompt is not None
    assert "## Prior session findings" in captured_prompt
    assert "Found auth module." in captured_prompt


@patch("cai.workflows.explore._explore_agent")
def test_prompt_omits_prior_findings_when_empty_string(mock_agent_factory, state):
    """When session_state.explore_findings is empty, no prior findings section."""
    state.session_state = SessionState(explore_findings="")
    agent_instance = MagicMock()
    mock_agent_factory.return_value = agent_instance

    captured_prompt = None

    async def mock_run(prompt, *args, **kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt
        result = MagicMock()
        result.output = MagicMock()
        return result

    agent_instance.run = MagicMock(side_effect=mock_run)

    _run(ExploreNode(), state)

    assert captured_prompt is not None
    assert "## Prior session findings" not in captured_prompt


# ---------------------------------------------------------------------------
# ExploreNode — session state saving
# ---------------------------------------------------------------------------


@patch("cai.workflows.explore.save_session_state")
@patch("cai.workflows.explore._explore_agent")
def test_saves_new_findings_when_session_state_exists(mock_agent_factory, mock_save, state):
    """When session_state exists and findings diff, save_session_state is called."""
    state.session_state = SessionState(
        explore_findings="Old findings.",
        explore_files=["old.py"],
    )
    agent_instance = MagicMock()
    mock_agent_factory.return_value = agent_instance

    async def mock_run(prompt, *args, **kwargs):
        result = MagicMock()
        result.output = MagicMock()
        result.output.summary = "New findings."
        result.output.related_files = ["new.py"]
        return result

    agent_instance.run = MagicMock(side_effect=mock_run)

    _run(ExploreNode(), state)

    mock_save.assert_called_once()
    assert state.session_state.explore_findings == "New findings."
    assert state.session_state.explore_files == ["new.py"]


@patch("cai.workflows.explore.save_session_state")
@patch("cai.workflows.explore._explore_agent")
def test_does_not_save_when_findings_unchanged(mock_agent_factory, mock_save, state):
    """When findings haven't changed, save_session_state is NOT called."""
    state.session_state = SessionState(
        explore_findings="Same findings.",
        explore_files=["same.py"],
    )
    agent_instance = MagicMock()
    mock_agent_factory.return_value = agent_instance

    async def mock_run(prompt, *args, **kwargs):
        result = MagicMock()
        result.output = MagicMock()
        result.output.summary = "Same findings."
        result.output.related_files = ["same.py"]
        return result

    agent_instance.run = MagicMock(side_effect=mock_run)

    _run(ExploreNode(), state)

    mock_save.assert_not_called()
    assert state.session_state.explore_findings == "Same findings."


@patch("cai.workflows.explore.save_session_state")
@patch("cai.workflows.explore._explore_agent")
def test_does_not_save_when_session_state_is_none(mock_agent_factory, mock_save, state):
    """When session_state is None, save_session_state is never called."""
    state.session_state = None
    agent_instance = MagicMock()
    mock_agent_factory.return_value = agent_instance

    async def mock_run(prompt, *args, **kwargs):
        result = MagicMock()
        result.output = MagicMock()
        result.output.summary = "New findings."
        result.output.related_files = ["new.py"]
        return result

    agent_instance.run = MagicMock(side_effect=mock_run)

    _run(ExploreNode(), state)

    mock_save.assert_not_called()

