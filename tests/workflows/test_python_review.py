from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

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
