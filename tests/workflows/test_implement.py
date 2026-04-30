from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic_ai.usage import UsageLimits

from cai.github.issues import IssueMeta
from cai.workflows.implement import ImplementNode
from cai.workflows.state import IssueState
from cai.workflows.test_runner import TestNode


@pytest.fixture
def state(tmp_path: Path) -> IssueState:
    body = tmp_path / "body.md"
    body.write_text("body")
    meta = IssueMeta(repo="o/r", number=99, title="t")
    bot = MagicMock()
    bot.token_for.return_value = "tok"
    s = IssueState(
        bot=bot,
        meta=meta,
        body_path=body,
        repo_root=tmp_path,
        branch_name="feature/x",
    )
    s.new_meta = meta
    return s


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
    assert kwargs["usage_limits"].request_limit == 60
